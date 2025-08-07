#!/usr/bin/env python3
"""Unit tests for CheckpointManager recovery and resilience features."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from src.utils.checkpoint_manager import (
    CheckpointManager,
    CheckpointStatus,
    RecoveryAction,
)


class TestCheckpointManager:
    """Test cases for CheckpointManager functionality."""

    @pytest.fixture
    def checkpoint_manager(self, tmp_path):
        """Create CheckpointManager instance with temporary directory."""
        return CheckpointManager(checkpoint_dir=tmp_path / "checkpoints")

    @pytest.fixture
    def sample_checkpoint_data(self):
        """Sample checkpoint data for testing."""
        return {
            "migration_record_id": "test_migration_123",
            "step_name": "user_processing",
            "step_description": "Processing user entities",
            "entities_processed": 50,
            "entities_total": 100,
            "current_entity_id": "user_50",
            "current_entity_type": "users",
            "metadata": {"batch_number": 5},
        }

    def test_checkpoint_manager_initialization(self, tmp_path) -> None:
        """Test CheckpointManager initialization creates proper directory structure."""
        checkpoint_dir = tmp_path / "test_checkpoints"
        manager = CheckpointManager(checkpoint_dir=checkpoint_dir)

        # Verify directory structure
        assert checkpoint_dir.exists()
        assert (checkpoint_dir / "active").exists()
        assert (checkpoint_dir / "completed").exists()
        assert (checkpoint_dir / "failed").exists()
        assert (checkpoint_dir / "recovery_plans").exists()

        # Verify empty tracking
        assert manager._active_checkpoints == {}
        assert manager._progress_trackers == {}

    def test_create_checkpoint(
        self,
        checkpoint_manager,
        sample_checkpoint_data,
    ) -> None:
        """Test creating a new checkpoint."""
        checkpoint_id = checkpoint_manager.create_checkpoint(**sample_checkpoint_data)

        # Verify checkpoint was created
        assert checkpoint_id is not None
        assert len(checkpoint_id) == 32  # UUID hex length

        # Verify checkpoint is in active tracking
        assert checkpoint_id in checkpoint_manager._active_checkpoints

        # Verify checkpoint data
        checkpoint = checkpoint_manager._active_checkpoints[checkpoint_id]
        assert checkpoint["migration_record_id"] == "test_migration_123"
        assert checkpoint["step_name"] == "user_processing"
        assert checkpoint["entities_processed"] == 50
        assert checkpoint["entities_total"] == 100
        assert checkpoint["progress_percentage"] == 50.0
        assert checkpoint["status"] == CheckpointStatus.PENDING.value

        # Verify checkpoint file was created
        checkpoint_file = (
            checkpoint_manager.checkpoint_dir / "active" / f"{checkpoint_id}.json"
        )
        assert checkpoint_file.exists()

    def test_start_checkpoint(self, checkpoint_manager, sample_checkpoint_data) -> None:
        """Test starting a checkpoint."""
        checkpoint_id = checkpoint_manager.create_checkpoint(**sample_checkpoint_data)
        checkpoint_manager.start_checkpoint(checkpoint_id)

        # Verify status was updated
        checkpoint = checkpoint_manager._active_checkpoints[checkpoint_id]
        assert checkpoint["status"] == CheckpointStatus.IN_PROGRESS.value

    def test_complete_checkpoint(
        self,
        checkpoint_manager,
        sample_checkpoint_data,
    ) -> None:
        """Test completing a checkpoint."""
        checkpoint_id = checkpoint_manager.create_checkpoint(**sample_checkpoint_data)
        checkpoint_manager.start_checkpoint(checkpoint_id)

        # Complete with updated entity count
        checkpoint_manager.complete_checkpoint(
            checkpoint_id,
            entities_processed=75,
            metadata={"completion_note": "batch completed"},
        )

        # Verify checkpoint was moved to completed
        assert checkpoint_id not in checkpoint_manager._active_checkpoints

        # Verify completed file exists
        completed_file = (
            checkpoint_manager.checkpoint_dir / "completed" / f"{checkpoint_id}.json"
        )
        assert completed_file.exists()

        # Verify completion data
        with completed_file.open() as f:
            completed_data = json.load(f)
        assert completed_data["status"] == CheckpointStatus.COMPLETED.value
        assert completed_data["entities_processed"] == 75
        assert completed_data["progress_percentage"] == 75.0
        assert completed_data["completed_at"] is not None
        assert completed_data["metadata"]["completion_note"] == "batch completed"

    def test_fail_checkpoint(self, checkpoint_manager, sample_checkpoint_data) -> None:
        """Test failing a checkpoint."""
        checkpoint_id = checkpoint_manager.create_checkpoint(**sample_checkpoint_data)
        checkpoint_manager.start_checkpoint(checkpoint_id)

        error_message = "Network timeout during user processing"
        checkpoint_manager.fail_checkpoint(
            checkpoint_id,
            error_message,
            metadata={"retry_count": 3},
        )

        # Verify checkpoint was moved to failed
        assert checkpoint_id not in checkpoint_manager._active_checkpoints

        # Verify failed file exists
        failed_file = (
            checkpoint_manager.checkpoint_dir / "failed" / f"{checkpoint_id}.json"
        )
        assert failed_file.exists()

        # Verify failure data
        with failed_file.open() as f:
            failed_data = json.load(f)
        assert failed_data["status"] == CheckpointStatus.FAILED.value
        assert failed_data["failed_at"] is not None
        assert failed_data["metadata"]["error_message"] == error_message
        assert failed_data["metadata"]["retry_count"] == 3

    def test_get_resume_point(self, checkpoint_manager, sample_checkpoint_data) -> None:
        """Test finding resume point for migration."""
        migration_id = "test_migration_456"

        # Create multiple checkpoints for the migration
        checkpoint1_id = checkpoint_manager.create_checkpoint(
            migration_record_id=migration_id,
            step_name="step1",
            step_description="First step",
            entities_processed=25,
            entities_total=100,
        )
        checkpoint_manager.start_checkpoint(checkpoint1_id)
        checkpoint_manager.complete_checkpoint(checkpoint1_id)

        checkpoint2_id = checkpoint_manager.create_checkpoint(
            migration_record_id=migration_id,
            step_name="step2",
            step_description="Second step",
            entities_processed=50,
            entities_total=100,
        )
        checkpoint_manager.start_checkpoint(checkpoint2_id)
        checkpoint_manager.complete_checkpoint(checkpoint2_id)

        # Get resume point
        resume_point = checkpoint_manager.get_resume_point(migration_id)

        # Should return the most recent completed checkpoint
        assert resume_point is not None
        assert resume_point["checkpoint_id"] == checkpoint2_id
        assert resume_point["step_name"] == "step2"
        assert resume_point["entities_processed"] == 50

    def test_can_resume_migration(self, checkpoint_manager) -> None:
        """Test checking if migration can be resumed."""
        migration_id = "test_migration_789"

        # No checkpoints - cannot resume
        assert not checkpoint_manager.can_resume_migration(migration_id)

        # Create and complete a checkpoint
        checkpoint_id = checkpoint_manager.create_checkpoint(
            migration_record_id=migration_id,
            step_name="processing",
            step_description="Processing entities",
            entities_processed=30,
            entities_total=100,
        )
        checkpoint_manager.start_checkpoint(checkpoint_id)
        checkpoint_manager.complete_checkpoint(checkpoint_id)

        # Now can resume
        assert checkpoint_manager.can_resume_migration(migration_id)

    def test_get_checkpoints_for_migration(self, checkpoint_manager) -> None:
        """Test retrieving all checkpoints for a migration."""
        migration_id = "test_migration_abc"

        # Create checkpoints in different states
        checkpoint1_id = checkpoint_manager.create_checkpoint(
            migration_record_id=migration_id,
            step_name="step1",
            step_description="First step",
            entities_processed=20,
            entities_total=100,
        )
        checkpoint_manager.start_checkpoint(checkpoint1_id)
        checkpoint_manager.complete_checkpoint(checkpoint1_id)

        checkpoint2_id = checkpoint_manager.create_checkpoint(
            migration_record_id=migration_id,
            step_name="step2",
            step_description="Second step",
            entities_processed=40,
            entities_total=100,
        )
        checkpoint_manager.start_checkpoint(checkpoint2_id)
        checkpoint_manager.fail_checkpoint(checkpoint2_id, "Test failure")

        # Get all checkpoints
        checkpoints = checkpoint_manager.get_checkpoints_for_migration(migration_id)

        # Should return both checkpoints, sorted by creation time
        assert len(checkpoints) == 2
        assert checkpoints[0]["step_name"] == "step1"
        assert checkpoints[0]["status"] == CheckpointStatus.COMPLETED.value
        assert checkpoints[1]["step_name"] == "step2"
        assert checkpoints[1]["status"] == CheckpointStatus.FAILED.value

    def test_create_recovery_plan(
        self,
        checkpoint_manager,
        sample_checkpoint_data,
    ) -> None:
        """Test creating a recovery plan for failed checkpoint."""
        # Create and fail a checkpoint
        checkpoint_id = checkpoint_manager.create_checkpoint(**sample_checkpoint_data)
        checkpoint_manager.start_checkpoint(checkpoint_id)
        checkpoint_manager.fail_checkpoint(checkpoint_id, "Network connection lost")

        # Create recovery plan
        plan_id = checkpoint_manager.create_recovery_plan(
            checkpoint_id=checkpoint_id,
            failure_type="network_error",
            error_message="Network connection lost",
            manual_steps=["Check network connectivity", "Verify VPN connection"],
        )

        # Verify recovery plan was created
        assert plan_id is not None
        plan_file = (
            checkpoint_manager.checkpoint_dir / "recovery_plans" / f"{plan_id}.json"
        )
        assert plan_file.exists()

        # Verify plan content
        with plan_file.open() as f:
            plan_data = json.load(f)
        assert plan_data["failure_type"] == "network_error"
        assert plan_data["error_message"] == "Network connection lost"
        assert (
            plan_data["recommended_action"]
            == RecoveryAction.RETRY_FROM_CHECKPOINT.value
        )
        assert plan_data["checkpoint_id"] == checkpoint_id
        assert "Check network connectivity" in plan_data["manual_steps"]

    def test_determine_recovery_action(self, checkpoint_manager) -> None:
        """Test recovery action determination based on failure types."""
        # Network errors -> retry
        action = checkpoint_manager._determine_recovery_action(
            "network_error",
            "Connection timeout",
        )
        assert action == RecoveryAction.RETRY_FROM_CHECKPOINT

        # Validation errors -> manual intervention or skip
        action = checkpoint_manager._determine_recovery_action(
            "validation_error",
            "Required field missing",
        )
        assert action == RecoveryAction.MANUAL_INTERVENTION

        action = checkpoint_manager._determine_recovery_action(
            "validation_error",
            "Invalid date format",
        )
        assert action == RecoveryAction.SKIP_AND_CONTINUE

        # Auth errors -> manual intervention
        action = checkpoint_manager._determine_recovery_action(
            "auth_error",
            "Token expired",
        )
        assert action == RecoveryAction.MANUAL_INTERVENTION

        # System errors -> abort
        action = checkpoint_manager._determine_recovery_action(
            "system_error",
            "Database corruption",
        )
        assert action == RecoveryAction.ABORT_MIGRATION

        # Unknown errors -> retry
        action = checkpoint_manager._determine_recovery_action(
            "unknown_error",
            "Something went wrong",
        )
        assert action == RecoveryAction.RETRY_FROM_CHECKPOINT

    def test_progress_tracking(self, checkpoint_manager) -> None:
        """Test progress tracking functionality."""
        migration_id = "test_migration_progress"
        total_steps = 5

        # Start progress tracking
        checkpoint_manager.start_progress_tracking(
            migration_id,
            total_steps,
            "Initializing",
        )

        # Verify progress tracker was created
        tracker = checkpoint_manager.get_progress_status(migration_id)
        assert tracker is not None
        assert tracker["migration_record_id"] == migration_id
        assert tracker["total_steps"] == total_steps
        assert tracker["completed_steps"] == 0
        assert tracker["current_step"] == "Initializing"
        assert tracker["overall_progress"] == 0.0
        assert tracker["status"] == "running"

        # Update progress
        checkpoint_manager.update_progress(
            migration_id,
            current_step="Processing users",
            current_step_progress=50.0,
            completed_steps=2,
        )

        # Verify updated progress
        tracker = checkpoint_manager.get_progress_status(migration_id)
        assert tracker["current_step"] == "Processing users"
        assert tracker["current_step_progress"] == 50.0
        assert tracker["completed_steps"] == 2
        assert tracker["overall_progress"] > 0  # Should be calculated

    def test_cleanup_completed_migration(
        self,
        checkpoint_manager,
        sample_checkpoint_data,
    ) -> None:
        """Test cleanup of completed migration data."""
        migration_id = "test_migration_cleanup"

        # Create progress tracker
        checkpoint_manager.start_progress_tracking(migration_id, 3)

        # Create active checkpoint
        checkpoint_id = checkpoint_manager.create_checkpoint(
            migration_record_id=migration_id,
            step_name="processing",
            step_description="Processing data",
            entities_processed=10,
            entities_total=20,
        )

        # Verify data exists
        assert migration_id in checkpoint_manager._progress_trackers
        assert checkpoint_id in checkpoint_manager._active_checkpoints

        # Cleanup
        checkpoint_manager.cleanup_completed_migration(migration_id)

        # Verify cleanup
        assert migration_id not in checkpoint_manager._progress_trackers
        assert checkpoint_id not in checkpoint_manager._active_checkpoints

        # Checkpoint should be moved to completed
        completed_file = (
            checkpoint_manager.checkpoint_dir / "completed" / f"{checkpoint_id}.json"
        )
        assert completed_file.exists()

    def test_throughput_calculation(self, checkpoint_manager) -> None:
        """Test throughput and ETA calculation."""
        migration_id = "test_migration_throughput"

        # Mock datetime to control time calculations
        with patch("src.utils.checkpoint_manager.datetime") as mock_datetime:
            # Set start time
            start_time = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)
            current_time = datetime(
                2024,
                1,
                1,
                10,
                10,
                0,
                tzinfo=UTC,
            )  # 10 minutes later

            mock_datetime.now.return_value = current_time
            mock_datetime.fromisoformat.return_value = start_time

            # Start tracking
            checkpoint_manager.start_progress_tracking(migration_id, 10)

            # Update with some progress
            checkpoint_manager.update_progress(
                migration_id,
                completed_steps=3,
                current_step_progress=50.0,
            )

            # Verify throughput calculation
            tracker = checkpoint_manager.get_progress_status(migration_id)
            # 3.5 steps completed in 10 minutes = 0.35 steps per minute
            assert tracker["throughput_per_minute"] == pytest.approx(0.35, rel=1e-2)
            assert "minutes" in tracker["estimated_time_remaining"]

    def test_error_handling_invalid_checkpoint(self, checkpoint_manager) -> None:
        """Test error handling for invalid checkpoint operations."""
        # Try to start non-existent checkpoint
        checkpoint_manager.start_checkpoint("invalid_checkpoint_id")
        # Should not raise exception, just log error

        # Try to complete non-existent checkpoint
        checkpoint_manager.complete_checkpoint("invalid_checkpoint_id")
        # Should not raise exception, just log error

        # Try to fail non-existent checkpoint
        checkpoint_manager.fail_checkpoint("invalid_checkpoint_id", "Test error")
        # Should not raise exception, just log error

    def test_checkpoint_persistence(
        self,
        checkpoint_manager,
        sample_checkpoint_data,
    ) -> None:
        """Test that checkpoints are properly persisted to disk."""
        checkpoint_id = checkpoint_manager.create_checkpoint(**sample_checkpoint_data)

        # Verify file was created with correct content
        checkpoint_file = (
            checkpoint_manager.checkpoint_dir / "active" / f"{checkpoint_id}.json"
        )
        assert checkpoint_file.exists()

        with checkpoint_file.open() as f:
            saved_data = json.load(f)

        assert saved_data["checkpoint_id"] == checkpoint_id
        assert (
            saved_data["migration_record_id"]
            == sample_checkpoint_data["migration_record_id"]
        )
        assert saved_data["step_name"] == sample_checkpoint_data["step_name"]
        assert (
            saved_data["entities_processed"]
            == sample_checkpoint_data["entities_processed"]
        )

    def test_recovery_plan_execution_mock(
        self,
        checkpoint_manager,
        sample_checkpoint_data,
    ) -> None:
        """Test recovery plan execution (mocked)."""
        # Create and fail a checkpoint
        checkpoint_id = checkpoint_manager.create_checkpoint(**sample_checkpoint_data)
        checkpoint_manager.fail_checkpoint(checkpoint_id, "Test failure")

        # Create recovery plan
        plan_id = checkpoint_manager.create_recovery_plan(
            checkpoint_id=checkpoint_id,
            failure_type="network_error",
            error_message="Test failure",
        )

        # Execute recovery plan
        result = checkpoint_manager.execute_recovery_plan(plan_id)

        # Should succeed (mocked implementation)
        assert result is True

    def test_manual_intervention_recovery(
        self,
        checkpoint_manager,
        sample_checkpoint_data,
    ) -> None:
        """Test recovery plan for manual intervention scenarios."""
        checkpoint_id = checkpoint_manager.create_checkpoint(**sample_checkpoint_data)
        checkpoint_manager.fail_checkpoint(checkpoint_id, "Data validation failed")

        plan_id = checkpoint_manager.create_recovery_plan(
            checkpoint_id=checkpoint_id,
            failure_type="validation_error",
            error_message="Required field missing",
        )

        # Load and verify plan
        plan_file = (
            checkpoint_manager.checkpoint_dir / "recovery_plans" / f"{plan_id}.json"
        )
        with plan_file.open() as f:
            plan_data = json.load(f)

        assert (
            plan_data["recommended_action"] == RecoveryAction.MANUAL_INTERVENTION.value
        )
        assert len(plan_data["manual_steps"]) > 0
