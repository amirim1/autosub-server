import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dashboard import parse_direct_domains_text, save_admin_form


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
    storage.set_direct_domains.assert_not_awaited()


def test_save_admin_form_persists_direct_domains():
    storage = AsyncMock()
    storage.get_security_rules.return_value = {}
    storage.get_autoselects.return_value = []

    asyncio.run(
        save_admin_form(
            storage,
            {
                "direct_domains": "# comment\ndomain:example.ru\n\nfull:login.example.ru\n",
            },
        )
    )

    storage.set_direct_domains.assert_awaited_once_with(
        ["domain:example.ru", "full:login.example.ru"]
    )


def test_save_admin_form_allows_empty_direct_domain_list():
    storage = AsyncMock()
    storage.get_security_rules.return_value = {}
    storage.get_autoselects.return_value = []

    asyncio.run(save_admin_form(storage, {"direct_domains": " # comment only\n"}))

    storage.set_direct_domains.assert_awaited_once_with([])


@pytest.mark.parametrize(
    "value",
    [
        "example.ru",
        "domain:",
        "unknown:example.ru",
        "domain:" + "x" * 513,
    ],
)
def test_parse_direct_domains_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_direct_domains_text(value)


def test_invalid_direct_domains_are_validated_before_any_write():
    storage = AsyncMock()

    with pytest.raises(ValueError):
        asyncio.run(save_admin_form(storage, {"direct_domains": "example.ru"}))

    storage.set_group_rules.assert_not_awaited()
    storage.set_security_rules.assert_not_awaited()
    storage.set_probe_config.assert_not_awaited()
    storage.set_direct_domains.assert_not_awaited()


def test_parse_direct_domains_limits_entry_count():
    text = "\n".join(f"domain:{index}.example" for index in range(513))

    with pytest.raises(ValueError):
        parse_direct_domains_text(text)


def test_admin_template_does_not_offer_happ_payload_encryption():
    template = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(
        encoding="utf-8"
    )

    assert "security_happ_groups" not in template
    assert "Happ Payload" not in template
    assert 'name="direct_domains"' in template
