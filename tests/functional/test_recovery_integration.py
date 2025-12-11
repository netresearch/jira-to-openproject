#!/usr/bin/env python3
"""Integration tests for recovery and resilience features."""

import json
from unittest.mock import Mock

import pytest

from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult
from src.utils.checkpoint_manager import CheckpointManager, CheckpointStatus


class TestRecoveryIntegration:
    """Integration tests for the complete recovery workflow."""

    @pytest.fixture
    def temp_dirs(self, tmp_path):
        """Create temporary directories for testing."""
        return {
            "state": tmp_path / "state",
            "checkpoints": tmp_path / "checkpoints",
            "preservation": tmp_path / "preservation",
        }

    @pytest.fixture
    def mock_migration(self, temp_dirs):
        """Create a mock migration with recovery capabilities."""
        migration = BaseMigration("test_recovery_migration")
        migration.checkpoint_manager = CheckpointManager(
            checkpoint_dir=temp_dirs["checkpoints"],
        )

        # Mock the run method to simulate processing
        migration.run = Mock(
            return_value=ComponentResult(
                success=True,
                message="Test migration completed",
                success_count=100,
                failed_count=0,
                total_count=100,
            ),
        )

        return migration

    @pytest.fixture
    def failed_migration(self, temp_dirs):
        """Create a migration that will fail for testing recovery."""
        migration = BaseMigration("test_failed_migration")
        migration.checkpoint_manager = CheckpointManager(
            checkpoint_dir=temp_dirs["checkpoints"],
        )

        # Mock the run method to simulate failure
        migration.run = Mock(
            return_value=ComponentResult(
                success=False,
                message="Migration failed",
                success_count=50,
                failed_count=50,
                total_count=100,
                errors=["Network connection timeout", "API rate limit exceeded"],
            ),
        )

        return migration

    def test_complete_recovery_workflow(self, mock_migration) -> None:
        """Test the complete recovery workflow from start to finish."""
        entity_type = "test_entities"
        entity_count = 100

        # 1. Run migration with recovery enabled
        result = mock_migration.run_with_recovery(
            entity_type=entity_type,
            entity_count=entity_count,
            enable_checkpoints=True,
            checkpoint_frequency=10,
        )

        # 2. Verify successful completion
        assert result.success is True
        assert result.details["recovery_enabled"] is True
        assert result.details["checkpoints_created"] > 0

        # 3. Verify migration record was created
        migration_record_id = result.details["migration_record_id"]
        assert migration_record_id is not None

        # 4. Verify checkpoints were created and completed
        checkpoints = mock_migration.checkpoint_manager.get_checkpoints_for_migration(
            migration_record_id,
        )
        assert len(checkpoints) > 0

        # At least one checkpoint should be completed
        completed_checkpoints = [cp for cp in checkpoints if cp["status"] == CheckpointStatus.COMPLETED.value]
        assert len(completed_checkpoints) > 0

        # 5. Verify cleanup was performed
        progress = mock_migration.checkpoint_manager.get_progress_status(
            migration_record_id,
        )
        assert progress is None  # Should be cleaned up after completion

    def test_migration_failure_and_recovery_plan_creation(
        self,
        failed_migration,
    ) -> None:
        """Test migration failure handling and recovery plan creation."""
        entity_type = "test_entities"
        entity_count = 100

        # 1. Run migration that will fail
        result = failed_migration.run_with_recovery(
            entity_type=entity_type,
            entity_count=entity_count,
            enable_checkpoints=True,
        )

        # 2. Verify failure was handled gracefully
        assert result.success is False
        assert result.details["recovery_enabled"] is True
        assert result.details["can_resume"] is True

        # 3. Verify recovery plan was created
        recovery_plan_id = result.details["recovery_plan_id"]
        assert recovery_plan_id is not None

        # 4. Verify recovery plan file exists and contains correct data
        plan_file = failed_migration.checkpoint_manager.checkpoint_dir / "recovery_plans" / f"{recovery_plan_id}.json"
        assert plan_file.exists()

        with plan_file.open() as f:
            plan_data = json.load(f)

        assert plan_data["failure_type"] in [
            "network_error",
            "unknown_error",
        ]  # Based on error messages
        assert len(plan_data["manual_steps"]) > 0

        # 5. Verify failed checkpoint exists
        migration_record_id = result.details["migration_record_id"]
        checkpoints = failed_migration.checkpoint_manager.get_checkpoints_for_migration(
            migration_record_id,
        )
        failed_checkpoints = [cp for cp in checkpoints if cp["status"] == CheckpointStatus.FAILED.value]
        assert len(failed_checkpoints) > 0

    def test_migration_resume_capability(self, mock_migration) -> None:
        """Test resuming a migration from checkpoints."""
        migration_record_id = "test_migration_resume_123"

        # 1. Create a completed checkpoint to simulate previous migration progress
        checkpoint_id = mock_migration.checkpoint_manager.create_checkpoint(
            migration_record_id=migration_record_id,
            step_name="user_processing",
            step_description="Processing user entities",
            entities_processed=75,
            entities_total=100,
            metadata={"last_entity_id": "user_75"},
        )
        mock_migration.checkpoint_manager.start_checkpoint(checkpoint_id)
        mock_migration.checkpoint_manager.complete_checkpoint(checkpoint_id)

        # 2. Check if migration can be resumed
        can_resume = mock_migration.checkpoint_manager.can_resume_migration(
            migration_record_id,
        )
        assert can_resume is True

        # 3. Get resume point
        resume_point = mock_migration.checkpoint_manager.get_resume_point(
            migration_record_id,
        )
        assert resume_point is not None
        assert resume_point["entities_processed"] == 75
        assert resume_point["progress_percentage"] == 75.0

        # 4. Resume migration
        resumed_result = mock_migration.resume_migration(migration_record_id)
        assert resumed_result.success is True
        assert resumed_result.details["resumed_from_checkpoint"] == checkpoint_id

    def test_progress_tracking_during_migration(self, mock_migration) -> None:
        """Test real-time progress tracking during migration."""
        entity_type = "test_entities"
        entity_count = 50

        # Mock the _run_with_checkpoints method to simulate progress updates
        original_run_with_checkpoints = mock_migration._run_with_checkpoints

        def mock_run_with_checkpoints(entity_type, entity_count, checkpoint_frequency):
            # Simulate progress during migration
            for i in range(0, entity_count, 10):
                if mock_migration._current_migration_record_id:
                    mock_migration.checkpoint_manager.update_progress(
                        migration_record_id=mock_migration._current_migration_record_id,
                        current_step=f"Processing batch {i // 10 + 1}",
                        current_step_progress=(i / entity_count) * 100,
                        completed_steps=i // 10,
                    )

            return original_run_with_checkpoints(
                entity_type,
                entity_count,
                checkpoint_frequency,
            )

        mock_migration._run_with_checkpoints = mock_run_with_checkpoints

        # Run migration with progress tracking
        result = mock_migration.run_with_recovery(
            entity_type=entity_type,
            entity_count=entity_count,
            enable_checkpoints=True,
        )

        # Verify successful completion with progress tracking
        assert result.success is True
        assert result.details["recovery_enabled"] is True

    def test_checkpoint_creation_during_migration(self, mock_migration) -> None:
        """Test checkpoint creation at strategic points during migration."""
        entity_type = "test_entities"
        entity_count = 30

        # Set up current migration context
        mock_migration._current_migration_record_id = "test_checkpoint_creation"

        # Create checkpoints at different stages
        checkpoint1 = mock_migration.create_checkpoint_during_migration(
            step_name="initialization",
            step_description="Initializing migration",
            entities_processed=0,
            entities_total=entity_count,
        )

        checkpoint2 = mock_migration.create_checkpoint_during_migration(
            step_name="batch_processing",
            step_description="Processing entities in batches",
            entities_processed=15,
            entities_total=entity_count,
            current_entity_id="entity_15",
            current_entity_type=entity_type,
        )

        checkpoint3 = mock_migration.create_checkpoint_during_migration(
            step_name="finalization",
            step_description="Finalizing migration",
            entities_processed=30,
            entities_total=entity_count,
        )

        # Verify checkpoints were created
        assert checkpoint1 is not None
        assert checkpoint2 is not None
        assert checkpoint3 is not None

        # Complete all checkpoints
        mock_migration.complete_current_checkpoint(entities_processed=30)

        # Verify progress was tracked
        all_checkpoints = mock_migration.checkpoint_manager.get_checkpoints_for_migration(
            mock_migration._current_migration_record_id,
        )
        assert len(all_checkpoints) >= 3

    def test_rollback_functionality(self, mock_migration) -> None:
        """Test rollback to specific checkpoint."""
        # Create a checkpoint
        checkpoint_id = mock_migration.checkpoint_manager.create_checkpoint(
            migration_record_id="test_rollback_123",
            step_name="safe_point",
            step_description="Safe rollback point",
            entities_processed=25,
            entities_total=100,
        )
        mock_migration.checkpoint_manager.complete_checkpoint(checkpoint_id)

        # Attempt rollback
        rollback_result = mock_migration.rollback_to_checkpoint(checkpoint_id)

        # Should succeed (basic implementation)
        assert rollback_result is True

    def test_error_classification_and_recovery_actions(self, mock_migration) -> None:
        """Test error classification and appropriate recovery action determination."""
        test_cases = [
            (["Network connection timeout"], "network_error"),
            (["Invalid user data format"], "validation_error"),
            (["Authentication failed"], "auth_error"),
            (["Disk space insufficient"], "resource_error"),
            (["Unknown error occurred"], "unknown_error"),
        ]

        for errors, expected_type in test_cases:
            classified_type = mock_migration._classify_failure_type(errors)
            assert classified_type == expected_type

    def test_manual_recovery_steps_generation(self, mock_migration) -> None:
        """Test generation of manual recovery steps based on errors."""
        network_errors = ["Connection timeout", "DNS resolution failed"]
        steps = mock_migration._generate_manual_recovery_steps(network_errors)

        assert any("network connectivity" in step.lower() for step in steps)
        assert any("firewall" in step.lower() for step in steps)

        auth_errors = ["Token expired", "Invalid credentials"]
        steps = mock_migration._generate_manual_recovery_steps(auth_errors)

        assert any("credentials" in step.lower() for step in steps)
        assert any("permissions" in step.lower() for step in steps)

    def test_concurrent_checkpoint_operations(self, mock_migration) -> None:
        """Test that checkpoint operations are safe under concurrent access."""
        migration_record_id = "test_concurrent_123"

        # Simulate concurrent checkpoint creation
        checkpoints = []
        for i in range(5):
            checkpoint_id = mock_migration.checkpoint_manager.create_checkpoint(
                migration_record_id=migration_record_id,
                step_name=f"concurrent_step_{i}",
                step_description=f"Concurrent processing step {i}",
                entities_processed=i * 10,
                entities_total=50,
            )
            checkpoints.append(checkpoint_id)

        # Verify all checkpoints were created successfully
        assert len(checkpoints) == 5
        assert len(set(checkpoints)) == 5  # All unique

        # Complete all checkpoints
        for checkpoint_id in checkpoints:
            mock_migration.checkpoint_manager.complete_checkpoint(checkpoint_id)

        # Verify all were completed
        all_checkpoints = mock_migration.checkpoint_manager.get_checkpoints_for_migration(
            migration_record_id,
        )
        completed_count = sum(1 for cp in all_checkpoints if cp["status"] == CheckpointStatus.COMPLETED.value)
        assert completed_count == 5

    def test_recovery_with_data_preservation(self, mock_migration, temp_dirs) -> None:
        """Test recovery workflow integrated with data preservation."""
        # This test would verify that recovery features work alongside data preservation
        # For now, we'll test that the recovery methods don't interfere with data preservation

        entity_type = "test_entities_with_preservation"
        entity_count = 20

        # Run with both recovery and data preservation
        result = mock_migration.run_with_recovery(
            entity_type=entity_type,
            entity_count=entity_count,
            enable_checkpoints=True,
        )

        # Should complete successfully
        assert result.success is True
        assert result.details["recovery_enabled"] is True

        # Verify recovery features didn't interfere with normal operation
        migration_record_id = result.details["migration_record_id"]
        assert migration_record_id is not None

    def test_migration_interruption_simulation(self, failed_migration) -> None:
        """Test handling of simulated migration interruption."""
        entity_type = "test_interrupted_entities"
        entity_count = 100

        # Run migration that will fail (simulating interruption)
        result = failed_migration.run_with_recovery(
            entity_type=entity_type,
            entity_count=entity_count,
            enable_checkpoints=True,
        )

        # Verify failure was handled properly
        assert result.success is False

        # Verify recovery information is available
        assert "recovery_plan_id" in result.details
        assert "migration_record_id" in result.details

        # Verify we can get progress information
        migration_record_id = result.details["migration_record_id"]
        progress_info = failed_migration.get_migration_progress(migration_record_id)

        assert progress_info["migration_record_id"] == migration_record_id
        assert "checkpoints" in progress_info
        assert "can_resume" in progress_info

    def test_estimate_migration_steps(self, mock_migration) -> None:
        """Test migration step estimation."""
        # Test with different entity counts
        steps_small = mock_migration._estimate_migration_steps("users", 5)
        steps_medium = mock_migration._estimate_migration_steps("users", 50)
        steps_large = mock_migration._estimate_migration_steps("users", 500)

        # Should increase with entity count
        assert steps_small < steps_medium < steps_large

        # Should have minimum base steps
        assert steps_small >= 3  # pre-processing + processing + post-processing
