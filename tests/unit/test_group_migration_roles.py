"""Unit tests for GroupMigration role resolution helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.migrations.group_migration import GroupMigration

pytestmark = pytest.mark.unit


def _create_migration():
    migration = GroupMigration.__new__(GroupMigration)
    migration.logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    return migration


def test_resolve_role_ids_exact_match():
    migration = _create_migration()
    lookup = {"project admin": 10, "member": 20}

    assert migration._resolve_role_ids("Project Admin", lookup) == [10]


def test_resolve_role_ids_falls_back_to_member():
    migration = _create_migration()
    lookup = {"member": 20}

    assert migration._resolve_role_ids("QA Reviewer", lookup) == [20]
    assert migration._resolve_role_ids("Read Only", lookup) == [20]
