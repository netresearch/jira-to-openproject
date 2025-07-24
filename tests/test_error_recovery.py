#!/usr/bin/env python3
"""Tests for the error recovery system."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, UTC

from src.utils.error_recovery import (
    ErrorRecoverySystem,
    error_recovery,
    MigrationCheckpoint,
    CircuitBreakerError,
)


class TestErrorRecoverySystem:
    """Test cases for ErrorRecoverySystem."""

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path for testing."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)
        yield db_path
        db_path.unlink(missing_ok=True)

    @pytest.fixture
    def error_recovery_system(self, temp_db_path):
        """Create an ErrorRecoverySystem instance for testing."""
        return ErrorRecoverySystem(db_path=str(temp_db_path))

    def test_initialization(self, error_recovery_system, temp_db_path):
        """Test ErrorRecoverySystem initialization."""
        assert error_recovery_system.checkpoint_manager.db_path == temp_db_path
        assert error_recovery_system.circuit_breaker_manager is not None
        assert error_recovery_system.retry_manager is not None

    def test_create_checkpoint(self, error_recovery_system):
        """Test checkpoint creation."""
        migration_id = "test_migration"
        checkpoint_type = "issue"
        entity_id = "ISSUE-123"
        data = {"test": "data"}
        
        error_recovery_system.checkpoint_manager.create_checkpoint(
            migration_id, checkpoint_type, entity_id, data
        )
        
        # Verify checkpoint was created
        checkpoints = error_recovery_system.checkpoint_manager.get_pending_checkpoints(
            migration_id, checkpoint_type
        )
        assert len(checkpoints) == 1
        assert checkpoints[0]['entity_id'] == entity_id

    def test_get_migration_status(self, error_recovery_system):
        """Test getting migration status."""
        migration_id = "test_migration"
        
        # Create some checkpoints first
        error_recovery_system.checkpoint_manager.create_checkpoint(
            migration_id, "issue", "ISSUE-1", {"data": "test1"}
        )
        error_recovery_system.checkpoint_manager.create_checkpoint(
            migration_id, "issue", "ISSUE-2", {"data": "test2"}
        )
        
        # Update one to completed
        error_recovery_system.checkpoint_manager.update_checkpoint(
            migration_id, "ISSUE-1", "completed"
        )
        
        status = error_recovery_system.get_migration_status(migration_id)
        
        assert status['total'] == 2
        assert status['completed'] == 1
        assert status['pending'] == 1

    def test_resume_migration(self, error_recovery_system):
        """Test migration resume functionality."""
        migration_id = "test_migration"
        
        # Create checkpoints with different statuses
        error_recovery_system.checkpoint_manager.create_checkpoint(
            migration_id, "issue", "ISSUE-1", {"data": "test1"}
        )
        error_recovery_system.checkpoint_manager.create_checkpoint(
            migration_id, "issue", "ISSUE-2", {"data": "test2"}
        )
        
        # Update one to completed
        error_recovery_system.checkpoint_manager.update_checkpoint(
            migration_id, "ISSUE-1", "completed"
        )
        
        # Resume should return pending entities
        resume_list = error_recovery_system.resume_migration(migration_id, "issue")
        
        assert len(resume_list) == 1
        assert resume_list[0]['entity_id'] == "ISSUE-2"

    def test_circuit_breaker_functionality(self, error_recovery_system):
        """Test circuit breaker pattern."""
        # Mock a function that fails
        failing_function = Mock(side_effect=Exception("Test error"))
        failing_function.__name__ = "test_function"  # Add __name__ attribute
        
        # First few calls should fail with the original exception
        # The circuit breaker opens after 4 failures (fail_max=5 means 5th failure opens it)
        for _ in range(4):  # Up to failure threshold
            with pytest.raises(Exception, match="Test error"):
                error_recovery_system.circuit_breaker_manager.call_with_breaker(
                    "test_service", failing_function
                )
        
        # The 5th call should trigger circuit breaker
        with pytest.raises(CircuitBreakerError):
            error_recovery_system.circuit_breaker_manager.call_with_breaker(
                "test_service", failing_function
            )

    def test_retry_logic(self, error_recovery_system):
        """Test retry logic with exponential backoff."""
        call_count = 0
        
        def failing_then_succeeding_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Simulated error")
            return "success"
        
        # Apply retry decorator
        retry_decorator = error_recovery_system.retry_manager.retry_with_backoff(
            retry_exceptions=(ValueError,)
        )
        decorated_function = retry_decorator(failing_then_succeeding_function)
        
        # Should succeed after retries
        result = decorated_function()
        assert result == "success"
        assert call_count == 3

    def test_execute_with_recovery(self, error_recovery_system):
        """Test the main execute_with_recovery method."""
        migration_id = "test_migration"
        checkpoint_type = "issue"
        entity_id = "ISSUE-123"
        
        def successful_function():
            return "success"
        
        result = error_recovery_system.execute_with_recovery(
            migration_id, checkpoint_type, entity_id, successful_function
        )
        
        assert result == "success"
        
        # Check that checkpoint was created and updated
        status = error_recovery_system.get_migration_status(migration_id)
        assert status['total'] == 1
        assert status['completed'] == 1

    def test_execute_with_recovery_failure(self, error_recovery_system):
        """Test execute_with_recovery with a failing function."""
        migration_id = "test_migration"
        checkpoint_type = "issue"
        entity_id = "ISSUE-123"
        
        def failing_function():
            raise ValueError("Test error")
        
        with pytest.raises(ValueError):
            error_recovery_system.execute_with_recovery(
                migration_id, checkpoint_type, entity_id, failing_function
            )
        
        # Check that checkpoint was created and marked as failed
        status = error_recovery_system.get_migration_status(migration_id)
        assert status['total'] == 1
        assert status['failed'] == 1

    def test_clear_migration_data(self, error_recovery_system):
        """Test clearing migration data."""
        migration_id = "test_migration"
        
        # Create some checkpoints
        error_recovery_system.checkpoint_manager.create_checkpoint(
            migration_id, "issue", "ISSUE-1", {"data": "test1"}
        )
        
        # Clear the data
        error_recovery_system.clear_migration_data(migration_id)
        
        # Verify data is cleared
        status = error_recovery_system.get_migration_status(migration_id)
        assert status['total'] == 0


class TestErrorRecoveryDecorator:
    """Test cases for the error_recovery decorator."""

    def test_successful_execution(self):
        """Test successful execution with decorator."""
        # Use the retry manager's decorator instead
        retry_decorator = error_recovery.retry_manager.retry_with_backoff()
        
        @retry_decorator
        def successful_function():
            return "success"
        
        result = successful_function()
        assert result == "success"

    def test_retry_on_failure(self):
        """Test retry behavior on failure."""
        call_count = 0
        
        retry_decorator = error_recovery.retry_manager.retry_with_backoff(
            retry_exceptions=(ValueError,)
        )
        
        @retry_decorator
        def failing_then_succeeding_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Simulated error")
            return "success"
        
        result = failing_then_succeeding_function()
        assert result == "success"
        assert call_count == 3

    def test_max_retries_exceeded(self):
        """Test behavior when max retries are exceeded."""
        retry_decorator = error_recovery.retry_manager.retry_with_backoff(
            retry_exceptions=(ValueError,)
        )
        
        @retry_decorator
        def always_failing_function():
            raise ValueError("Always fails")
        
        # When retries are exhausted, tenacity raises a RetryError
        from tenacity import RetryError
        with pytest.raises(RetryError):
            always_failing_function()


class TestMigrationCheckpoint:
    """Test cases for MigrationCheckpoint model."""

    def test_checkpoint_creation(self):
        """Test MigrationCheckpoint model creation."""
        checkpoint = MigrationCheckpoint(
            migration_id="test_migration",
            checkpoint_type="issue",
            entity_id="ISSUE-123",
            status="pending",
            data='{"test": "data"}',
            retry_count=0
        )
        
        assert checkpoint.migration_id == "test_migration"
        assert checkpoint.checkpoint_type == "issue"
        assert checkpoint.entity_id == "ISSUE-123"
        assert checkpoint.status == "pending"
        assert checkpoint.data == '{"test": "data"}'
        assert checkpoint.retry_count == 0 