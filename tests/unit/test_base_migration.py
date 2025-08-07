#!/usr/bin/env python3
"""Tests for the BaseMigration class, focused on dependency injection."""

import unittest
from unittest.mock import MagicMock

import pytest

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult
from src.utils.change_detector import ChangeDetector


class TestBaseMigration:
    """Test cases for BaseMigration class."""

    def test_init_without_dependencies(
        self,
        monkeypatch: pytest.MonkeyPatch,
        monkeypatch_helpers,
    ) -> None:
        """Test initialization without providing any client instances."""
        # Setup the mocks using monkeypatch helpers
        mock_jira_instance = MagicMock(spec=JiraClient)
        mock_op_instance = MagicMock(spec=OpenProjectClient)
        mock_change_detector_instance = MagicMock(spec=ChangeDetector)

        monkeypatch_helpers.mock_class_return_value(
            monkeypatch,
            "src.migrations.base_migration",
            "JiraClient",
            mock_jira_instance,
        )
        monkeypatch_helpers.mock_class_return_value(
            monkeypatch,
            "src.migrations.base_migration",
            "OpenProjectClient",
            mock_op_instance,
        )
        monkeypatch_helpers.mock_class_return_value(
            monkeypatch,
            "src.migrations.base_migration",
            "ChangeDetector",
            mock_change_detector_instance,
        )

        # Create BaseMigration instance without dependencies
        migration = BaseMigration()

        # Verify the instance has the expected clients
        assert migration.jira_client == mock_jira_instance
        assert migration.op_client == mock_op_instance
        assert migration.change_detector == mock_change_detector_instance

    def test_init_with_jira_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        monkeypatch_helpers,
    ) -> None:
        """Test initialization with JiraClient provided."""
        # Create a mock JiraClient
        mock_jira = MagicMock(spec=JiraClient)

        # Setup mocks using monkeypatch helpers
        mock_op_instance = MagicMock(spec=OpenProjectClient)
        mock_change_detector_instance = MagicMock(spec=ChangeDetector)

        monkeypatch_helpers.mock_class_return_value(
            monkeypatch,
            "src.migrations.base_migration",
            "OpenProjectClient",
            mock_op_instance,
        )
        monkeypatch_helpers.mock_class_return_value(
            monkeypatch,
            "src.migrations.base_migration",
            "ChangeDetector",
            mock_change_detector_instance,
        )

        migration = BaseMigration(jira_client=mock_jira)

        # Verify JiraClient was not created, but OpenProjectClient and ChangeDetector were
        assert migration.jira_client == mock_jira
        assert migration.op_client == mock_op_instance
        assert migration.change_detector == mock_change_detector_instance

    def test_init_with_op_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        monkeypatch_helpers,
    ) -> None:
        """Test initialization with OpenProjectClient provided."""
        # Create a mock OpenProjectClient
        mock_op = MagicMock(spec=OpenProjectClient)

        # Setup mocks using monkeypatch helpers
        mock_jira_instance = MagicMock(spec=JiraClient)
        mock_change_detector_instance = MagicMock(spec=ChangeDetector)

        monkeypatch_helpers.mock_class_return_value(
            monkeypatch,
            "src.migrations.base_migration",
            "JiraClient",
            mock_jira_instance,
        )
        monkeypatch_helpers.mock_class_return_value(
            monkeypatch,
            "src.migrations.base_migration",
            "ChangeDetector",
            mock_change_detector_instance,
        )

        migration = BaseMigration(op_client=mock_op)

        # Verify OpenProjectClient was not created, but JiraClient and ChangeDetector were
        assert migration.op_client == mock_op
        assert migration.jira_client == mock_jira_instance
        assert migration.change_detector == mock_change_detector_instance

    def test_init_with_change_detector(
        self,
        monkeypatch: pytest.MonkeyPatch,
        monkeypatch_helpers,
    ) -> None:
        """Test initialization with ChangeDetector provided."""
        # Create a mock ChangeDetector
        mock_change_detector = MagicMock(spec=ChangeDetector)

        # Setup mocks using monkeypatch helpers
        mock_jira_instance = MagicMock(spec=JiraClient)
        mock_op_instance = MagicMock(spec=OpenProjectClient)

        monkeypatch_helpers.mock_class_return_value(
            monkeypatch,
            "src.migrations.base_migration",
            "JiraClient",
            mock_jira_instance,
        )
        monkeypatch_helpers.mock_class_return_value(
            monkeypatch,
            "src.migrations.base_migration",
            "OpenProjectClient",
            mock_op_instance,
        )

        migration = BaseMigration(change_detector=mock_change_detector)

        # Verify ChangeDetector was not created, but other clients were
        assert migration.change_detector == mock_change_detector
        assert migration.jira_client == mock_jira_instance
        assert migration.op_client == mock_op_instance

    def test_detect_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        monkeypatch_helpers,
    ) -> None:
        """Test the detect_changes method delegates to ChangeDetector."""
        mock_change_detector = MagicMock(spec=ChangeDetector)
        mock_change_report = {"total_changes": 2, "changes_by_type": {"updated": 2}}

        monkeypatch_helpers.mock_method_return_value(
            monkeypatch,
            mock_change_detector,
            "detect_changes",
            mock_change_report,
        )

        migration = BaseMigration(change_detector=mock_change_detector)

        # Test data
        entities = [{"id": "1", "name": "test"}]
        entity_type = "projects"

        # Call detect_changes
        result = migration.detect_changes(entities, entity_type)

        # Verify it delegates to the ChangeDetector
        mock_change_detector.detect_changes.assert_called_once_with(
            entities,
            entity_type,
        )
        assert result == mock_change_report

    def test_create_snapshot(
        self,
        monkeypatch: pytest.MonkeyPatch,
        monkeypatch_helpers,
    ) -> None:
        """Test the create_snapshot method delegates to ChangeDetector."""
        mock_change_detector = MagicMock(spec=ChangeDetector)
        mock_snapshot_path = "/path/to/snapshot.json"

        monkeypatch_helpers.mock_method_return_value(
            monkeypatch,
            mock_change_detector,
            "create_snapshot",
            mock_snapshot_path,
        )

        migration = BaseMigration(change_detector=mock_change_detector)

        # Test data
        entities = [{"id": "1", "name": "test"}]
        entity_type = "projects"

        # Call create_snapshot
        result = migration.create_snapshot(entities, entity_type)

        # Verify it delegates to the ChangeDetector with the component name
        mock_change_detector.create_snapshot.assert_called_once_with(
            entities,
            entity_type,
            "BaseMigration",
        )
        assert result == mock_snapshot_path

    def test_get_current_entities_for_type_not_implemented(self) -> None:
        """Test that _get_current_entities_for_type raises NotImplementedError."""
        migration = BaseMigration()

        with pytest.raises(NotImplementedError) as context:
            migration._get_current_entities_for_type("projects")

        assert "must implement _get_current_entities_for_type()" in str(context.value)
        assert "projects" in str(context.value)

    def test_should_skip_migration_no_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        monkeypatch_helpers,
    ) -> None:
        """Test should_skip_migration when no changes are detected."""
        mock_change_detector = MagicMock(spec=ChangeDetector)
        mock_change_report = {"total_changes": 0, "changes_by_type": {}}

        monkeypatch_helpers.mock_method_return_value(
            monkeypatch,
            mock_change_detector,
            "detect_changes",
            mock_change_report,
        )

        migration = BaseMigration(change_detector=mock_change_detector)

        # Mock the _get_current_entities_for_type method using monkeypatch
        mock_get_entities = MagicMock(return_value=[{"id": "1"}])
        monkeypatch.setattr(
            migration,
            "_get_current_entities_for_type",
            mock_get_entities,
        )

        # Call should_skip_migration
        should_skip, change_report = migration.should_skip_migration("projects")

        # Should skip when no changes detected
        assert should_skip is True
        assert change_report == mock_change_report

    def test_should_skip_migration_with_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        monkeypatch_helpers,
    ) -> None:
        """Test should_skip_migration when changes are detected."""
        mock_change_detector = MagicMock(spec=ChangeDetector)
        mock_change_report = {"total_changes": 2, "changes_by_type": {"updated": 2}}

        monkeypatch_helpers.mock_method_return_value(
            monkeypatch,
            mock_change_detector,
            "detect_changes",
            mock_change_report,
        )

        migration = BaseMigration(change_detector=mock_change_detector)

        # Mock the _get_current_entities_for_type method using monkeypatch
        mock_get_entities = MagicMock(return_value=[{"id": "1"}])
        monkeypatch.setattr(
            migration,
            "_get_current_entities_for_type",
            mock_get_entities,
        )

        # Call should_skip_migration
        should_skip, change_report = migration.should_skip_migration("projects")

        # Should not skip when changes detected
        assert should_skip is False
        assert change_report == mock_change_report

    def test_should_skip_migration_error_handling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test should_skip_migration error handling."""
        migration = BaseMigration()

        # Mock the _get_current_entities_for_type method to raise an exception using monkeypatch
        mock_get_entities = MagicMock(side_effect=Exception("Test error"))
        monkeypatch.setattr(
            migration,
            "_get_current_entities_for_type",
            mock_get_entities,
        )

        # Call should_skip_migration
        should_skip, change_report = migration.should_skip_migration("projects")

        # Should not skip when error occurs (fail safe)
        assert should_skip is False
        assert change_report is None

    def test_run_with_change_detection_no_entity_type(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test run_with_change_detection without entity type falls back to regular run."""
        migration = BaseMigration()

        # Mock the run method using monkeypatch
        mock_run = MagicMock(return_value=ComponentResult(success=True))
        monkeypatch.setattr(migration, "run", mock_run)

        # Call without entity type
        result = migration.run_with_change_detection()

        # Should call regular run method
        mock_run.assert_called_once()
        assert result.success is True

    def test_run_with_change_detection_no_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test run_with_change_detection when no changes are detected."""
        migration = BaseMigration()

        # Mock should_skip_migration using monkeypatch
        mock_should_skip = MagicMock(return_value=(True, {"total_changes": 0}))
        monkeypatch.setattr(migration, "should_skip_migration", mock_should_skip)

        # Call with entity type
        result = migration.run_with_change_detection("projects")

        # Should skip migration and return success
        assert result.success is True
        assert "No changes detected" in result.message
        assert result.details["change_report"]["total_changes"] == 0

    def test_run_with_change_detection_with_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        monkeypatch_helpers,
    ) -> None:
        """Test run_with_change_detection when changes are detected."""
        mock_change_detector = MagicMock(spec=ChangeDetector)
        migration = BaseMigration(change_detector=mock_change_detector)

        # Mock methods using monkeypatch
        change_report = {"total_changes": 2}
        mock_should_skip = MagicMock(return_value=(False, change_report))
        mock_run = MagicMock(return_value=ComponentResult(success=True, details={}))
        mock_get_entities = MagicMock(return_value=[{"id": "1"}])

        monkeypatch.setattr(migration, "should_skip_migration", mock_should_skip)
        monkeypatch.setattr(migration, "run", mock_run)
        monkeypatch.setattr(
            migration,
            "_get_current_entities_for_type",
            mock_get_entities,
        )

        monkeypatch_helpers.mock_method_return_value(
            monkeypatch,
            mock_change_detector,
            "create_snapshot",
            "/path/to/snapshot.json",
        )

        # Call with entity type
        result = migration.run_with_change_detection("projects")

        # Should run migration and create snapshot
        mock_run.assert_called_once()
        mock_get_entities.assert_called_with("projects")
        mock_change_detector.create_snapshot.assert_called_once()
        assert result.success is True
        assert "snapshot_created" in result.details
        assert "change_report" in result.details


if __name__ == "__main__":
    unittest.main()
