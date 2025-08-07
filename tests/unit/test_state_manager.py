#!/usr/bin/env python3
"""Unit tests for StateManager functionality."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.utils.state_manager import StateManager


@pytest.fixture
def temp_state_dir():
    """Provide a temporary directory for state files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def state_manager(temp_state_dir):
    """Provide a StateManager instance with temporary directory."""
    return StateManager(state_dir=temp_state_dir)


class TestStateManagerBasics:
    """Test basic StateManager functionality."""

    def test_initialization(self, temp_state_dir) -> None:
        """Test StateManager initialization."""
        state_manager = StateManager(state_dir=temp_state_dir)

        # Check directory structure
        assert (temp_state_dir / "mappings").exists()
        assert (temp_state_dir / "history").exists()
        assert (temp_state_dir / "snapshots").exists()
        assert (temp_state_dir / "current").exists()

        # Check state manager properties
        assert state_manager.state_dir == temp_state_dir
        assert isinstance(state_manager._current_mappings, dict)
        assert isinstance(state_manager._current_records, list)

    def test_register_entity_mapping(self, state_manager) -> None:
        """Test registering entity mappings."""
        mapping_id = state_manager.register_entity_mapping(
            jira_entity_type="project",
            jira_entity_id="PROJ123",
            openproject_entity_type="project",
            openproject_entity_id="456",
            migration_component="ProjectMigration",
            metadata={"test": "data"},
        )

        # Verify mapping ID is returned
        assert isinstance(mapping_id, str)
        assert len(mapping_id) == 32  # UUID hex length

        # Verify mapping is stored
        assert mapping_id in state_manager._current_mappings
        mapping = state_manager._current_mappings[mapping_id]

        assert mapping["jira_entity_type"] == "project"
        assert mapping["jira_entity_id"] == "PROJ123"
        assert mapping["openproject_entity_type"] == "project"
        assert mapping["openproject_entity_id"] == "456"
        assert mapping["mapped_by"] == "ProjectMigration"
        assert mapping["metadata"] == {"test": "data"}
        assert "mapped_at" in mapping

    def test_get_entity_mapping(self, state_manager) -> None:
        """Test retrieving entity mappings."""
        # Register a mapping first
        state_manager.register_entity_mapping(
            jira_entity_type="issue",
            jira_entity_id="ISSUE-123",
            openproject_entity_type="work_package",
            openproject_entity_id="789",
            migration_component="WorkPackageMigration",
        )

        # Retrieve the mapping
        retrieved = state_manager.get_entity_mapping("issue", "ISSUE-123")

        assert retrieved is not None
        assert retrieved["jira_entity_id"] == "ISSUE-123"
        assert retrieved["openproject_entity_id"] == "789"

    def test_get_entity_mapping_not_found(self, state_manager) -> None:
        """Test retrieving non-existent entity mapping."""
        result = state_manager.get_entity_mapping("project", "NONEXISTENT")
        assert result is None

    def test_start_migration_record(self, state_manager) -> None:
        """Test starting a migration record."""
        record_id = state_manager.start_migration_record(
            migration_component="TestMigration",
            entity_type="projects",
            operation_type="create",
            entity_count=10,
            user="test_user",
        )

        # Verify record ID is returned
        assert isinstance(record_id, str)
        assert len(record_id) == 32  # UUID hex length

        # Verify record is stored
        record = state_manager._find_record(record_id)
        assert record is not None
        assert record["migration_component"] == "TestMigration"
        assert record["entity_type"] == "projects"
        assert record["status"] == "started"
        assert record["user"] == "test_user"

    def test_complete_migration_record(self, state_manager) -> None:
        """Test completing a migration record."""
        # Start a record
        record_id = state_manager.start_migration_record(
            migration_component="TestMigration",
            entity_type="projects",
            operation_type="create",
            entity_count=10,
        )

        # Complete the record
        state_manager.complete_migration_record(
            record_id=record_id,
            success_count=8,
            error_count=2,
        )

        # Verify record is updated
        record = state_manager._find_record(record_id)
        assert record["status"] == "failed"  # Because error_count > 0
        assert record["success_count"] == 8
        assert record["error_count"] == 2

    def test_create_state_snapshot(self, state_manager, temp_state_dir) -> None:
        """Test creating a state snapshot."""
        # Add some data first
        state_manager.register_entity_mapping(
            jira_entity_type="project",
            jira_entity_id="PROJ1",
            openproject_entity_type="project",
            openproject_entity_id="123",
            migration_component="ProjectMigration",
        )

        # Create snapshot
        snapshot_id = state_manager.create_state_snapshot(
            description="Test snapshot",
            user="test_user",
        )

        # Verify snapshot ID is returned
        assert isinstance(snapshot_id, str)
        assert len(snapshot_id) == 32

        # Verify snapshot file is created
        snapshot_file = temp_state_dir / "snapshots" / f"{snapshot_id}.json"
        assert snapshot_file.exists()

    def test_save_and_load_state(self, temp_state_dir) -> None:
        """Test saving and loading current state."""
        # Create state manager and add some data
        state_manager = StateManager(state_dir=temp_state_dir)

        state_manager.register_entity_mapping(
            jira_entity_type="project",
            jira_entity_id="PROJ1",
            openproject_entity_type="project",
            openproject_entity_id="123",
            migration_component="ProjectMigration",
        )

        # Save current state
        state_manager.save_current_state()

        # Verify files are created
        assert (temp_state_dir / "current" / "mappings.json").exists()
        assert (temp_state_dir / "current" / "records.json").exists()
        assert (temp_state_dir / "current" / "version.json").exists()

        # Create new state manager instance to test loading
        new_state_manager = StateManager(state_dir=temp_state_dir)

        # Verify data is loaded
        assert len(new_state_manager._current_mappings) == 1

    def test_get_mapping_statistics(self, state_manager) -> None:
        """Test getting mapping statistics."""
        # Add various mappings
        state_manager.register_entity_mapping(
            jira_entity_type="project",
            jira_entity_id="PROJ1",
            openproject_entity_type="project",
            openproject_entity_id="123",
            migration_component="ProjectMigration",
        )

        state_manager.register_entity_mapping(
            jira_entity_type="issue",
            jira_entity_id="ISSUE-1",
            openproject_entity_type="work_package",
            openproject_entity_id="789",
            migration_component="WorkPackageMigration",
        )

        # Get statistics
        stats = state_manager.get_mapping_statistics()

        assert stats["total_mappings"] == 2
        assert stats["mappings_by_jira_type"]["project"] == 1
        assert stats["mappings_by_jira_type"]["issue"] == 1
        assert stats["mappings_by_component"]["ProjectMigration"] == 1
        assert stats["mappings_by_component"]["WorkPackageMigration"] == 1

    def test_cleanup_old_state(self, state_manager, temp_state_dir) -> None:
        """Test cleaning up old state files."""
        import time

        # Create some old snapshot files
        snapshots_dir = temp_state_dir / "snapshots"

        old_file = snapshots_dir / "old.json"
        new_file = snapshots_dir / "new.json"

        # Create files with content
        for file_path in [old_file, new_file]:
            with file_path.open("w") as f:
                json.dump({"test": "data"}, f)

        # Set file modification times
        old_time = time.time() - (40 * 24 * 60 * 60)  # 40 days ago
        new_time = time.time() - (10 * 24 * 60 * 60)  # 10 days ago

        os.utime(old_file, (old_time, old_time))
        os.utime(new_file, (new_time, new_time))

        # Clean up files older than 30 days
        deleted_count = state_manager.cleanup_old_state(keep_days=30)

        # Should delete 1 old file
        assert deleted_count == 1
        assert not old_file.exists()
        assert new_file.exists()  # Should still exist

    def test_error_handling(self, state_manager) -> None:
        """Test error handling in StateManager."""
        # Test completing non-existent migration record
        with patch.object(state_manager.logger, "warning") as mock_warning:
            state_manager.complete_migration_record(
                record_id="nonexistent",
                success_count=5,
                error_count=0,
            )
            mock_warning.assert_called_once()

        # Test loading corrupted state files
        with patch("builtins.open", side_effect=json.JSONDecodeError("msg", "doc", 0)):
            with patch.object(state_manager.logger, "warning"):
                state_manager._load_current_state()
                # Should not crash and should have empty state
                assert len(state_manager._current_mappings) == 0
