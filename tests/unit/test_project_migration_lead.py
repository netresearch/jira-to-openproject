"""Tests covering project lead attribute handling in ProjectMigration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.migrations.project_migration import (
    PROJECT_LEAD_CF_NAME,
    PROJECT_LEAD_DISPLAY_CF_NAME,
    ProjectMigration,
)

pytestmark = pytest.mark.unit


def _logger_stub():
    return SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        success=lambda *args, **kwargs: None,
        notice=lambda *args, **kwargs: None,
    )


def test_assign_project_lead_records_display_when_user_missing():
    migration = ProjectMigration.__new__(ProjectMigration)
    migration.logger = _logger_stub()
    migration.op_client = MagicMock()
    migration._lookup_op_user_id = lambda *_args, **_kwargs: None
    migration._get_role_id = lambda *_args, **_kwargs: None

    jira_project = {"lead": "jdoe", "lead_display": "Jane Doe"}

    migration._assign_project_lead(op_project_id=42, jira_project=jira_project)

    calls = migration.op_client.upsert_project_attribute.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["name"] == PROJECT_LEAD_CF_NAME
    assert calls[0].kwargs["value"] == "jdoe"
    assert calls[0].kwargs["field_format"] == "string"

    assert calls[1].kwargs["name"] == PROJECT_LEAD_DISPLAY_CF_NAME
    assert calls[1].kwargs["value"] == "Jane Doe"
    assert calls[1].kwargs["field_format"] == "string"

    migration.op_client.assign_user_roles.assert_not_called()


def test_assign_project_lead_sets_membership_and_display():
    migration = ProjectMigration.__new__(ProjectMigration)
    migration.logger = _logger_stub()
    migration.op_client = MagicMock()
    migration.op_client.assign_user_roles.return_value = {"success": True}
    migration._lookup_op_user_id = lambda *_args, **_kwargs: 123
    migration._get_role_id = lambda name: 77 if "project admin" in name.lower() else None

    jira_project = {"lead": "jdoe", "lead_display": "Jane Doe"}

    migration._assign_project_lead(op_project_id=7, jira_project=jira_project)

    migration.op_client.assign_user_roles.assert_called_once_with(
        project_id=7,
        user_id=123,
        role_ids=[77],
    )

    calls = migration.op_client.upsert_project_attribute.call_args_list
    assert calls[0].kwargs == {
        "project_id": 7,
        "name": PROJECT_LEAD_CF_NAME,
        "value": "123",
        "field_format": "user",
    }
    assert calls[1].kwargs == {
        "project_id": 7,
        "name": PROJECT_LEAD_DISPLAY_CF_NAME,
        "value": "Jane Doe",
        "field_format": "string",
    }
