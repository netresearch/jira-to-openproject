#!/usr/bin/env python3
"""Unit tests for BaseMigration integration with StateManager."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.migrations.base_migration import BaseMigration
from src.models.component_results import ComponentResult
from src.utils.change_detector import ChangeDetector
from src.utils.state_manager import StateManager


class MockBaseMigration(BaseMigration):
    """Test migration class for testing state management integration."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.test_entities = [
            {"id": "entity1", "name": "Test Entity 1"},
            {"id": "entity2", "name": "Test Entity 2"},
        ]
        self.run_called = False

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict]:
        """Return test entities for change detection."""
        return self.test_entities

    def run(self) -> ComponentResult:
        """Mock run method that simulates successful migration."""
        self.run_called = True
        return ComponentResult(
            success=True,
            message="Test migration completed",
            success_count=2,
            failed_count=0,
            total_count=2,
        )


class TestBaseMigrationStateIntegration:
    """Test StateManager integration with BaseMigration."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        # Create temporary directories
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.state_dir = self.temp_path / "state"
        self.data_dir = self.temp_path / "data"

        # Create mock clients
        self.jira_client = MagicMock()
        self.op_client = MagicMock()

        # Create real components
        self.state_manager = StateManager(state_dir=self.state_dir)
        self.change_detector = ChangeDetector(snapshot_dir=self.data_dir)

    def teardown_method(self) -> None:
        """Clean up test fixtures."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_state_manager_dependency_injection(self) -> None:
        """Test that StateManager is properly injected into BaseMigration."""
        migration = MockBaseMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
            state_manager=self.state_manager,
            change_detector=self.change_detector,
        )

        assert migration.state_manager is self.state_manager
        assert hasattr(migration, "register_entity_mapping")
        assert hasattr(migration, "get_entity_mapping")
        assert hasattr(migration, "start_migration_record")
        assert hasattr(migration, "complete_migration_record")

    def test_register_entity_mapping_wrapper(self) -> None:
        """Test the register_entity_mapping wrapper method."""
        migration = MockBaseMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
            state_manager=self.state_manager,
            change_detector=self.change_detector,
        )

        mapping_id = migration.register_entity_mapping(
            jira_entity_type="project",
            jira_entity_id="PROJ123",
            openproject_entity_type="project",
            openproject_entity_id="456",
        )

        # Verify mapping was registered
        assert isinstance(mapping_id, str)

        # Verify it's accessible through state manager
        retrieved = migration.get_entity_mapping("project", "PROJ123")
        assert retrieved is not None
        assert retrieved["openproject_entity_id"] == "456"
        assert retrieved["mapped_by"] == "MockBaseMigration"

    def test_migration_record_lifecycle(self) -> None:
        """Test migration record start and complete lifecycle."""
        migration = MockBaseMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
            state_manager=self.state_manager,
            change_detector=self.change_detector,
        )

        # Start migration record
        record_id = migration.start_migration_record(
            entity_type="projects",
            operation_type="migrate",
            entity_count=5,
        )

        assert isinstance(record_id, str)

        # Complete migration record
        migration.complete_migration_record(
            record_id=record_id,
            success_count=5,
            error_count=0,
        )

        # Verify record exists in state manager
        records = self.state_manager.get_migration_history()
        assert len(records) == 1
        assert records[0]["record_id"] == record_id
        assert records[0]["success_count"] == 5

    def test_run_with_state_management_with_changes(self) -> None:
        """Test state management workflow when changes are detected."""
        migration = MockBaseMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
            state_manager=self.state_manager,
            change_detector=self.change_detector,
        )

        # Mock change detection to detect changes
        with patch.object(migration, "should_skip_migration") as mock_skip:
            mock_skip.return_value = (
                False,
                {"total_changes": 2, "changes_by_type": {"new": 1, "modified": 1}},
            )

            # Run migration with state management
            result = migration.run_with_state_management(
                entity_type="test_entities",
                operation_type="migrate",
                entity_count=2,
            )

            # Verify migration was executed
            assert migration.run_called is True
            assert result.success is True
            assert "change_report" in result.details
            assert "migration_record_id" in result.details
            assert result.details["state_management"] is True

    def test_run_with_state_management_no_changes(self) -> None:
        """Test state management workflow when no changes are detected."""
        migration = MockBaseMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
            state_manager=self.state_manager,
            change_detector=self.change_detector,
        )

        # Mock change detection to detect no changes
        with patch.object(migration, "should_skip_migration") as mock_skip:
            mock_skip.return_value = (True, {"total_changes": 0, "changes_by_type": {}})

            # Run migration with state management
            result = migration.run_with_state_management(
                entity_type="test_entities",
                operation_type="migrate",
                entity_count=0,
            )

            # Verify migration was NOT executed
            assert migration.run_called is False
            assert result.success is True
            assert "migration_skipped" in result.details
            assert result.details["migration_skipped"] is True
            assert result.success_count == 0
