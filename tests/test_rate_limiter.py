import asyncio

import pytest

from rate_limiter import (
    ClientIpResolver,
    RateLimiter,
    RateLimitPolicy,
    TrustedProxyConfigError,
    parse_trusted_proxies,
)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


PUBLIC = RateLimitPolicy("public", 3, 60)
ADMIN = RateLimitPolicy("admin", 2, 60)


@pytest.mark.parametrize(
    ("peer", "trusted", "xff", "real", "expected"),
    [
        ("198.51.100.10", "127.0.0.1", "1.2.3.4", "", "198.51.100.10"),
        ("127.0.0.1", "127.0.0.1", "203.0.113.50", "", "203.0.113.50"),
        (
            "127.0.0.1",
            "127.0.0.1,10.0.0.2",
            "203.0.113.5, 10.0.0.2",
            "",
            "203.0.113.5",
        ),
        ("::1", "::1/128", "2001:db8::5", "", "2001:db8::5"),
        ("2001:0db8:0:0::10", "::1", "2001:db8::99", "", "2001:db8::10"),
        ("::ffff:192.0.2.1", "", "", "", "192.0.2.1"),
        ("127.0.0.1", "127.0.0.1", "", "203.0.113.9", "203.0.113.9"),
    ],
)
def test_client_ip_resolver_trust_matrix(peer, trusted, xff, real, expected):
    resolved = ClientIpResolver(trusted).resolve(peer, xff, real)
    assert resolved.ip == expected
    assert resolved.malformed_forwarded is False


def test_untrusted_peer_does_not_parse_malformed_spoofed_header():
    resolved = ClientIpResolver("127.0.0.1").resolve(
        "198.51.100.10", "secret-spoof-value"
    )
    assert resolved.ip == "198.51.100.10"
    assert resolved.malformed_forwarded is False


def test_malformed_forwarded_value_is_safe_and_does_not_hide_valid_client():
    resolved = ClientIpResolver("127.0.0.1").resolve(
        "127.0.0.1", "not-an-ip, 203.0.113.8"
    )
    assert resolved.ip == "203.0.113.8"
    assert resolved.malformed_forwarded is True
    assert ClientIpResolver("127.0.0.1").resolve(
        "127.0.0.1", "not-an-ip"
    ).ip == "127.0.0.1"


def test_malformed_peer_falls_back_to_one_safe_identity():
    resolved = ClientIpResolver("127.0.0.1").resolve("not-an-ip", "203.0.113.8")
    assert resolved.ip == "unknown"
    assert resolved.malformed_peer is True


@pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "::/0", "not-a-network"])
def test_unsafe_or_malformed_trusted_proxy_config_is_rejected(value):
    with pytest.raises(TrustedProxyConfigError, match="AUTOSUB_TRUSTED_PROXIES"):
        parse_trusted_proxies(value)


def test_empty_trusted_proxy_config_disables_forwarded_trust():
    assert parse_trusted_proxies("") == ()


@pytest.mark.asyncio
async def test_exact_limit_retry_after_and_window_boundary():
    clock = Clock()
    limiter = RateLimiter(clock=clock)

    assert (await limiter.check("192.0.2.1", PUBLIC)).allowed
    clock.now += 10
    assert (await limiter.check("192.0.2.1", PUBLIC)).allowed
    clock.now += 10
    assert (await limiter.check("192.0.2.1", PUBLIC)).allowed
    rejected = await limiter.check("192.0.2.1", PUBLIC)
    assert rejected.allowed is False
    assert rejected.retry_after == 40

    clock.now = 160
    assert (await limiter.check("192.0.2.1", PUBLIC)).allowed


@pytest.mark.asyncio
async def test_policies_and_ips_have_isolated_buckets():
    limiter = RateLimiter()
    for _ in range(PUBLIC.limit):
        assert (await limiter.check("192.0.2.1", PUBLIC)).allowed
    assert not (await limiter.check("192.0.2.1", PUBLIC)).allowed
    assert (await limiter.check("192.0.2.1", ADMIN)).allowed
    assert (await limiter.check("192.0.2.2", PUBLIC)).allowed


@pytest.mark.asyncio
async def test_lru_capacity_evicts_b_not_recently_touched_a():
    limiter = RateLimiter(capacity=3)
    policy = RateLimitPolicy("lru-abuse", 1, 60)
    for address in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
        await limiter.check(address, policy)
    assert not (await limiter.check("192.0.2.1", policy)).allowed
    await limiter.check("192.0.2.4", policy)

    assert await limiter.contains("192.0.2.1", policy)
    assert not await limiter.contains("192.0.2.2", policy)
    assert await limiter.contains("192.0.2.3", policy)
    assert (await limiter.stats())["evictions"] == 1


@pytest.mark.asyncio
async def test_idle_buckets_are_purged_before_capacity_eviction():
    clock = Clock()
    limiter = RateLimiter(capacity=2, idle_ttl=20, clock=clock)
    await limiter.check("192.0.2.1", PUBLIC)
    await limiter.check("192.0.2.2", PUBLIC)
    clock.now += 20
    await limiter.check("192.0.2.3", PUBLIC)

    stats = await limiter.stats()
    assert stats["entries"] == 1
    assert stats["idle_evictions"] == 2
    assert stats["evictions"] == 0


@pytest.mark.asyncio
async def test_one_hundred_concurrent_requests_have_exact_result():
    limiter = RateLimiter()
    policy = RateLimitPolicy("concurrent", 60, 60)
    decisions = await asyncio.gather(
        *(limiter.check("192.0.2.1", policy) for _ in range(100))
    )
    assert sum(decision.allowed for decision in decisions) == 60
    assert sum(not decision.allowed for decision in decisions) == 40
    assert all(decision.retry_after == 60 for decision in decisions[60:])


@pytest.mark.asyncio
async def test_one_hundred_different_ips_are_independently_allowed():
    limiter = RateLimiter()
    decisions = await asyncio.gather(
        *(
            limiter.check(f"198.51.100.{index}", PUBLIC)
            for index in range(1, 101)
        )
    )
    assert all(decision.allowed for decision in decisions)
    assert (await limiter.stats())["entries"] == 100
