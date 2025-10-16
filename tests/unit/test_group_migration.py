"""Unit tests for GroupMigration synchronization logic."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src import config
from src.migrations.group_migration import GroupMigration

pytestmark = pytest.mark.unit


class DummyMappings:
    """Lightweight in-memory mappings store for testing."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, object]] = {}

    def get_mapping(self, name: str) -> dict[str, object] | None:
        return self._store.get(name)

    def set_mapping(self, name: str, value: dict[str, object]) -> None:
        self._store[name] = value


@pytest.fixture
def dummy_mappings(monkeypatch):
    """Provide a dummy mappings store hooked into config."""
    store = DummyMappings()
    monkeypatch.setattr(config, "get_mappings", lambda: store)
    monkeypatch.setattr(config, "mappings", store)
    return store


def test_group_migration_creates_groups_and_syncs_members(tmp_path, dummy_mappings):
    """End-to-end happy path covering creation, membership sync, and role assignments."""
    # Seed required mappings for users and projects
    dummy_mappings.set_mapping(
        "project",
        {
            "PROJ": {"openproject_id": "500"},
        },
    )
    dummy_mappings.set_mapping(
        "user",
        {
            "user1": {"openproject_id": 101, "jira_key": "user1"},
            "user2": {"openproject_id": 202, "jira_key": "user2"},
        },
    )

    jira_client = MagicMock()
    op_client = MagicMock()

    jira_client.get_groups.return_value = [
        {"name": "Developers", "groupId": "grp-1"},
    ]
    jira_client.get_group_members.return_value = [{"key": "user1"}]
    jira_client.get_project_roles.return_value = [
        {
            "name": "Developers",
            "actors": [
                {"type": "atlassian-group-role-actor", "name": "Developers"},
                {"type": "atlassian-user-role-actor", "userKey": "user2"},
            ],
        },
    ]

    op_client.get_groups.side_effect = [
        [],
        [
            {"name": "Developers", "id": 300},
            {"name": "J2O Role PROJ::Developers", "id": 301},
        ],
    ]
    op_client.bulk_create_records.return_value = {
        "status": "success",
        "created": [
            {"id": 300, "index": 0},
            {"id": 301, "index": 1},
        ],
    }
    op_client.sync_group_memberships.return_value = {"updated": 1, "errors": 0}
    op_client.get_roles.return_value = [
        {"id": 10, "name": "Developer"},
        {"id": 20, "name": "Member"},
    ]
    op_client.assign_group_roles.return_value = {"updated": 1, "errors": 0}

    migration = GroupMigration(jira_client=jira_client, op_client=op_client)
    migration.data_dir = tmp_path
    migration.jira_groups_file = tmp_path / "jira_groups.json"
    migration.op_groups_file = tmp_path / "op_groups.json"
    migration.group_mapping_file = tmp_path / "group_mapping.json"

    result = migration.run()

    assert result.success
    assert result.data["groups_created"] == 2
    assert dummy_mappings.get_mapping("group") is not None

    group_mapping = dummy_mappings.get_mapping("group")
    assert group_mapping["Developers"]["openproject_id"] == 300
    assert group_mapping["J2O Role PROJ::Developers"]["role_backed"] is True

    op_client.sync_group_memberships.assert_called_once()
    op_client.assign_group_roles.assert_called_once()
    membership_payload = op_client.sync_group_memberships.call_args.args[0]
    names_to_users = {entry["name"]: entry["user_ids"] for entry in membership_payload}
    assert names_to_users["Developers"] == [101]
    assert set(names_to_users["J2O Role PROJ::Developers"]) == {101, 202}
    role_payload = op_client.assign_group_roles.call_args.args[0]
    assert role_payload[0]["role_ids"]
    mapping_path = tmp_path / "group_mapping.json"
    assert mapping_path.exists()
