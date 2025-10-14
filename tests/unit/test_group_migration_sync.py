"""Tests for GroupMigration helper methods."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.migrations.group_migration import GroupMigration

pytestmark = pytest.mark.unit


class DummyMappings:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, object]] = {}

    def set(self, name: str, value: dict[str, object]) -> None:
        self._store[name] = value

    def set_mapping(self, name: str, value: dict[str, object]) -> None:
        self._store[name] = value

    def get_mapping(self, name: str) -> dict[str, object] | None:
        return self._store.get(name)


def _migration(mappings: DummyMappings, monkeypatch) -> GroupMigration:
    migration = GroupMigration.__new__(GroupMigration)
    migration.logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    migration.jira_client = SimpleNamespace()
    migration.op_client = SimpleNamespace()
    migration.group_mapping = {}
    migration.data_dir = None

    monkeypatch.setattr("src.migrations.group_migration.config.mappings", mappings)
    monkeypatch.setattr("src.migrations.group_migration.config.get_mappings", lambda: mappings)
    return migration


def test_collect_project_role_groups(monkeypatch):
    mappings = DummyMappings()
    mappings.set(
        "project",
        {"PROJ": {"openproject_id": "400"}},
    )
    migration = _migration(mappings, monkeypatch)

    role_payload = [
        {
            "name": "Developers",
            "actors": [
                {"type": "group", "name": "Developers"},
                {"type": "user", "userKey": "user2"},
            ],
        },
        {
            "name": "Observers",
            "actors": [{"type": "user", "name": "user3"}],
        },
    ]

    migration.jira_client.get_project_roles = lambda project_key: role_payload

    role_groups, assignments = migration._collect_project_role_groups(
        {"developers": {"user1"}, "observers": set()},
    )

    assert "J2O Role PROJ::Developers" in role_groups
    assert assignments[0]["group_name"] == "J2O Role PROJ::Developers"
    assert assignments[0]["openproject_project_id"] == 400


def test_synchronize_memberships(monkeypatch):
    mappings = DummyMappings()
    mappings.set(
        "user",
        {
            "user1": {"openproject_id": 101, "jira_key": "user1"},
            "user2": {"openproject_id": 202, "jira_key": "user2"},
        },
    )
    migration = _migration(mappings, monkeypatch)

    migration.op_client.sync_group_memberships = lambda payload: {"updated": len(payload), "errors": 0}

    result = migration._synchronize_memberships(
        {"developers": {"user1"}},
        {},
        {"Developers": {"jira_name": "Developers"}},
    )

    assert result == {"updated": 1, "errors": 0}
