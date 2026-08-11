import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from contextvars import copy_context
from dataclasses import dataclass
from time import monotonic

from http_client_errors import (
    UpstreamConnectionError,
    UpstreamServerError,
    UpstreamTimeoutError,
)
from logging_utils import set_request_id
from logger import logger


DEFAULT_CAPACITY = 256
DEFAULT_FRESH_TTL = 30.0
DEFAULT_STALE_WINDOW = 300.0
DEFAULT_MAX_PAYLOAD_BYTES = 256 * 1024

SubscriptionResult = tuple[str, str, Mapping[str, str]]
BuildSubscription = Callable[[], Awaitable[SubscriptionResult]]


class SubscriptionCacheClosedError(RuntimeError):
    pass


@dataclass(frozen=True)
class CacheKey:
    subscription_hash: str
    query_hash: str
    variant_hash: str
    representation: str
    mode: str
    generation: int


@dataclass(frozen=True)
class CachedSubscription:
    payload: str
    content_type: str
    headers: tuple[tuple[str, str], ...]
    size_bytes: int

    def materialize(self):
        return self.payload, self.content_type, dict(self.headers)


@dataclass(frozen=True)
class _Entry:
    value: CachedSubscription
    fresh_until: float
    stale_until: float


def _digest(value):
    return hashlib.sha256(
        str(value or "").encode("utf-8", errors="replace")
    ).hexdigest()


def _is_transient_error(error):
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        if isinstance(
            current,
            (UpstreamConnectionError, UpstreamServerError, UpstreamTimeoutError),
        ):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class SubscriptionCache:
    def __init__(
        self,
        *,
        capacity=DEFAULT_CAPACITY,
        fresh_ttl=DEFAULT_FRESH_TTL,
        stale_window=DEFAULT_STALE_WINDOW,
        max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
        clock=monotonic,
    ):
        if capacity < 1 or fresh_ttl < 0 or stale_window < 0:
            raise ValueError("subscription cache limits must be non-negative")
        if max_payload_bytes < 1:
            raise ValueError("subscription cache payload limit must be positive")
        self._capacity = capacity
        self._fresh_ttl = fresh_ttl
        self._stale_window = stale_window
        self._max_payload_bytes = max_payload_bytes
        self._clock = clock
        self._entries = OrderedDict()
        self._inflight = {}
        self._generation = 0
        self._accepting = True
        self._lock = asyncio.Lock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "builds": 0,
            "coalesced_waiters": 0,
            "evictions": 0,
            "stale_served": 0,
        }

    async def make_key(
        self,
        sub_id,
        query="",
        *,
        variant="",
        representation="json",
        mode="public",
    ):
        async with self._lock:
            self._require_accepting()
            generation = self._generation
        return CacheKey(
            _digest(sub_id),
            _digest(query),
            _digest(variant),
            representation,
            mode,
            generation,
        )

    async def get_or_build(self, key, build):
        stale_entry = None
        async with self._lock:
            self._require_accepting()
            now = self._clock()
            entry = self._entries.get(key)
            if entry is not None and now < entry.fresh_until:
                self._entries.move_to_end(key)
                self._stats["hits"] += 1
                return entry.value.materialize()
            if entry is not None and now < entry.stale_until:
                stale_entry = entry
                self._entries.move_to_end(key)
            elif entry is not None:
                self._entries.pop(key, None)

            task = self._inflight.get(key)
            if task is None:
                self._stats["misses"] += 1
                self._stats["builds"] += 1
                task = self._create_build_task(key, build)
                self._inflight[key] = task
            else:
                self._stats["coalesced_waiters"] += 1

        try:
            value = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if stale_entry is None or not _is_transient_error(error):
                raise
            async with self._lock:
                if self._clock() >= stale_entry.stale_until:
                    raise
                current = self._entries.get(key)
                if current is not stale_entry:
                    raise
                self._entries.move_to_end(key)
                self._stats["stale_served"] += 1
                logger.warning(
                    "Serving stale subscription after transient refresh failure "
                    "error_type=%s",
                    type(error).__name__,
                )
                return stale_entry.value.materialize()
        return value.materialize()

    def _create_build_task(self, key, build):
        context = copy_context()
        context.run(set_request_id, "-")
        return context.run(asyncio.create_task, self._run_build(key, build))

    async def _run_build(self, key, build):
        task = asyncio.current_task()
        try:
            result = await build()
            value = self._make_value(result)
            if value.size_bytes <= self._max_payload_bytes:
                async with self._lock:
                    if self._accepting:
                        now = self._clock()
                        self._purge_expired(now)
                        self._entries[key] = _Entry(
                            value,
                            now + self._fresh_ttl,
                            now + self._fresh_ttl + self._stale_window,
                        )
                        self._entries.move_to_end(key)
                        self._evict_to_capacity()
            return value
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

    def _make_value(self, result):
        payload, content_type, headers = result
        payload = str(payload)
        return CachedSubscription(
            payload,
            str(content_type),
            tuple((str(key), str(value)) for key, value in headers.items()),
            len(payload.encode("utf-8", errors="replace")),
        )

    def _purge_expired(self, now):
        expired = [
            key for key, entry in self._entries.items() if now >= entry.stale_until
        ]
        for key in expired:
            self._entries.pop(key, None)

    def _evict_to_capacity(self):
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
            self._stats["evictions"] += 1

    async def invalidate(self):
        async with self._lock:
            self._require_accepting()
            self._generation += 1

    async def close(self):
        async with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._inflight.clear()
            self._entries.clear()

    async def stats(self):
        async with self._lock:
            return {
                **self._stats,
                "entries": len(self._entries),
                "inflight": len(self._inflight),
                "generation": self._generation,
            }

    def _require_accepting(self):
        if not self._accepting:
            raise SubscriptionCacheClosedError("subscription cache is closed")
