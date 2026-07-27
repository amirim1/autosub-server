import pytest
import aiosqlite
import os
from storage import Storage

@pytest.mark.asyncio
async def test_storage_client_groups(tmp_path):
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
