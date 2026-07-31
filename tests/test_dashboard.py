import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from dashboard import save_admin_form


def test_save_admin_form_persists_autoselect_strategy():
    storage = AsyncMock()
    storage.get_security_rules.return_value = {
        "hide_settings_groups": ["old"],
        "happ_encrypt_groups": ["legacy"],
    }
    storage.get_autoselects.return_value = [
        {"id": "auto", "name": "Auto", "strategy": "leastPing"}
    ]

    asyncio.run(
        save_admin_form(
            storage,
            {
                "strategy_auto": "leastLoad",
                "mode_auto": "*",
                "security_hide_groups": "clients",
                "security_happ_groups": "must-not-be-used",
            },
        )
    )

    storage.update_autoselect.assert_awaited_once_with(
        "auto",
        selected_node_ids=["*"],
        tag_filter=[],
        name="Auto",
        strategy="leastLoad",
    )
    storage.set_security_rules.assert_awaited_once_with(
        {
            "hide_settings_groups": ["clients"],
            "happ_encrypt_groups": ["legacy"],
        }
    )


def test_admin_template_does_not_offer_happ_payload_encryption():
    template = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(
        encoding="utf-8"
    )

    assert "security_happ_groups" not in template
    assert "Happ Payload" not in template
