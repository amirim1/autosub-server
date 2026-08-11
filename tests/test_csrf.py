import asyncio
import base64
import hashlib
import hmac
from pathlib import Path

import pytest

import csrf
from csrf import CsrfSecretConfigError, CsrfTokenManager, create_csrf_manager


SECRET = "test-secret-key-with-at-least-32-characters"
OTHER_SECRET = "other-secret-key-with-at-least-32-characters"


def _encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signed_token(secret, timestamp, *, version="v1", nonce=None, scope="admin"):
    nonce = nonce or _encoded(b"123456789012345678")
    payload = f"{version}.{timestamp}.{nonce}.{scope}"
    signature = hmac.new(
        secret.encode(), payload.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{payload}.{_encoded(signature)}"


def test_valid_token_is_reusable_and_nonce_is_unique():
    manager = CsrfTokenManager(SECRET, clock=lambda: 1_000_000)
    first = manager.generate()
    second = manager.generate()

    assert first.startswith("v1.1000000.")
    assert first != second
    assert manager.verify(first)
    assert manager.verify(first)
    assert SECRET not in first


def test_same_secret_supports_restart_workers_and_tabs():
    first_process = CsrfTokenManager(SECRET, clock=lambda: 1000)
    token = first_process.generate()
    restarted_process = CsrfTokenManager(SECRET, clock=lambda: 1001)
    other_worker = CsrfTokenManager(SECRET, clock=lambda: 1002)

    assert restarted_process.verify(token)
    assert other_worker.verify(token)
    assert CsrfTokenManager(OTHER_SECRET, clock=lambda: 1002).verify(token) is False


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "invalid",
        "токен",
        "v2.1000.nonce.admin.signature",
        "v1.1000.admin.signature",
        "v1.1000.nonce.admin.signature.extra",
        "v1.not-a-number.nonce.admin.signature",
        "v1.-1.MTIzNDU2Nzg5MDEyMzQ1Njc4.admin.signature",
        "v1.1000.%%%%.admin.signature",
        "x" * 2049,
    ],
)
def test_malformed_tokens_return_false(token):
    manager = CsrfTokenManager(SECRET, clock=lambda: 1000)
    assert manager.verify(token) is False


def test_signature_covers_every_payload_field():
    manager = CsrfTokenManager(SECRET, clock=lambda: 1000)
    token = manager.generate()
    parts = token.split(".")

    for index, replacement in ((1, "999"), (2, _encoded(b"x" * 18)), (3, "other")):
        changed = parts.copy()
        changed[index] = replacement
        assert manager.verify(".".join(changed)) is False

    signature = parts[-1]
    parts[-1] = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert manager.verify(".".join(parts)) is False


def test_scope_binding_and_unknown_version():
    manager = CsrfTokenManager(SECRET, clock=lambda: 1000)
    token = manager.generate("admin")

    assert manager.verify(token, "other") is False
    assert manager.verify(_signed_token(SECRET, 1000, version="v2")) is False
    assert manager.verify(token, "не-admin") is False
    with pytest.raises(ValueError, match="scope"):
        manager.generate("не-admin")


def test_expiry_future_timestamp_and_clock_skew():
    clock = {"now": 1000}
    manager = CsrfTokenManager(SECRET, clock=lambda: clock["now"])
    token = manager.generate()

    clock["now"] = 4600
    assert manager.verify(token)
    clock["now"] = 4601
    assert manager.verify(token) is False

    clock["now"] = 1000
    assert manager.verify(_signed_token(SECRET, 1060))
    assert manager.verify(_signed_token(SECRET, 1061)) is False
    assert manager.verify(_signed_token(SECRET, -1)) is False
    assert manager.verify(_signed_token(SECRET, "01000")) is False


def test_signature_comparison_is_constant_time(monkeypatch):
    calls = []
    original = csrf.hmac.compare_digest

    def record_compare(left, right):
        calls.append((left, right))
        return original(left, right)

    manager = CsrfTokenManager(SECRET, clock=lambda: 1000)
    token = manager.generate()
    monkeypatch.setattr(csrf.hmac, "compare_digest", record_compare)

    assert manager.verify(token)
    assert len(calls) == 1
    assert all(isinstance(value, bytes) for value in calls[0])


def test_twenty_async_checks_accept_the_same_token():
    manager = CsrfTokenManager(SECRET, clock=lambda: 1000)
    token = manager.generate()

    async def verify_once():
        return manager.verify(token)

    async def exercise():
        return await asyncio.gather(*(verify_once() for _ in range(20)))

    assert all(asyncio.run(exercise()))


@pytest.mark.parametrize(
    ("host", "secret", "generated", "raises"),
    [
        ("127.0.0.1", "", True, False),
        ("::1", "   ", True, False),
        ("localhost", SECRET, False, False),
        ("0.0.0.0", "", False, True),
        ("192.168.1.5", "short", False, True),
        ("0.0.0.0", SECRET, False, False),
        ("127.0.0.1", "short", False, True),
    ],
)
def test_secret_policy_matrix(host, secret, generated, raises):
    def is_loopback(value):
        return value in {"127.0.0.1", "::1", "localhost"}

    if raises:
        with pytest.raises(CsrfSecretConfigError) as error:
            create_csrf_manager(host, secret, is_loopback=is_loopback)
        if secret:
            assert secret not in str(error.value)
    else:
        manager, was_generated = create_csrf_manager(
            host, secret, is_loopback=is_loopback, clock=lambda: 1000
        )
        assert was_generated is generated
        assert manager.verify(manager.generate())


def test_backup_and_installer_paths_are_safe():
    root = Path(__file__).parents[1]
    forbidden_backup_path = "/opt/autosub-server" + "-backups"
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (root / "update.sh", root / "README.md", root / "README_EN.md")
    )
    installer = (root / "install.sh").read_text(encoding="utf-8")
    updater = (root / "update.sh").read_text(encoding="utf-8")

    assert forbidden_backup_path not in sources
    assert "/opt/autosub-server/shared/backups/" in sources
    assert 'bash "$TMP_DIR/checkout/update.sh"' in installer
    assert "secrets.token_urlsafe(48)" in updater
    assert 'if [ ! -f "$APP_DIR/shared/.env" ]' in updater
    assert "AUTOSUB_SECRET_KEY" in (root / ".env.example").read_text(encoding="utf-8")
