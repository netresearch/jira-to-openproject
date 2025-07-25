"""
State consistency tests for the migration tool.
Tests data integrity under concurrent access, partial failures, and state corruption scenarios.

Based on Zen TestGen analysis identifying critical gaps in:
- State consistency during partial failures
- Concurrent migration conflicts  
- Data corruption recovery
- Migration record vs snapshot consistency
"""

import pytest
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch, call
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from src.utils.state_manager import StateManager, StateCorruptionError, StateSnapshot
from src.migrations.base_migration import BaseMigration, MigrationError
from src.migration import Migration


class TestStateConsistencyUnderConcurrency:
    """Tests for state consistency when multiple operations occur simultaneously."""

    @pytest.fixture
    def state_manager(self):
        """Create StateManager instance with mocked storage."""
        with patch('src.utils.state_manager.storage') as mock_storage:
            state_mgr = StateManager()
            state_mgr._storage = mock_storage
            return state_mgr

    @pytest.fixture
    def concurrent_migrations(self, state_manager):
        """Set up multiple migration instances for concurrency testing."""
        migrations = []
        for i in range(3):
            migration = BaseMigration(f"test_migration_{i}")
            migration.state_manager = state_manager
            migrations.append(migration)
        return migrations

    def test_concurrent_state_writes_maintain_consistency(self, state_manager, concurrent_migrations):
        """
        CONCURRENCY TEST: Multiple migrations writing state simultaneously.
        Verifies that concurrent state updates don't corrupt each other.
        """
        # Arrange: Track state write operations
        write_operations = []
        original_save = state_manager.save_migration_state
        
        def tracked_save(migration_id, state_data):
            write_operations.append({
                'migration_id': migration_id,
                'timestamp': datetime.now(),
                'state_data': state_data,
                'thread_id': threading.get_ident()
            })
            # Simulate database write time
            time.sleep(0.01)
            return original_save(migration_id, state_data)
        
        state_manager.save_migration_state = tracked_save
        
        # Act: Run concurrent state writes
        def write_state(migration):
            for i in range(5):
                state_data = {
                    'step': i,
                    'progress': i * 20,
                    'status': 'running',
                    'timestamp': datetime.now().isoformat()
                }
                migration.save_current_state(state_data)
                time.sleep(0.005)  # Small delay between writes
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(write_state, migration) for migration in concurrent_migrations]
            for future in futures:
                future.result()
        
        # Assert: Verify state consistency
        assert len(write_operations) == 15  # 3 migrations × 5 writes each
        
        # Check that each migration's final state is correct
        for migration in concurrent_migrations:
            final_state = state_manager.get_migration_state(migration.migration_id)
            assert final_state['step'] == 4  # Last step
            assert final_state['progress'] == 80  # 4 * 20
            assert final_state['status'] == 'running'

    def test_migration_record_snapshot_consistency(self, state_manager):
        """
        CONSISTENCY TEST: Migration records must stay consistent with snapshots.
        Tests recovery when migration records exist but snapshots are corrupted.
        """
        migration_id = "test_migration_consistency"
        
        # Arrange: Create migration record
        migration_record = {
            'id': migration_id,
            'status': 'running',
            'progress': 50,
            'started_at': datetime.now().isoformat(),
            'checkpoints': ['users_migrated', 'projects_migrated']
        }
        state_manager.save_migration_record(migration_id, migration_record)
        
        # Simulate snapshot corruption
        state_manager._storage.get_snapshot.side_effect = StateCorruptionError(
            "Snapshot file corrupted: invalid JSON"
        )
        
        # Act & Assert: Should detect inconsistency and attempt recovery
        with pytest.raises(StateCorruptionError, match="Migration record exists but snapshot is corrupted"):
            state_manager.validate_state_consistency(migration_id)
        
        # Verify recovery procedures were triggered
        state_manager.attempt_state_recovery.assert_called_once_with(migration_id)
        state_manager.create_emergency_backup.assert_called_once()

    def test_partial_failure_state_preservation(self, state_manager):
        """
        CONSISTENCY TEST: State must be preserved during partial migration failures.
        Ensures that successful migration steps are not lost when later steps fail.
        """
        migration_id = "test_partial_failure"
        
        # Arrange: Migration progresses through several successful steps
        successful_steps = [
            {'step': 'users', 'status': 'completed', 'migrated_count': 150},
            {'step': 'projects', 'status': 'completed', 'migrated_count': 25},
            {'step': 'work_packages', 'status': 'in_progress', 'migrated_count': 500}
        ]
        
        for step_data in successful_steps:
            state_manager.save_step_state(migration_id, step_data)
        
        # Simulate failure during work packages migration
        state_manager.mark_step_failed(migration_id, 'work_packages', 
            error="Network timeout during batch 10")
        
        # Act: Retrieve state after failure
        migration_state = state_manager.get_migration_state(migration_id)
        
        # Assert: Successful steps should be preserved
        assert migration_state['steps']['users']['status'] == 'completed'
        assert migration_state['steps']['users']['migrated_count'] == 150
        assert migration_state['steps']['projects']['status'] == 'completed'
        assert migration_state['steps']['projects']['migrated_count'] == 25
        
        # Failed step should be marked correctly
        assert migration_state['steps']['work_packages']['status'] == 'failed'
        assert migration_state['steps']['work_packages']['migrated_count'] == 500  # Partial progress preserved
        assert 'Network timeout' in migration_state['steps']['work_packages']['error']

    def test_concurrent_migration_lock_contention(self, state_manager):
        """
        CONCURRENCY TEST: Multiple migrations attempting to acquire locks simultaneously.
        Tests that only one migration can run at a time and others wait appropriately.
        """
        migration_ids = ['migration_1', 'migration_2', 'migration_3']
        lock_acquisition_results = {}
        
        # Arrange: Mock lock acquisition with realistic timing
        def simulate_lock_acquisition(migration_id):
            try:
                # Attempt to acquire lock with timeout
                acquired = state_manager.acquire_migration_lock(migration_id, timeout=2.0)
                if acquired:
                    lock_acquisition_results[migration_id] = {
                        'status': 'acquired',
                        'timestamp': datetime.now()
                    }
                    time.sleep(0.5)  # Simulate migration work
                    state_manager.release_migration_lock(migration_id)
                    lock_acquisition_results[migration_id]['released'] = datetime.now()
                else:
                    lock_acquisition_results[migration_id] = {
                        'status': 'timeout',
                        'timestamp': datetime.now()
                    }
            except Exception as e:
                lock_acquisition_results[migration_id] = {
                    'status': 'error',
                    'error': str(e),
                    'timestamp': datetime.now()
                }
        
        # Act: Attempt concurrent lock acquisition
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(simulate_lock_acquisition, mid) for mid in migration_ids]
            for future in futures:
                future.result()
        
        # Assert: Only one migration should succeed, others should timeout or wait
        successful_acquisitions = [
            result for result in lock_acquisition_results.values() 
            if result['status'] == 'acquired'
        ]
        assert len(successful_acquisitions) == 1  # Only one should succeed
        
        # Others should timeout
        timeouts = [
            result for result in lock_acquisition_results.values()
            if result['status'] == 'timeout'
        ]
        assert len(timeouts) == 2

    def test_state_recovery_from_corrupted_snapshot(self, state_manager):
        """
        RECOVERY TEST: Recover migration state when snapshot file is corrupted.
        Tests fallback to migration records and reconstruction of state.
        """
        migration_id = "test_recovery"
        
        # Arrange: Create valid migration record
        migration_record = {
            'id': migration_id,
            'status': 'running',
            'progress': 75,
            'started_at': (datetime.now() - timedelta(hours=2)).isoformat(),
            'checkpoints': [
                {'step': 'users', 'completed_at': (datetime.now() - timedelta(hours=1)).isoformat()},
                {'step': 'projects', 'completed_at': (datetime.now() - timedelta(minutes=30)).isoformat()}
            ]
        }
        state_manager._storage.get_migration_record.return_value = migration_record
        
        # Simulate corrupted snapshot
        state_manager._storage.get_snapshot.side_effect = StateCorruptionError(
            "Snapshot file corrupted"
        )
        
        # Act: Attempt state recovery
        recovered_state = state_manager.recover_state_from_record(migration_id)
        
        # Assert: State should be reconstructed from migration record
        assert recovered_state['id'] == migration_id
        assert recovered_state['status'] == 'running'
        assert recovered_state['progress'] == 75
        assert len(recovered_state['completed_steps']) == 2
        assert 'users' in [step['step'] for step in recovered_state['completed_steps']]
        assert 'projects' in [step['step'] for step in recovered_state['completed_steps']]

    def test_atomic_state_transitions(self, state_manager):
        """
        ATOMICITY TEST: State transitions must be atomic to prevent inconsistent states.
        Tests that state updates either complete fully or not at all.
        """
        migration_id = "test_atomic_transitions"
        
        # Arrange: Mock storage to simulate failure during multi-step update
        update_calls = []
        original_update = state_manager._storage.update
        
        def failing_update(*args, **kwargs):
            update_calls.append((args, kwargs))
            if len(update_calls) == 2:  # Fail on second update
                raise Exception("Database connection lost")
            return original_update(*args, **kwargs)
        
        state_manager._storage.update = failing_update
        
        # Act: Attempt complex state transition that requires multiple updates
        complex_state_update = {
            'migration_record': {'status': 'completed', 'progress': 100},
            'snapshot': {'final_state': True, 'completion_time': datetime.now().isoformat()},
            'cleanup_flags': {'temp_files_removed': True, 'locks_released': True}
        }
        
        # Assert: Should fail atomically
        with pytest.raises(Exception, match="Database connection lost"):
            state_manager.atomic_state_transition(migration_id, complex_state_update)
        
        # Verify rollback - no partial updates should remain
        current_state = state_manager.get_migration_state(migration_id)
        assert current_state.get('status') != 'completed'  # Should not be partially updated
        
        # Verify rollback was attempted
        state_manager._storage.rollback_transaction.assert_called_once()

    def test_concurrent_snapshot_creation(self, state_manager):
        """
        CONCURRENCY TEST: Multiple snapshot creations happening simultaneously.
        Tests that concurrent snapshots don't interfere with each other.
        """
        migration_ids = ['snap_migration_1', 'snap_migration_2', 'snap_migration_3']
        snapshot_results = {}
        
        # Arrange: Simulate concurrent snapshot creation
        def create_snapshot(migration_id):
            try:
                snapshot_data = {
                    'migration_id': migration_id,
                    'created_at': datetime.now().isoformat(),
                    'state': {
                        'progress': 50,
                        'current_step': 'work_packages',
                        'processed_items': list(range(100))  # Large data
                    }
                }
                
                # Simulate snapshot creation time
                time.sleep(0.1)
                
                snapshot = state_manager.create_snapshot(migration_id, snapshot_data)
                snapshot_results[migration_id] = {
                    'status': 'success',
                    'snapshot_id': snapshot.id,
                    'created_at': snapshot.created_at
                }
            except Exception as e:
                snapshot_results[migration_id] = {
                    'status': 'error',
                    'error': str(e)
                }
        
        # Act: Create snapshots concurrently
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(create_snapshot, mid) for mid in migration_ids]
            for future in futures:
                future.result()
        
        # Assert: All snapshots should be created successfully
        for migration_id in migration_ids:
            assert snapshot_results[migration_id]['status'] == 'success'
            assert 'snapshot_id' in snapshot_results[migration_id]
        
        # Verify each snapshot has unique ID and proper content
        snapshot_ids = [result['snapshot_id'] for result in snapshot_results.values()]
        assert len(set(snapshot_ids)) == 3  # All unique IDs


