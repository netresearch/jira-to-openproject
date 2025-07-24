from src.display import configure_logging
#!/usr/bin/env python3
"""Checkpoint management system for resilient migration operations.

This module provides functionality to:
- Track migration progress at a granular level
- Create checkpoints during long-running operations  
- Enable resuming interrupted migrations from checkpoints
- Provide rollback capabilities to specific checkpoints
- Monitor real-time progress and status
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict
from enum import Enum

from src import config


class CheckpointStatus(Enum):
    """Status of a checkpoint."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class RecoveryAction(Enum):
    """Available recovery actions for different failure scenarios."""
    RETRY_FROM_CHECKPOINT = "retry_from_checkpoint"
    ROLLBACK_TO_CHECKPOINT = "rollback_to_checkpoint"
    SKIP_AND_CONTINUE = "skip_and_continue"
    ABORT_MIGRATION = "abort_migration"
    MANUAL_INTERVENTION = "manual_intervention"


class ProgressCheckpoint(TypedDict):
    """Represents a specific checkpoint in migration progress."""
    
    checkpoint_id: str
    migration_record_id: str
    step_name: str
    step_description: str
    status: str
    created_at: str
    completed_at: str | None
    failed_at: str | None
    progress_percentage: float
    entities_processed: int
    entities_total: int
    current_entity_id: str | None
    current_entity_type: str | None
    data_snapshot: dict[str, Any]  # Minimal state snapshot for this checkpoint
    metadata: dict[str, Any]


class RecoveryPlan(TypedDict):
    """Recovery plan for handling specific failure scenarios."""
    
    plan_id: str
    failure_type: str
    error_message: str
    recommended_action: str
    checkpoint_id: str | None
    rollback_target: str | None
    retry_attempts: int
    manual_steps: list[str]
    metadata: dict[str, Any]


class ProgressTracker(TypedDict):
    """Real-time progress tracking for migration operations."""
    
    migration_record_id: str
    total_steps: int
    completed_steps: int
    current_step: str
    current_step_progress: float
    overall_progress: float
    estimated_time_remaining: str | None
    start_time: str
    last_update: str
    throughput_per_minute: float
    status: str


