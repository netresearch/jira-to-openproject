"""Tests for BaseMigration data preservation workflow functionality."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult
from src.utils.change_detector import ChangeDetector, ChangeReport
from src.utils.data_preservation_manager import DataPreservationManager
from src.utils.state_manager import StateManager


class DataPreservationTestMigration(BaseMigration):
    """Test migration class for data preservation workflow testing."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.run_called = False
        self.mock_entities = [
            {"id": "1", "key": "TEST-1", "name": "Test Issue 1"},
            {"id": "2", "key": "TEST-2", "name": "Test Issue 2"},
        ]

    def run(self) -> ComponentResult:
        """Mock implementation of run method."""
        self.run_called = True
        return ComponentResult(
            success=True,
            message="Test migration completed",
            success_count=2,
            failed_count=0,
            total_count=2,
        )

    def _get_current_entities_for_type(self, entity_type: str):
        """Mock implementation for entity retrieval."""
        return self.mock_entities


@pytest.fixture
def mock_clients():
    """Provide mock clients."""
    jira_client = Mock(spec=JiraClient)
    openproject_client = Mock(spec=OpenProjectClient)
    return jira_client, openproject_client


@pytest.fixture
def mock_change_detector():
    """Provide mock change detector."""
    detector = Mock(spec=ChangeDetector)
    detector.detect_changes.return_value = ChangeReport(
        has_changes=True,
        changes={"created": 1, "updated": 1, "deleted": 0},
        priority_score=50,
        change_summary="2 entities detected",
        total_entities=2,
        entity_type="issues",
        total_changes=2,
    )
    return detector


@pytest.fixture
def mock_state_manager():
    """Provide mock state manager."""
    manager = Mock(spec=StateManager)
    manager.start_migration_record.return_value = "test-record-123"
    manager.create_state_snapshot.return_value = "test-snapshot-123"
    return manager


@pytest.fixture
def mock_data_preservation_manager():
    """Provide mock data preservation manager."""
    manager = Mock(spec=DataPreservationManager)
    manager.analyze_preservation_status.return_value = {
        "total_conflicts": 1,
        "conflicts_by_resolution": {"preserve_jira": 1},
        "conflicts": [
            {
                "entity_id": "1",
                "entity_type": "issues",
                "conflict_type": "modification",
                "resolution": "preserve_jira",
            }
        ],
    }
    return manager


@pytest.fixture
def migration_with_preservation(
    mock_clients,
    mock_change_detector,
    mock_state_manager,
    mock_data_preservation_manager,
):
    """Provide migration instance with all dependencies for data preservation."""
    jira_client, openproject_client = mock_clients

    migration = DataPreservationTestMigration(
        jira_client=jira_client,
        op_client=openproject_client,
        change_detector=mock_change_detector,
        state_manager=mock_state_manager,
        data_preservation_manager=mock_data_preservation_manager,
    )

    return migration


class TestDataPreservationWorkflow:
    """Test the complete data preservation workflow."""

    def test_run_with_data_preservation_no_entity_type(
        self, migration_with_preservation
    ):
        """Test data preservation workflow without entity type falls back to standard run."""
        result = migration_with_preservation.run_with_data_preservation()

        assert result.success is True
        assert result.message == "Test migration completed"
        assert migration_with_preservation.run_called is True
        assert (
            not migration_with_preservation.state_manager.start_migration_record.called
        )

    def test_run_with_data_preservation_successful_workflow(
        self, migration_with_preservation
    ):
        """Test complete successful data preservation workflow."""
        # Mock snapshot creation
        migration_with_preservation.create_snapshot = Mock(
            return_value=Path("test-snapshot.json")
        )

        result = migration_with_preservation.run_with_data_preservation(
            entity_type="issues",
            operation_type="migrate",
            entity_count=2,
            analyze_conflicts=True,
            create_backups=True,
        )

        # Verify the workflow executed successfully
        assert result.success is True
        assert result.message == "Test migration completed"
        assert migration_with_preservation.run_called is True

        # Verify conflict analysis was performed
        migration_with_preservation.data_preservation_manager.analyze_preservation_status.assert_called_once()

        # Verify state management workflow
        migration_with_preservation.state_manager.start_migration_record.assert_called_once()
        migration_with_preservation.state_manager.complete_migration_record.assert_called_once()
        migration_with_preservation.state_manager.create_state_snapshot.assert_called_once()
        migration_with_preservation.state_manager.save_current_state.assert_called_once()

        # Verify original states were stored (using correct method name)
        store_calls = (
            migration_with_preservation.data_preservation_manager.store_original_state.call_args_list
        )
        assert len(store_calls) == 2  # One for each entity

        # Verify result details include preservation info
        assert result.details["data_preservation"] is True
        assert result.details["state_management"] is True
        assert "conflict_report" in result.details
        assert "migration_record_id" in result.details
        assert "state_snapshot_id" in result.details

    def test_run_with_data_preservation_skip_migration_no_changes(
        self, migration_with_preservation
    ):
        """Test skipping migration when no changes are detected."""
        # Mock no changes detected
        migration_with_preservation.change_detector.detect_changes.return_value = (
            ChangeReport(
                has_changes=False,
                changes={"created": 0, "updated": 0, "deleted": 0},
                priority_score=0,
                change_summary="No changes detected",
                total_entities=2,
                entity_type="issues",
                total_changes=0,
            )
        )

        result = migration_with_preservation.run_with_data_preservation(
            entity_type="issues", analyze_conflicts=True
        )

        # Verify migration was skipped
        assert result.success is True
        assert "No changes detected" in result.message
        assert migration_with_preservation.run_called is False
        assert result.details["migration_skipped"] is True
        assert result.details["data_preservation"] is True

        # Verify conflict analysis still occurred
        migration_with_preservation.data_preservation_manager.analyze_preservation_status.assert_called_once()

    def test_run_with_data_preservation_conflict_analysis_disabled(
        self, migration_with_preservation
    ):
        """Test workflow with conflict analysis disabled."""
        migration_with_preservation.create_snapshot = Mock(
            return_value=Path("test-snapshot.json")
        )

        result = migration_with_preservation.run_with_data_preservation(
            entity_type="issues", analyze_conflicts=False
        )

        assert result.success is True
        assert migration_with_preservation.run_called is True

        # Verify conflict analysis was not performed
        migration_with_preservation.data_preservation_manager.analyze_preservation_status.assert_not_called()

        # Verify result details show no conflict report
        assert result.details["conflict_report"] is None

    def test_run_with_data_preservation_migration_failure(
        self, migration_with_preservation
    ):
        """Test workflow when migration fails."""
        # Mock failed migration
        migration_with_preservation.run = Mock(
            return_value=ComponentResult(
                success=False,
                message="Migration failed",
                errors=["Test error"],
                success_count=0,
                failed_count=1,
                total_count=1,
            )
        )

        result = migration_with_preservation.run_with_data_preservation(
            entity_type="issues"
        )

        # Verify failure is properly handled
        assert result.success is False
        assert result.message == "Migration failed"

        # Verify migration record was completed with error
        migration_with_preservation.state_manager.complete_migration_record.assert_called_once()
        complete_call = (
            migration_with_preservation.state_manager.complete_migration_record.call_args
        )
        assert complete_call[1]["error_count"] == 1
        assert complete_call[1]["errors"] == ["Test error"]

        # Verify no snapshot creation for failed migration
        migration_with_preservation.state_manager.create_state_snapshot.assert_not_called()
