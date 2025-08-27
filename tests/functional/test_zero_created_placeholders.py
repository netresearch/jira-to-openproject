"""Placeholders to enforce zero-created gating across components.

These are marked xfail until each component implements consistent behavior:
if input is discovered (items to migrate) but zero items are created, the
component run() should signal failure.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.migrations.custom_field_migration import CustomFieldMigration
from src.migrations.status_migration import StatusMigration
from src.migrations.user_migration import UserMigration


@pytest.mark.xfail(reason="Zero-created gating not yet implemented for UserMigration", strict=False)
@patch("src.migrations.user_migration.OpenProjectClient")
@patch("src.migrations.user_migration.JiraClient")
def test_users_zero_created_gating(mock_jira: MagicMock, mock_op: MagicMock) -> None:
    mig = UserMigration(jira_client=mock_jira.return_value, op_client=mock_op.return_value)
    # Pretend there are unmatched users
    mig.create_user_mapping = lambda: {
        "missing": {
            "jira_key": "missing",
            "jira_name": "missing",
            "jira_email": "missing@example.com",
            "jira_display_name": "Missing User",
            "openproject_id": None,
            "openproject_login": None,
            "openproject_email": None,
            "matched_by": "none",
        },
    }
    mock_op.return_value.create_users_in_bulk.return_value = json.dumps({"created_count": 0, "created": []})
    result = mig.run()
    assert result.success is False


@pytest.mark.xfail(reason="Zero-created gating not yet implemented for CustomFieldMigration", strict=False)
@patch("src.migrations.custom_field_migration.OpenProjectClient")
@patch("src.migrations.custom_field_migration.JiraClient")
def test_custom_fields_zero_created_gating(mock_jira: MagicMock, mock_op: MagicMock) -> None:
    mig = CustomFieldMigration(jira_client=mock_jira.return_value, op_client=mock_op.return_value)
    # Force a scenario where Jira has fields but migration creates none
    mig.extract_jira_custom_fields = lambda: [{"id": "1", "name": "CF1"}]  # type: ignore[attr-defined]
    mig.extract_openproject_custom_fields = list  # type: ignore[attr-defined]
    mig.create_custom_field_mapping = lambda: {"1": {"jira": "CF1"}}  # type: ignore[attr-defined]
    mig.migrate_custom_fields = lambda: False  # indicates failure/no creations  # type: ignore[attr-defined]
    result = mig.run()
    assert result.success is False


@pytest.mark.xfail(reason="Zero-created gating not yet implemented for StatusMigration", strict=False)
@patch("src.migrations.status_migration.OpenProjectClient")
@patch("src.migrations.status_migration.JiraClient")
def test_statuses_zero_created_gating(mock_jira: MagicMock, mock_op: MagicMock) -> None:
    mig = StatusMigration(jira_client=mock_jira.return_value, op_client=mock_op.return_value)
    # Force statuses present but mapping leads to zero updates/creations
    mig.extract_jira_statuses = lambda: [{"id": "1", "name": "Open"}]  # type: ignore[attr-defined]
    mig.extract_status_categories = list  # type: ignore[attr-defined]
    mig.get_openproject_statuses = list  # type: ignore[attr-defined]
    mig.create_status_mapping = lambda: {"1": {"openproject_id": None}}  # type: ignore[attr-defined]
    # run() currently returns success when mapping proceeds; expect gating later
    result = mig.run()
    assert result.success is False