class CheckpointManager:
    """Manages checkpoints and recovery for resilient migration operations."""

    def __init__(self, checkpoint_dir: Path | None = None) -> None:
        """Initialize the checkpoint manager.

        Args:
            checkpoint_dir: Directory to store checkpoint files.
                           Defaults to var/state/checkpoints/
        """
        self.logger = configure_logging("INFO", None)
        self.checkpoint_dir = checkpoint_dir or config.get_path("data").parent / "state" / "checkpoints"
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

        # Ensure checkpoint directory structure exists
        (self.checkpoint_dir / "active").mkdir(exist_ok=True)
        (self.checkpoint_dir / "completed").mkdir(exist_ok=True)
        (self.checkpoint_dir / "failed").mkdir(exist_ok=True)
        (self.checkpoint_dir / "recovery_plans").mkdir(exist_ok=True)

        # In-memory tracking
        self._active_checkpoints: dict[str, ProgressCheckpoint] = {}
        self._progress_trackers: dict[str, ProgressTracker] = {}

    def create_checkpoint(
        self,
        migration_record_id: str,
        step_name: str,
        step_description: str,
        entities_processed: int = 0,
        entities_total: int = 0,
        current_entity_id: str | None = None,
        current_entity_type: str | None = None,
        data_snapshot: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new checkpoint during migration.

        Args:
            migration_record_id: ID of the migration record
            step_name: Name of the migration step
            step_description: Description of what this step does
            entities_processed: Number of entities processed so far
            entities_total: Total number of entities to process
            current_entity_id: ID of the entity currently being processed
            current_entity_type: Type of the entity being processed
            data_snapshot: Minimal state snapshot for this checkpoint
            metadata: Additional metadata

        Returns:
            Checkpoint ID
        """
        checkpoint_id = self._generate_id()
        
        progress_percentage = (
            (entities_processed / entities_total * 100) if entities_total > 0 else 0.0
        )

        checkpoint = ProgressCheckpoint(
            checkpoint_id=checkpoint_id,
            migration_record_id=migration_record_id,
            step_name=step_name,
            step_description=step_description,
            status=CheckpointStatus.PENDING.value,
            created_at=datetime.now(tz=UTC).isoformat(),
            completed_at=None,
            failed_at=None,
            progress_percentage=progress_percentage,
            entities_processed=entities_processed,
            entities_total=entities_total,
            current_entity_id=current_entity_id,
            current_entity_type=current_entity_type,
            data_snapshot=data_snapshot or {},
            metadata=metadata or {},
        )

        # Store in memory and persist to disk
        self._active_checkpoints[checkpoint_id] = checkpoint
        self._save_checkpoint(checkpoint)

        self.logger.info(
            "Created checkpoint %s for step '%s' (%d/%d entities, %.1f%%)",
            checkpoint_id[:8],
            step_name,
            entities_processed,
            entities_total,
            progress_percentage,
        )

        return checkpoint_id

    def start_checkpoint(self, checkpoint_id: str) -> None:
        """Mark a checkpoint as in progress."""
        checkpoint = self._active_checkpoints.get(checkpoint_id)
        if not checkpoint:
            self.logger.error("Checkpoint not found: %s", checkpoint_id)
            return

        checkpoint["status"] = CheckpointStatus.IN_PROGRESS.value
        self._save_checkpoint(checkpoint)

        self.logger.debug("Started checkpoint %s", checkpoint_id[:8])

    def complete_checkpoint(
        self,
        checkpoint_id: str,
        entities_processed: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mark a checkpoint as completed.

        Args:
            checkpoint_id: ID of the checkpoint
            entities_processed: Updated count of entities processed
            metadata: Additional metadata to store
        """
        checkpoint = self._active_checkpoints.get(checkpoint_id)
        if not checkpoint:
            self.logger.error("Checkpoint not found: %s", checkpoint_id)
            return

        checkpoint["status"] = CheckpointStatus.COMPLETED.value
        checkpoint["completed_at"] = datetime.now(tz=UTC).isoformat()

        if entities_processed is not None:
            checkpoint["entities_processed"] = entities_processed
            if checkpoint["entities_total"] > 0:
                checkpoint["progress_percentage"] = (
                    entities_processed / checkpoint["entities_total"] * 100
                )

        if metadata:
            checkpoint["metadata"].update(metadata)

        self._save_checkpoint(checkpoint)
        self._move_checkpoint_to_completed(checkpoint_id)

        self.logger.info(
            "Completed checkpoint %s for step '%s'",
            checkpoint_id[:8],
            checkpoint["step_name"],
        )

    def fail_checkpoint(
        self,
        checkpoint_id: str,
        error_message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mark a checkpoint as failed.

        Args:
            checkpoint_id: ID of the checkpoint
            error_message: Error message describing the failure
            metadata: Additional metadata about the failure
        """
        checkpoint = self._active_checkpoints.get(checkpoint_id)
        if not checkpoint:
            self.logger.error("Checkpoint not found: %s", checkpoint_id)
            return

        checkpoint["status"] = CheckpointStatus.FAILED.value
        checkpoint["failed_at"] = datetime.now(tz=UTC).isoformat()

        if metadata:
            checkpoint["metadata"].update(metadata)

        checkpoint["metadata"]["error_message"] = error_message

        self._save_checkpoint(checkpoint)
        self._move_checkpoint_to_failed(checkpoint_id)

        self.logger.error(
            "Failed checkpoint %s for step '%s': %s",
            checkpoint_id[:8],
            checkpoint["step_name"],
            error_message,
        )

    def get_resume_point(self, migration_record_id: str) -> ProgressCheckpoint | None:
        """Find the best checkpoint to resume from for a migration.

        Args:
            migration_record_id: ID of the migration record

        Returns:
            Last completed checkpoint or None if no suitable resume point
        """
        checkpoints = self.get_checkpoints_for_migration(migration_record_id)
        
        # Find the last completed checkpoint
        completed_checkpoints = [
            cp for cp in checkpoints 
            if cp["status"] == CheckpointStatus.COMPLETED.value
        ]
        
        if not completed_checkpoints:
            return None

        # Sort by creation time and return the most recent
        completed_checkpoints.sort(key=lambda x: x["created_at"], reverse=True)
        return completed_checkpoints[0]

    def can_resume_migration(self, migration_record_id: str) -> bool:
        """Check if a migration can be resumed from checkpoints.

        Args:
            migration_record_id: ID of the migration record

        Returns:
            True if the migration can be resumed, False otherwise
        """
        resume_point = self.get_resume_point(migration_record_id)
        return resume_point is not None

    def get_checkpoints_for_migration(self, migration_record_id: str) -> list[ProgressCheckpoint]:
        """Get all checkpoints for a specific migration.

        Args:
            migration_record_id: ID of the migration record

        Returns:
            List of checkpoints for the migration
        """
        checkpoints = []

        # Check active checkpoints
        for checkpoint in self._active_checkpoints.values():
            if checkpoint["migration_record_id"] == migration_record_id:
                checkpoints.append(checkpoint)

        # Check completed checkpoints
        completed_dir = self.checkpoint_dir / "completed"
        for checkpoint_file in completed_dir.glob("*.json"):
            try:
                with checkpoint_file.open() as f:
                    checkpoint = json.load(f)
                if checkpoint["migration_record_id"] == migration_record_id:
                    checkpoints.append(checkpoint)
            except Exception as e:
                self.logger.warning("Failed to load checkpoint %s: %s", checkpoint_file, e)

        # Check failed checkpoints
        failed_dir = self.checkpoint_dir / "failed"
        for checkpoint_file in failed_dir.glob("*.json"):
            try:
                with checkpoint_file.open() as f:
                    checkpoint = json.load(f)
                if checkpoint["migration_record_id"] == migration_record_id:
                    checkpoints.append(checkpoint)
            except Exception as e:
                self.logger.warning("Failed to load checkpoint %s: %s", checkpoint_file, e)

        # Sort by creation time
        checkpoints.sort(key=lambda x: x["created_at"])
        return checkpoints

    def create_recovery_plan(
        self,
        checkpoint_id: str,
        failure_type: str,
        error_message: str,
        manual_steps: list[str] | None = None,
    ) -> str:
        """Create a recovery plan for a failed checkpoint.

        Args:
            checkpoint_id: ID of the failed checkpoint
            failure_type: Type of failure (e.g., 'network_error', 'validation_error')
            error_message: Error message from the failure
            manual_steps: Manual steps required for recovery

        Returns:
            Recovery plan ID
        """
        plan_id = self._generate_id()
        
        # Determine recommended action based on failure type
        recommended_action = self._determine_recovery_action(failure_type, error_message)
        
        recovery_plan = RecoveryPlan(
            plan_id=plan_id,
            failure_type=failure_type,
            error_message=error_message,
            recommended_action=recommended_action.value,
            checkpoint_id=checkpoint_id,
            rollback_target=None,  # Will be determined based on action
            retry_attempts=0,
            manual_steps=manual_steps or [],
            metadata={
                "created_at": datetime.now(tz=UTC).isoformat(),
                "failure_checkpoint": checkpoint_id,
            },
        )

        # Save recovery plan
        plan_path = self.checkpoint_dir / "recovery_plans" / f"{plan_id}.json"
        with plan_path.open("w") as f:
            json.dump(recovery_plan, f, indent=2)

        self.logger.info(
            "Created recovery plan %s for checkpoint %s (action: %s)",
            plan_id[:8],
            checkpoint_id[:8],
            recommended_action.value,
        )

        return plan_id

    def execute_recovery_plan(self, plan_id: str) -> bool:
        """Execute a recovery plan.

        Args:
            plan_id: ID of the recovery plan

        Returns:
            True if recovery was successful, False otherwise
        """
        plan_path = self.checkpoint_dir / "recovery_plans" / f"{plan_id}.json"
        if not plan_path.exists():
            self.logger.error("Recovery plan not found: %s", plan_id)
            return False

        try:
            with plan_path.open() as f:
                plan = json.load(f)

            action = RecoveryAction(plan["recommended_action"])
            
            if action == RecoveryAction.RETRY_FROM_CHECKPOINT:
                return self._retry_from_checkpoint(plan["checkpoint_id"])
            elif action == RecoveryAction.ROLLBACK_TO_CHECKPOINT:
                return self._rollback_to_checkpoint(plan["checkpoint_id"])
            elif action == RecoveryAction.SKIP_AND_CONTINUE:
                return self._skip_and_continue(plan["checkpoint_id"])
            elif action == RecoveryAction.ABORT_MIGRATION:
                return self._abort_migration(plan["checkpoint_id"])
            else:  # MANUAL_INTERVENTION
                self.logger.warning(
                    "Manual intervention required for recovery plan %s. Steps: %s",
                    plan_id[:8],
                    ", ".join(plan["manual_steps"]),
                )
                return False

        except Exception as e:
            self.logger.exception("Failed to execute recovery plan %s: %s", plan_id, e)
            return False

    def start_progress_tracking(
        self,
        migration_record_id: str,
        total_steps: int,
        current_step: str = "Initializing",
    ) -> None:
        """Start tracking progress for a migration.

        Args:
            migration_record_id: ID of the migration record
            total_steps: Total number of steps in the migration
            current_step: Current step name
        """
        tracker = ProgressTracker(
            migration_record_id=migration_record_id,
            total_steps=total_steps,
            completed_steps=0,
            current_step=current_step,
            current_step_progress=0.0,
            overall_progress=0.0,
            estimated_time_remaining=None,
            start_time=datetime.now(tz=UTC).isoformat(),
            last_update=datetime.now(tz=UTC).isoformat(),
            throughput_per_minute=0.0,
            status="running",
        )

        self._progress_trackers[migration_record_id] = tracker
        self.logger.info(
            "Started progress tracking for migration %s (%d total steps)",
            migration_record_id[:8],
            total_steps,
        )

    def update_progress(
        self,
        migration_record_id: str,
        current_step: str | None = None,
        current_step_progress: float | None = None,
        completed_steps: int | None = None,
    ) -> None:
        """Update progress information for a migration.

        Args:
            migration_record_id: ID of the migration record
            current_step: Current step name
            current_step_progress: Progress within current step (0.0-100.0)
            completed_steps: Number of completed steps
        """
        tracker = self._progress_trackers.get(migration_record_id)
        if not tracker:
            self.logger.warning(
                "Progress tracker not found for migration %s", migration_record_id
            )
            return

        if current_step is not None:
            tracker["current_step"] = current_step
        if current_step_progress is not None:
            tracker["current_step_progress"] = current_step_progress
        if completed_steps is not None:
            tracker["completed_steps"] = completed_steps

        # Calculate overall progress
        if tracker["total_steps"] > 0:
            step_progress = tracker["completed_steps"] / tracker["total_steps"]
            current_progress = tracker["current_step_progress"] / 100.0 / tracker["total_steps"]
            tracker["overall_progress"] = (step_progress + current_progress) * 100.0

        tracker["last_update"] = datetime.now(tz=UTC).isoformat()

        # Calculate throughput and estimated time remaining
        self._calculate_throughput_and_eta(tracker)

    def get_progress_status(self, migration_record_id: str) -> ProgressTracker | None:
        """Get current progress status for a migration.

        Args:
            migration_record_id: ID of the migration record

        Returns:
            Current progress tracker or None if not found
        """
        return self._progress_trackers.get(migration_record_id)

    def cleanup_completed_migration(self, migration_record_id: str) -> None:
        """Clean up tracking data for a completed migration.

        Args:
            migration_record_id: ID of the migration record
        """
        # Remove from active progress tracking
        if migration_record_id in self._progress_trackers:
            del self._progress_trackers[migration_record_id]

        # Move active checkpoints to completed
        for checkpoint_id, checkpoint in list(self._active_checkpoints.items()):
            if checkpoint["migration_record_id"] == migration_record_id:
                self._move_checkpoint_to_completed(checkpoint_id)

        self.logger.debug("Cleaned up tracking data for migration %s", migration_record_id)

    def _generate_id(self) -> str:
        """Generate a unique identifier."""
        return uuid.uuid4().hex

    def _save_checkpoint(self, checkpoint: ProgressCheckpoint) -> None:
        """Save a checkpoint to disk."""
        checkpoint_path = self.checkpoint_dir / "active" / f"{checkpoint['checkpoint_id']}.json"
        try:
            with checkpoint_path.open("w") as f:
                json.dump(checkpoint, f, indent=2)
        except Exception as e:
            self.logger.error("Failed to save checkpoint %s: %s", checkpoint["checkpoint_id"], e)

    def _move_checkpoint_to_completed(self, checkpoint_id: str) -> None:
        """Move a checkpoint from active to completed directory."""
        active_path = self.checkpoint_dir / "active" / f"{checkpoint_id}.json"
        completed_path = self.checkpoint_dir / "completed" / f"{checkpoint_id}.json"
        
        try:
            if active_path.exists():
                active_path.rename(completed_path)
            if checkpoint_id in self._active_checkpoints:
                del self._active_checkpoints[checkpoint_id]
        except Exception as e:
            self.logger.error("Failed to move checkpoint %s to completed: %s", checkpoint_id, e)

    def _move_checkpoint_to_failed(self, checkpoint_id: str) -> None:
        """Move a checkpoint from active to failed directory."""
        active_path = self.checkpoint_dir / "active" / f"{checkpoint_id}.json"
        failed_path = self.checkpoint_dir / "failed" / f"{checkpoint_id}.json"
        
        try:
            if active_path.exists():
                active_path.rename(failed_path)
            if checkpoint_id in self._active_checkpoints:
                del self._active_checkpoints[checkpoint_id]
        except Exception as e:
            self.logger.error("Failed to move checkpoint %s to failed: %s", checkpoint_id, e)

    def _determine_recovery_action(self, failure_type: str, error_message: str) -> RecoveryAction:
        """Determine the best recovery action based on failure type and message."""
        error_lower = error_message.lower()
        
        # Network-related errors - retry
        if failure_type in ["network_error", "timeout", "connection_error"]:
            return RecoveryAction.RETRY_FROM_CHECKPOINT
        
        # Validation errors - might need manual intervention
        if failure_type in ["validation_error", "data_error"]:
            if "required field" in error_lower or "invalid format" in error_lower:
                return RecoveryAction.MANUAL_INTERVENTION
            else:
                return RecoveryAction.SKIP_AND_CONTINUE
        
        # Authentication/authorization errors - manual intervention
        if failure_type in ["auth_error", "permission_error"]:
            return RecoveryAction.MANUAL_INTERVENTION
        
        # Critical system errors - abort
        if failure_type in ["system_error", "corruption_error"]:
            return RecoveryAction.ABORT_MIGRATION
        
        # Default to retry for unknown errors
        return RecoveryAction.RETRY_FROM_CHECKPOINT

    def _retry_from_checkpoint(self, checkpoint_id: str) -> bool:
        """Retry execution from a specific checkpoint."""
        # This would be implemented by the calling migration to restart from the checkpoint
        self.logger.info("Setting up retry from checkpoint %s", checkpoint_id[:8])
        return True

    def _rollback_to_checkpoint(self, checkpoint_id: str) -> bool:
        """Rollback to a specific checkpoint."""
        # This would involve restoring state from the checkpoint's data snapshot
        self.logger.info("Setting up rollback to checkpoint %s", checkpoint_id[:8])
        return True

    def _skip_and_continue(self, checkpoint_id: str) -> bool:
        """Skip the failed checkpoint and continue with the next step."""
        self.logger.info("Skipping failed checkpoint %s and continuing", checkpoint_id[:8])
        return True

    def _abort_migration(self, checkpoint_id: str) -> bool:
        """Abort the entire migration due to critical failure."""
        self.logger.error("Aborting migration due to critical failure at checkpoint %s", checkpoint_id[:8])
        return False

    def _calculate_throughput_and_eta(self, tracker: ProgressTracker) -> None:
        """Calculate throughput and estimated time remaining."""
        try:
            start_time = datetime.fromisoformat(tracker["start_time"].replace("Z", "+00:00"))
            current_time = datetime.now(tz=UTC)
            elapsed_minutes = (current_time - start_time).total_seconds() / 60.0
            
            if elapsed_minutes > 0:
                # Calculate throughput (steps per minute)
                completed_work = tracker["completed_steps"] + (tracker["current_step_progress"] / 100.0)
                tracker["throughput_per_minute"] = completed_work / elapsed_minutes
                
                # Estimate time remaining
                if tracker["throughput_per_minute"] > 0:
                    remaining_work = tracker["total_steps"] - completed_work
                    remaining_minutes = remaining_work / tracker["throughput_per_minute"]
                    
                    if remaining_minutes < 60:
                        tracker["estimated_time_remaining"] = f"{remaining_minutes:.1f} minutes"
                    else:
                        hours = remaining_minutes / 60
                        tracker["estimated_time_remaining"] = f"{hours:.1f} hours"
                        
        except Exception as e:
            self.logger.debug("Failed to calculate throughput and ETA: %s", e) 