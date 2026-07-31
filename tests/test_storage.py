import pytest
import asyncio
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

        # Security rules
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
