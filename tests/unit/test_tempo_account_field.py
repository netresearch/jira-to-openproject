# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The Jira Tempo "Account" field lands in the "Tempo Account" work package field."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

TEMPO_TYPE = "com.tempoplugin.tempo-accounts:accounts.customfield"
# Field and value shapes as Jira Server returned them in the NR live run.
JIRA_ACCOUNT_FIELD = {
    "id": "customfield_10881",
    "name": "Account",
    "schema": {"type": "account", "custom": TEMPO_TYPE, "customId": 10881},
}
JIRA_ACCOUNT_LEAD = {
    "id": "customfield_19681",
    "name": "Account Lead",
    "schema": {"type": "user", "custom": "com.onresolve.jira.groovy.groovyrunner:scripted-field", "customId": 19681},
}
ACCOUNT_VALUE = {
    "self": "https://jira.example.com/rest/tempo-accounts/1/account/72",
    "id": "72",
    "key": "NR",
    "name": "Netresearch",
}


@pytest.fixture
def _mappings(monkeypatch: pytest.MonkeyPatch) -> dict:
    import src.config as cfg

    store: dict = {}

    class DummyMappings:
        def get_mapping(self, name: str) -> dict:
            return store.get(name, {})

        def set_mapping(self, name: str, value: dict) -> None:
            store[name] = value

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)
    return store


def _field_mapping(op_fields: list[dict]) -> dict:
    from src.application.components.custom_field_migration import CustomFieldMigration

    mig = CustomFieldMigration(jira_client=MagicMock(), op_client=MagicMock())
    mig.jira_custom_fields = [JIRA_ACCOUNT_FIELD, JIRA_ACCOUNT_LEAD]
    mig.op_custom_fields = op_fields
    return mig.create_custom_field_mapping()


def test_tempo_field_maps_to_existing_tempo_account_field_not_to_account(_mappings) -> None:
    op_fields = [
        {"id": 185, "name": "Account", "field_format": "text"},
        {"id": 23, "name": "Account Lead", "field_format": "user"},
        {"id": 200, "name": " Tempo account ", "field_format": "string"},
    ]

    mapping = _field_mapping(op_fields)

    assert mapping["customfield_10881"]["openproject_id"] == 200
    assert mapping["customfield_10881"]["openproject_type"] == "string"
    assert mapping["customfield_10881"]["matched_by"] == "name"
    assert mapping["customfield_19681"]["openproject_id"] == 23


def test_tempo_field_creates_tempo_account_field_on_fresh_target(_mappings) -> None:
    mapping = _field_mapping([{"id": 185, "name": "Account", "field_format": "text"}])

    entry = mapping["customfield_10881"]
    assert entry["openproject_name"] == "Tempo Account"
    assert entry["openproject_type"] == "string"
    assert entry["matched_by"] == "create"
    assert entry["openproject_id"] is None


def test_content_pass_writes_the_account_key(tmp_path: Path, _mappings) -> None:
    from src.application.components.work_package_content_migration import WorkPackageContentMigration

    jira = MagicMock()
    mig = WorkPackageContentMigration(jira_client=jira, op_client=MagicMock())
    mig.data_dir = tmp_path
    mig.custom_field_mapping = {
        "customfield_10881": {"openproject_id": 200, "openproject_type": "string", "jira_custom_type": TEMPO_TYPE},
        "customfield_10882": {"openproject_id": 201, "openproject_type": "string", "jira_custom_type": TEMPO_TYPE},
        "customfield_10883": {"openproject_id": 202, "openproject_type": "string", "jira_custom_type": TEMPO_TYPE},
    }
    issue = SimpleNamespace(
        key="NR-58",
        raw={
            "fields": {
                "customfield_10881": ACCOUNT_VALUE,
                "customfield_10882": {"id": "73", "name": "Keyless"},
                "customfield_10883": "NR-1",
            }
        },
    )

    updates = mig._custom_field_updates(issue)

    assert updates["customField200"] == "NR"
    # An account without a key is refused visibly, never written as its name.
    assert updates["customField201"] == {"unmapped_tempo_account": True}
    # A plain value is kept as Jira sent it; issue keys in it are not rewritten as links.
    assert updates["customField202"] == "NR-1"
