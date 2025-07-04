#!/usr/bin/env python3
"""Unit tests for BaseMigration integration with StateManager."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.migrations.base_migration import BaseMigration
from src.utils.change_detector import ChangeDetector
from src.utils.state_manager import StateManager


class MockMigration(BaseMigration):
    """Mock migration class for testing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.changes_detected = True
        self.migration_run = False
        self.mappings_registered = []

    async def check_for_changes(self):
        """Mock change detection."""
        return {"has_changes": self.changes_detected, "change_count": 5 if self.changes_detected else 0}

    async def run(self):
        """Mock migration run."""
        self.migration_run = True
        # Simulate registering entity mappings during migration
        for mapping in self.mappings_registered:
            self.register_entity_mapping(**mapping)
        return {
            "status": "success",
            "message": "Migration completed successfully",
            "migrated_count": 5
        }


@pytest.fixture
def temp_dirs():
    """Provide temporary directories for state and data."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        yield {
            "state_dir": temp_path / "state",
            "data_dir": temp_path / "data"
        }


@pytest.fixture
def mock_clients():
    """Provide mock clients."""
    return {
        "jira_client": AsyncMock(),
        "openproject_client": AsyncMock(),
        "logger": MagicMock()
    }


@pytest.fixture
def components(temp_dirs, mock_clients):
    """Provide test components."""
    state_manager = StateManager(state_dir=temp_dirs["state_dir"])
    change_detector = ChangeDetector(data_dir=temp_dirs["data_dir"])

    migration = MockMigration(
        jira_client=mock_clients["jira_client"],
        openproject_client=mock_clients["openproject_client"],
        logger=mock_clients["logger"],
        state_manager=state_manager,
        change_detector=change_detector
    )

    return {
        "migration": migration,
        "state_manager": state_manager,
        "change_detector": change_detector
    }


class TestStateManagerIntegration:
    """Test StateManager integration with BaseMigration."""

    def test_state_manager_dependency_injection(self, components):
        """Test that StateManager is properly injected into BaseMigration."""
        migration = components["migration"]
        state_manager = components["state_manager"]

        assert migration.state_manager is state_manager
        assert hasattr(migration, "register_entity_mapping")
        assert hasattr(migration, "get_entity_mapping")
        assert hasattr(migration, "start_migration_record")
        assert hasattr(migration, "complete_migration_record")
        assert hasattr(migration, "create_state_snapshot")

    def test_register_entity_mapping_wrapper(self, components):
        """Test the register_entity_mapping wrapper method."""
        migration = components["migration"]

        mapping_id = migration.register_entity_mapping(
            jira_entity_type="project",
            jira_entity_id="PROJ123",
            openproject_entity_type="project",
            openproject_entity_id="456",
            migration_component="TestMigration"
        )

        # Verify mapping was registered
        assert isinstance(mapping_id, str)

        # Verify it's accessible through state manager
        retrieved = migration.state_manager.get_entity_mapping("project", "PROJ123")
        assert retrieved is not None
        assert retrieved["openproject_entity_id"] == "456"

    def test_get_entity_mapping_wrapper(self, components):
        """Test the get_entity_mapping wrapper method."""
        migration = components["migration"]

        # Register a mapping first
        migration.register_entity_mapping(
            jira_entity_type="issue",
            jira_entity_id="ISSUE-123",
            openproject_entity_type="work_package",
            openproject_entity_id="789",
            migration_component="TestMigration"
        )

        # Retrieve through wrapper
        retrieved = migration.get_entity_mapping("issue", "ISSUE-123")
        assert retrieved is not None
        assert retrieved["openproject_entity_id"] == "789"

    def test_migration_record_wrapper_methods(self, components):
        """Test migration record wrapper methods."""
        migration = components["migration"]

        # Start migration record
        record_id = migration.start_migration_record(
            migration_component="TestMigration",
            entity_type="projects",
            operation_type="create",
            entity_count=10
        )

        assert isinstance(record_id, str)

        # Complete migration record
        migration.complete_migration_record(
            record_id=record_id,
            success_count=8,
            error_count=2
        )

        # Verify record exists and is updated
        record = migration.state_manager._find_record(record_id)
        assert record is not None
        assert record["status"] == "failed"  # Because error_count > 0
        assert record["success_count"] == 8

    def test_create_state_snapshot_wrapper(self, components):
        """Test create_state_snapshot wrapper method."""
        migration = components["migration"]

        # Add some data first
        migration.register_entity_mapping(
            jira_entity_type="project",
            jira_entity_id="PROJ1",
            openproject_entity_type="project",
            openproject_entity_id="123",
            migration_component="TestMigration"
        )

        # Create snapshot
        snapshot_id = migration.create_state_snapshot(
            description="Test snapshot",
            user="test_user"
        )

        assert isinstance(snapshot_id, str)

        # Verify snapshot file exists
        snapshot_file = migration.state_manager.state_dir / "snapshots" / f"{snapshot_id}.json"
        assert snapshot_file.exists()


class TestStateManagementWorkflow:
    """Test the complete state management workflow."""

    @pytest.mark.asyncio
    async def test_run_with_state_management_with_changes(self, components, temp_dirs):
        """Test run_with_state_management when changes are detected."""
        migration = components["migration"]
        state_manager = components["state_manager"]

        # Set up migration to detect changes
        migration.changes_detected = True
        migration.mappings_registered = [
            {
                "jira_entity_type": "project",
                "jira_entity_id": "PROJ1",
                "openproject_entity_type": "project",
                "openproject_entity_id": "123",
                "migration_component": "MockMigration"
            }
        ]

        # Run migration with state management
        result = await migration.run_with_state_management(
            description="Test migration run",
            user="test_user"
        )

        # Verify migration was executed
        assert migration.migration_run is True
        assert result["status"] == "success"
        assert result["migrated_count"] == 5

        # Verify state was saved
        state_files = [
            temp_dirs["state_dir"] / "current" / "mappings.json",
            temp_dirs["state_dir"] / "current" / "records.json",
            temp_dirs["state_dir"] / "current" / "version.json"
        ]
        for state_file in state_files:
            assert state_file.exists()

        # Verify entity mapping was registered
        mapping = state_manager.get_entity_mapping("project", "PROJ1")
        assert mapping is not None
        assert mapping["openproject_entity_id"] == "123"

        # Verify migration record was created and completed
        assert len(state_manager._current_records) == 1
        record = state_manager._current_records[0]
        assert record["status"] == "completed"
        assert record["success_count"] == 5

    @pytest.mark.asyncio
    async def test_run_with_state_management_no_changes(self, components):
        """Test run_with_state_management when no changes are detected."""
        migration = components["migration"]

        # Set up migration to detect no changes
        migration.changes_detected = False

        # Run migration with state management
        result = await migration.run_with_state_management(
            description="Test migration run",
            user="test_user"
        )

        # Verify migration was NOT executed
        assert migration.migration_run is False
        assert result["status"] == "no_changes"
        assert result["message"] == "No changes detected, skipping migration"

    @pytest.mark.asyncio
    async def test_run_with_state_management_error_handling(self, components):
        """Test error handling in run_with_state_management."""
        migration = components["migration"]

        # Set up migration to raise an error
        async def failing_run():
            raise ValueError("Test error")

        migration.run = failing_run
        migration.changes_detected = True

        # Run migration with state management
        result = await migration.run_with_state_management(
            description="Test migration run",
            user="test_user"
        )

        # Verify error was handled
        assert result["status"] == "error"
        assert "Test error" in result["message"]

        # Verify migration record shows failure
        assert len(migration.state_manager._current_records) == 1
        record = migration.state_manager._current_records[0]
        assert record["status"] == "failed"
        assert record["error_count"] == 1

    @pytest.mark.asyncio
    async def test_run_with_state_management_creates_snapshots(self, components, temp_dirs):
        """Test that run_with_state_management creates pre and post snapshots."""
        migration = components["migration"]

        # Set up migration to detect changes
        migration.changes_detected = True
        migration.mappings_registered = [
            {
                "jira_entity_type": "project",
                "jira_entity_id": "PROJ1",
                "openproject_entity_type": "project",
                "openproject_entity_id": "123",
                "migration_component": "MockMigration"
            }
        ]

        # Run migration with state management
        result = await migration.run_with_state_management(
            description="Test migration run",
            user="test_user",
            create_snapshots=True
        )

        # Verify snapshots were created
        snapshots_dir = temp_dirs["state_dir"] / "snapshots"
        snapshot_files = list(snapshots_dir.glob("*.json"))

        # Should have pre-migration and post-migration snapshots
        assert len(snapshot_files) >= 2

        # Verify result includes snapshot information
        assert result["status"] == "success"
        assert "pre_migration_snapshot" in result
        assert "post_migration_snapshot" in result

    @pytest.mark.asyncio
    async def test_run_with_state_management_without_change_detector(self, temp_dirs, mock_clients):
        """Test run_with_state_management when change_detector is None."""
        state_manager = StateManager(state_dir=temp_dirs["state_dir"])

        migration = MockMigration(
            jira_client=mock_clients["jira_client"],
            openproject_client=mock_clients["openproject_client"],
            logger=mock_clients["logger"],
            state_manager=state_manager,
            change_detector=None  # No change detector
        )

        migration.changes_detected = True

        # Run migration with state management
        result = await migration.run_with_state_management(
            description="Test migration run",
            user="test_user"
        )

        # Should run migration without checking for changes
        assert migration.migration_run is True
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_run_with_state_management_partial_failure(self, components):
        """Test run_with_state_management with partial migration failure."""
        migration = components["migration"]

        # Set up migration to return partial success
        async def partial_success_run():
            migration.migration_run = True
            return {
                "status": "partial_success",
                "message": "Some items failed",
                "migrated_count": 3,
                "failed_count": 2
            }

        migration.run = partial_success_run
        migration.changes_detected = True

        # Run migration with state management
        result = await migration.run_with_state_management(
            description="Test migration run",
            user="test_user"
        )

        # Verify partial success was handled
        assert result["status"] == "partial_success"
        assert result["migrated_count"] == 3
        assert result["failed_count"] == 2

        # Verify migration record reflects partial success
        assert len(migration.state_manager._current_records) == 1
        record = migration.state_manager._current_records[0]
        assert record["status"] == "failed"  # Because there were failures
        assert record["success_count"] == 3
        assert record["error_count"] == 2


class TestBackwardCompatibility:
    """Test backward compatibility with existing migration patterns."""

    @pytest.mark.asyncio
    async def test_run_method_still_works(self, components):
        """Test that the original run method still works."""
        migration = components["migration"]

        # Call original run method
        result = await migration.run()

        # Should work as before
        assert migration.migration_run is True
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_run_with_change_detection_still_works(self, components):
        """Test that run_with_change_detection still works."""
        migration = components["migration"]
        migration.changes_detected = True

        # Call run_with_change_detection
        result = await migration.run_with_change_detection()

        # Should work as before
        assert migration.migration_run is True
        assert result["status"] == "success"

    def test_migration_without_state_manager_works(self, temp_dirs, mock_clients):
        """Test that migrations work without StateManager (backward compatibility)."""
        # Create migration without state manager
        migration = MockMigration(
            jira_client=mock_clients["jira_client"],
            openproject_client=mock_clients["openproject_client"],
            logger=mock_clients["logger"]
            # No state_manager parameter
        )

        # Should initialize correctly
        assert migration.state_manager is None

        # Should not have state management wrapper methods
        assert not hasattr(migration, "register_entity_mapping")
        assert not hasattr(migration, "get_entity_mapping")
