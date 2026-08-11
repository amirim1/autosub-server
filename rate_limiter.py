import asyncio
import ipaddress
import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from time import monotonic


DEFAULT_CAPACITY = 4096
DEFAULT_IDLE_TTL = 20 * 60.0


class TrustedProxyConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    limit: int
    window: float

    def __post_init__(self):
        if not self.name or self.limit < 1 or self.window <= 0:
            raise ValueError("rate limit policy values must be positive")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


@dataclass(frozen=True)
class ResolvedClient:
    ip: str
    malformed_forwarded: bool = False
    malformed_peer: bool = False


@dataclass
class _Bucket:
    requests: deque
    last_seen: float


def _normalize_address(value):
    address = ipaddress.ip_address(str(value or "").strip())
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def parse_trusted_proxies(value):
    networks = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        if item == "*":
            raise TrustedProxyConfigError(
                "AUTOSUB_TRUSTED_PROXIES must not trust every address"
            )
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise TrustedProxyConfigError(
                "AUTOSUB_TRUSTED_PROXIES contains an invalid address or network"
            ) from exc
        if network.prefixlen == 0:
            raise TrustedProxyConfigError(
                "AUTOSUB_TRUSTED_PROXIES must not trust every address"
            )
        networks.append(network)
    return tuple(networks)


class ClientIpResolver:
    def __init__(self, trusted_proxies=""):
        self._trusted = parse_trusted_proxies(trusted_proxies)

    def resolve(self, peer, x_forwarded_for="", x_real_ip=""):
        try:
            peer_ip = _normalize_address(peer)
        except ValueError:
            return ResolvedClient("unknown", malformed_peer=True)

        if not self._is_trusted(peer_ip):
            return ResolvedClient(str(peer_ip))

        forwarded = []
        malformed = False
        for item in str(x_forwarded_for or "").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                forwarded.append(_normalize_address(item))
            except ValueError:
                malformed = True

        for candidate in reversed(forwarded):
            if not self._is_trusted(candidate):
                return ResolvedClient(str(candidate), malformed_forwarded=malformed)

        if x_real_ip:
            try:
                real_ip = _normalize_address(x_real_ip)
            except ValueError:
                malformed = True
            else:
                return ResolvedClient(str(real_ip), malformed_forwarded=malformed)

        return ResolvedClient(str(peer_ip), malformed_forwarded=malformed)

    def _is_trusted(self, address):
        return any(address in network for network in self._trusted)


class RateLimiter:
    def __init__(
        self,
        *,
        capacity=DEFAULT_CAPACITY,
        idle_ttl=DEFAULT_IDLE_TTL,
        clock=monotonic,
    ):
        if capacity < 1 or idle_ttl <= 0:
            raise ValueError("rate limiter bounds must be positive")
        self._capacity = capacity
        self._idle_ttl = idle_ttl
        self._clock = clock
        self._buckets = OrderedDict()
        self._lock = asyncio.Lock()
        self._counters = {
            "allowed": 0,
            "rejected": 0,
            "evictions": 0,
            "idle_evictions": 0,
        }

    async def check(self, client_ip, policy):
        client_key = self._client_key(client_ip)
        key = (policy.name, client_key)
        async with self._lock:
            now = self._clock()
            self._purge_idle(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                self._evict_for_insert()
                bucket = _Bucket(deque(), now)
                self._buckets[key] = bucket

            while bucket.requests and now - bucket.requests[0] >= policy.window:
                bucket.requests.popleft()
            bucket.last_seen = now
            self._buckets.move_to_end(key)

            if len(bucket.requests) >= policy.limit:
                retry_after = max(
                    1,
                    math.ceil(bucket.requests[0] + policy.window - now),
                )
                self._counters["rejected"] += 1
                return RateLimitDecision(False, retry_after)

            bucket.requests.append(now)
            self._counters["allowed"] += 1
            return RateLimitDecision(True)

    async def stats(self):
        async with self._lock:
            return {**self._counters, "entries": len(self._buckets)}

    async def contains(self, client_ip, policy):
        key = (policy.name, self._client_key(client_ip))
        async with self._lock:
            return key in self._buckets

    def _client_key(self, client_ip):
        try:
            return str(_normalize_address(client_ip))
        except ValueError:
            return "unknown"

    def _purge_idle(self, now):
        cutoff = now - self._idle_ttl
        while self._buckets:
            _, bucket = next(iter(self._buckets.items()))
            if bucket.last_seen > cutoff:
                break
            self._buckets.popitem(last=False)
            self._counters["idle_evictions"] += 1

    def _evict_for_insert(self):
        if len(self._buckets) >= self._capacity:
            self._buckets.popitem(last=False)
            self._counters["evictions"] += 1
