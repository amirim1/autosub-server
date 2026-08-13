import asyncio
import json

import pytest

from config import DEFAULT_DIRECT_DOMAINS
from storage import Storage

def test_storage_client_groups(tmp_path):
    async def _test():
        db_file = tmp_path / "test.db"
        store = Storage(str(db_file))
        await store.connect()
        
        await store.set_client_groups("sub_123", "test@example.com", "vip, stable")
        groups = await store.get_client_groups("sub_123")
        assert groups == ["vip", "stable"]
        
        email = await store.get_client_email("sub_123")
        assert email == "test@example.com"
        
        await store.delete_client_groups("sub_123")
        groups2 = await store.get_client_groups("sub_123")
        assert groups2 == []
        
        await store.close()
        
    asyncio.run(_test())


def test_client_group_upsert_preserves_row_identity(tmp_path):
    async def _test():
        store = Storage(str(tmp_path / "upsert.db"))
        await store.connect()

        await store.set_client_groups("sub_123", "first@example.test", "stable")
        async with store.conn.execute(
            "SELECT id, created_at FROM client_groups WHERE sub_id = ?", ("sub_123",)
        ) as cursor:
            original = await cursor.fetchone()

        await store.set_client_groups("sub_123", "second@example.test", "vip")
        async with store.conn.execute(
            "SELECT id, created_at, email, groups FROM client_groups WHERE sub_id = ?",
            ("sub_123",),
        ) as cursor:
            updated = await cursor.fetchone()

        assert updated["id"] == original["id"]
        assert updated["created_at"] == original["created_at"]
        assert updated["email"] == "second@example.test"
        assert updated["groups"] == "vip"
        await store.close()

    asyncio.run(_test())


def test_storage_autoselect_crud_and_security(tmp_path):
    async def _test():
        db_file = tmp_path / "test2.db"
        store = Storage(str(db_file))
        await store.connect()

        # Add autoselect
        await store.add_autoselect("de_auto", "🇩🇪 Германия Авто", tag_filter=["DE"])
        autos = await store.get_autoselects()
        de = next((a for a in autos if a["id"] == "de_auto"), None)
        assert de is not None
        assert de["name"] == "🇩🇪 Германия Авто"
        assert de["tag_filter"] == ["DE"]

        # Update autoselect name
        await store.update_autoselect("de_auto", name="🇩🇪 Германия Супер Авто")
        autos2 = await store.get_autoselects()
        de2 = next((a for a in autos2 if a["id"] == "de_auto"), None)
        assert de2["name"] == "🇩🇪 Германия Супер Авто"

        await store.update_autoselect("de_auto", strategy="leastLoad")
        autos_strategy = await store.get_autoselects()
        de_strategy = next(a for a in autos_strategy if a["id"] == "de_auto")
        assert de_strategy["strategy"] == "leastLoad"

        await store.update_autoselect("de_auto", strategy="invalid")
        autos_fallback = await store.get_autoselects()
        de_fallback = next(a for a in autos_fallback if a["id"] == "de_auto")
        assert de_fallback["strategy"] == "leastPing"

        # Legacy Happ rules remain readable for non-destructive compatibility.
        await store.set_security_rules({"hide_settings_groups": ["default"], "happ_encrypt_groups": ["vip"]})
        sec = await store.get_security_rules()
        assert sec["hide_settings_groups"] == ["default"]
        assert sec["happ_encrypt_groups"] == ["vip"]

        # Delete autoselect
        await store.delete_autoselect("de_auto")
        autos3 = await store.get_autoselects()
        assert not any(a["id"] == "de_auto" for a in autos3)

        await store.close()

    asyncio.run(_test())


def test_direct_domains_default_and_persistence(tmp_path):
    async def _test():
        db_file = tmp_path / "direct-domains.db"
        store = Storage(db_file)
        await store.connect()

        assert await store.get_direct_domains() == list(DEFAULT_DIRECT_DOMAINS)

        custom = ["domain:example.ru", "full:login.example.ru"]
        await store.set_direct_domains(custom)
        assert await store.get_direct_domains() == custom

        await store.set_direct_domains([])
        assert await store.get_direct_domains() == []
        await store.close()

        reopened = Storage(db_file)
        await reopened.connect()
        assert await reopened.get_direct_domains() == []
        assert await reopened.get_meta("schema_version") == "4"
        await reopened.close()

    asyncio.run(_test())


def test_invalid_stored_direct_domains_fall_back_to_defaults(tmp_path):
    async def _test():
        store = Storage(tmp_path / "invalid-direct-domains.db")
        await store.connect()
        await store.set_meta("direct_domains", json.dumps(["not-an-xray-rule"]))

        assert await store.get_direct_domains() == list(DEFAULT_DIRECT_DOMAINS)
        await store.close()

    asyncio.run(_test())
