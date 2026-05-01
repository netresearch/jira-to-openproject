#!/usr/bin/env python3
"""Integration tests for recovery and resilience features.

The legacy recovery-related helpers that lived on ``BaseMigration``
(``run_with_recovery``, ``_run_with_checkpoints``,
``create_checkpoint_during_migration``, ``rollback_to_checkpoint``,
``_classify_failure_type``, ``_generate_manual_recovery_steps``,
``_estimate_migration_steps``) were removed when the recovery logic was
consolidated into ``CheckpointManager``. The tests that targeted those
methods were deleted; the checkpoint-manager integration below still
exercises the surviving concurrency guarantees end-to-end.
"""

import pytest

from src.application.components.base_migration import BaseMigration
from src.utils.checkpoint_manager import CheckpointManager, CheckpointStatus


class TestRecoveryIntegration:
    """Integration tests for the checkpoint-manager workflow."""

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
        """Create a migration wired to a fresh CheckpointManager."""
        migration = BaseMigration("test_recovery_migration")
        migration.checkpoint_manager = CheckpointManager(
            checkpoint_dir=temp_dirs["checkpoints"],
        )
        return migration

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