class TestDataIntegrityDuringFailures:
    """Tests for maintaining data integrity when operations fail."""

    @pytest.fixture
    def migration_with_state(self):
        """Create migration instance with state management."""
        migration = BaseMigration("integrity_test_migration")
        migration.state_manager = MagicMock()
        return migration

    def test_rollback_on_validation_failure(self, migration_with_state):
        """
        INTEGRITY TEST: Data changes must be rolled back if validation fails.
        Tests that failed validations don't leave partial updates.
        """
        # Arrange: Mock successful data write but failed validation
        migration_with_state.write_data = MagicMock(return_value=True)
        migration_with_state.validate_data_integrity = MagicMock(
            side_effect=Exception("Data validation failed: checksum mismatch")
        )
        migration_with_state.rollback_data_changes = MagicMock()
        
        test_data = {'users': [{'id': 1, 'name': 'Test User'}]}
        
        # Act & Assert
        with pytest.raises(MigrationError, match="Data validation failed"):
            migration_with_state.migrate_with_validation(test_data)
        
        # Verify rollback was triggered
        migration_with_state.rollback_data_changes.assert_called_once()
        migration_with_state.state_manager.mark_migration_failed.assert_called_once()

    def test_referential_integrity_preservation(self, migration_with_state):
        """
        INTEGRITY TEST: Foreign key relationships must remain valid during partial failures.
        Tests that related data stays consistent when some updates fail.
        """
        # Arrange: Set up related data
        projects_data = [
            {'id': 1, 'name': 'Project A'},
            {'id': 2, 'name': 'Project B'}
        ]
        
        work_packages_data = [
            {'id': 101, 'project_id': 1, 'title': 'Task 1'},
            {'id': 102, 'project_id': 2, 'title': 'Task 2'},
            {'id': 103, 'project_id': 999, 'title': 'Orphaned Task'}  # Invalid project_id
        ]
        
        # Mock referential integrity check
        def check_references(data):
            for wp in data:
                if wp.get('project_id') == 999:
                    raise Exception(f"Invalid project_id {wp['project_id']} for work package {wp['id']}")
            return True
        
        migration_with_state.validate_references = check_references
        
        # Act & Assert: Should fail due to referential integrity violation
        with pytest.raises(MigrationError, match="Invalid project_id 999"):
            migration_with_state.migrate_work_packages(work_packages_data)
        
        # Verify that no work packages were migrated (all-or-nothing)
        migration_with_state.state_manager.get_migrated_count.assert_called()
        assert migration_with_state.state_manager.get_migrated_count('work_packages') == 0 