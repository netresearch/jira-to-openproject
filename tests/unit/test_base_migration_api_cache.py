"""Tests for BaseMigration API call caching functionality in run_with_data_preservation."""

from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult
from src.utils.change_detector import ChangeDetector, ChangeReport
from src.utils.data_preservation_manager import DataPreservationManager
from src.utils.state_manager import StateManager


class CachingTestMigration(BaseMigration):
    """Test migration class for API caching functionality testing."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.run_called = False
        self.run_call_count = 0
        self.get_entities_call_count = 0
        self.mock_entities = [
            {"id": "1", "key": "TEST-1", "name": "Test Issue 1"},
            {"id": "2", "key": "TEST-2", "name": "Test Issue 2"},
            {"id": "3", "key": "TEST-3", "name": "Test Issue 3"},
        ]

    def run(self) -> ComponentResult:
        """Mock implementation of run method."""
        self.run_called = True
        self.run_call_count += 1
        return ComponentResult(
            success=True,
            message="Test migration completed",
            success_count=3,
            failed_count=0,
            total_count=3,
        )

    def _get_current_entities_for_type(self, entity_type: str):
        """Mock implementation for entity retrieval with call counting."""
        self.get_entities_call_count += 1
        return self.mock_entities.copy()


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
        total_entities=3,
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
        "total_conflicts": 0,
        "conflicts_by_resolution": {},
        "conflicts": [],
    }
    return manager


@pytest.fixture
def migration_with_caching(
    mock_clients,
    mock_change_detector,
    mock_state_manager,
    mock_data_preservation_manager,
):
    """Provide migration instance with all dependencies for caching tests."""
    jira_client, openproject_client = mock_clients

    migration = CachingTestMigration(
        jira_client=jira_client,
        op_client=openproject_client,
        change_detector=mock_change_detector,
        state_manager=mock_state_manager,
        data_preservation_manager=mock_data_preservation_manager,
    )

    # Mock the create_snapshot method
    migration.create_snapshot = Mock(return_value=Path("test-snapshot.json"))

    return migration


class TestAPICaching:
    """Test API call caching functionality in run_with_data_preservation."""

    def test_cache_reduces_api_calls_successful_migration(self, migration_with_caching):
        """Test that caching reduces API calls during successful migration."""
        result = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            operation_type="migrate",
            entity_count=3,
            analyze_conflicts=True,
            create_backups=True,
        )

        # Verify the migration succeeded
        assert result.success is True
        assert migration_with_caching.run_called is True

        # Verify API call reduction:
        # 1. conflict analysis: first cache call (API hit)
        # 2. should_skip_migration: cache hit (no additional API call)
        # 3. After migration: cache invalidated
        # 4. store original states: cache hit after invalidation (API hit)
        # 5. create snapshot: cache hit (no additional API call)
        # Total: 2 API calls instead of 4+ without caching
        assert migration_with_caching.get_entities_call_count == 2

        # Verify cache statistics are included in result
        assert "cache_stats" in result.details
        assert result.details["cache_stats"]["types_cached"] == 1
        # cache_invalidations tracks how many types have been invalidated (issues = 1)
        assert result.details["cache_stats"]["cache_invalidations"] == 1

    def test_cache_hit_behavior_without_invalidation(self, migration_with_caching):
        """Test cache hit behavior when no invalidation occurs."""
        # Create a test migration that will return no changes
        migration_with_caching.detect_changes = Mock(
            return_value={"total_changes": 0, "changes_by_type": {}}
        )

        result = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=True,
        )

        # Verify migration was skipped
        assert result.success is True
        assert "No changes detected" in result.message
        assert result.details["migration_skipped"] is True

        # Verify minimal API calls:
        # conflict analysis: 1 call (first cache usage)
        # should_skip_migration: cache hit (no additional call)
        # No invalidation since migration was skipped
        assert migration_with_caching.get_entities_call_count == 1

    def test_cache_invalidation_triggers_fresh_calls(self, migration_with_caching):
        """Test that cache invalidation triggers fresh API calls."""
        result = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=True,
        )

        # Verify successful migration
        assert result.success is True
        assert migration_with_caching.run_called is True

        # Track API calls:
        # 1. conflict analysis: 1 call (first cache usage)
        # 2. should_skip_migration: cache hit (no additional call)
        # 3. migration succeeds, cache invalidated
        # 4. store original states: 1 call (fresh after invalidation)
        # 5. create snapshot: cache hit (no additional call)
        assert migration_with_caching.get_entities_call_count == 2

        # Verify cache statistics show invalidation occurred
        assert result.details["cache_stats"]["cache_invalidations"] == 1

    def test_cache_isolation_between_migration_runs(self, migration_with_caching):
        """Test that cache doesn't leak between migration runs."""
        # Mock should_skip_migration to return False
        migration_with_caching.should_skip_migration = Mock(
            return_value=(False, {"total_changes": 1, "changes_by_type": "test"})
        )

        # First migration run
        result1 = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=True,
        )
        assert result1.success is True

        # Reset call counter to test second run
        first_run_calls = migration_with_caching.get_entities_call_count
        migration_with_caching.get_entities_call_count = 0

        # Second migration run - cache should start fresh
        result2 = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=True,
        )
        assert result2.success is True

        # Verify that the second run made the same number of calls as the first
        # (cache doesn't persist between runs)
        assert migration_with_caching.get_entities_call_count == 2

    def test_cache_works_correctly_during_exceptions(self, migration_with_caching):
        """Test that caching works correctly when exceptions occur."""
        # Mock run() to raise an exception
        migration_with_caching.run = Mock(
            side_effect=Exception("Test migration failure")
        )

        # Migration should handle the exception
        with pytest.raises(Exception, match="Test migration failure"):
            migration_with_caching.run_with_data_preservation(
                entity_type="issues",
                analyze_conflicts=True,
            )

        # Verify cache was used for conflict analysis despite the exception
        # conflict analysis: 1 call
        # should_skip_migration: cache hit (no additional call)
        assert migration_with_caching.get_entities_call_count == 1

    def test_cache_with_multiple_entity_types(self, migration_with_caching):
        """Test cache isolation works correctly with multiple entity types."""
        # Create a spy on _get_current_entities_for_type to track calls by entity type
        original_get_entities = migration_with_caching._get_current_entities_for_type
        call_log = []

        def track_calls(entity_type: str):
            call_log.append(entity_type)
            return original_get_entities(entity_type)

        migration_with_caching._get_current_entities_for_type = Mock(
            side_effect=track_calls
        )

        # Mock should_skip_migration to return False
        migration_with_caching.should_skip_migration = Mock(
            return_value=(False, {"total_changes": 1, "changes_by_type": "test"})
        )

        # Run migration for 'issues'
        result = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=True,
        )
        assert result.success is True

        # Verify correct entity type was used in all calls
        expected_calls = [
            call("issues"),  # should_skip_migration
            call("issues"),  # after invalidation for storing states
        ]
        migration_with_caching._get_current_entities_for_type.assert_has_calls(
            expected_calls
        )

        # All calls should be for 'issues' entity type
        assert all(entity_type == "issues" for entity_type in call_log)

    def test_cache_statistics_in_result_details(self, migration_with_caching):
        """Test that cache statistics are properly included in result details."""
        result = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=True,
        )

        # Verify cache statistics are present and correct
        assert "cache_stats" in result.details
        cache_stats = result.details["cache_stats"]

        # Check that all required cache statistics are present
        required_stats = [
            "cache_hits",
            "cache_misses",
            "cache_evictions",
            "memory_cleanups",
            "total_cache_size",
            "global_cache_types",
            "types_cached",
            "cache_invalidations",
        ]
        for stat in required_stats:
            assert stat in cache_stats

        # Basic validation of statistics values
        assert cache_stats["cache_hits"] >= 0
        assert cache_stats["cache_misses"] >= 0
        assert cache_stats["types_cached"] >= 0
        assert cache_stats["cache_invalidations"] >= 0

    def test_cache_memory_management_and_cleanup(self, migration_with_caching):
        """Test cache memory management and cleanup functionality."""
        # Mock large entity lists to trigger memory management
        large_entity_list = [{"id": f"entity_{i}", "data": "x" * 100} for i in range(2000)]
        migration_with_caching._get_current_entities_for_type = Mock(
            return_value=large_entity_list
        )

        result = migration_with_caching.run_with_data_preservation(
            entity_type="large_entities",
            analyze_conflicts=True,
        )

        assert result.success is True

        # Check that memory management statistics are tracked
        cache_stats = result.details["cache_stats"]
        assert "total_cache_size" in cache_stats
        assert cache_stats["total_cache_size"] >= 0

    def test_cache_thread_safety_simulation(self, migration_with_caching):
        """Test cache behavior under simulated concurrent access."""
        import threading
        import time

        results = []

        def run_migration():
            try:
                result = migration_with_caching.run_with_data_preservation(
                    entity_type="concurrent_entities",
                    analyze_conflicts=True,
                )
                results.append(result)
            except Exception as e:
                results.append(e)

        # Create multiple threads to simulate concurrent access
        threads = []
        for i in range(3):
            thread = threading.Thread(target=run_migration)
            threads.append(thread)

        # Start all threads
        for thread in threads:
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # All threads should complete successfully
        assert len(results) == 3
        for result in results:
            assert not isinstance(result, Exception)
            assert result.success is True

    def test_cache_global_statistics_tracking(self, migration_with_caching):
        """Test global cache statistics tracking across multiple operations."""
        # Run multiple operations to accumulate global statistics
        for i in range(3):
            result = migration_with_caching.run_with_data_preservation(
                entity_type=f"entity_type_{i}",
                analyze_conflicts=True,
            )
            assert result.success is True

        # Check that global statistics are being tracked
        # Note: Global stats are class-level, so they persist across runs
        assert hasattr(migration_with_caching, '_global_cache_stats')

    def test_cache_invalidation_edge_cases(self, migration_with_caching):
        """Test cache invalidation edge cases and error conditions."""
        # Test invalidating non-existent entity type
        result = migration_with_caching.run_with_data_preservation(
            entity_type="nonexistent_type",
            analyze_conflicts=True,
        )

        # Should handle gracefully
        assert result.success is True

        # Test multiple invalidations of same type
        migration_with_caching.should_skip_migration = Mock(
            return_value=(False, {"total_changes": 1, "changes_by_type": "test"})
        )

        result = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=True,
        )
        assert result.success is True

    def test_cache_with_api_failures_and_retries(self, migration_with_caching):
        """Test cache behavior when API calls fail and need retries."""
        # Mock API failure followed by success
        call_count = 0
        def failing_api_call(entity_type):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("API temporarily unavailable")
            return [{"id": 1, "key": "TEST-1"}, {"id": 2, "key": "TEST-2"}]

        migration_with_caching._get_current_entities_for_type = Mock(
            side_effect=failing_api_call
        )

        # The method should handle API failures gracefully
        result = migration_with_caching.run_with_data_preservation(
            entity_type="failing_entities",
            analyze_conflicts=True,
        )

        # Result might fail due to API error, but should handle gracefully
        # Check that cache statistics still work
        assert "cache_stats" in result.details

    def test_cache_with_different_migration_methods(self, migration_with_caching):
        """Test that caching works consistently across different migration methods."""
        # Test with run_with_change_detection
        result1 = migration_with_caching.run_with_change_detection(entity_type="issues")
        assert result1.success is True
        assert "cache_stats" in result1.details

        # Test with run_with_state_management
        result2 = migration_with_caching.run_with_state_management(entity_type="issues")
        assert result2.success is True
        assert "cache_stats" in result2.details

        # Both should have cache statistics
        for result in [result1, result2]:
            cache_stats = result.details["cache_stats"]
            assert "cache_hits" in cache_stats
            assert "cache_misses" in cache_stats

    def test_cache_configuration_constants(self, migration_with_caching):
        """Test that cache configuration constants are properly defined."""
        # Verify cache configuration constants exist
        assert hasattr(migration_with_caching, 'MAX_CACHE_SIZE_PER_TYPE')
        assert hasattr(migration_with_caching, 'MAX_TOTAL_CACHE_SIZE')
        assert hasattr(migration_with_caching, 'CACHE_CLEANUP_THRESHOLD')

        # Verify reasonable values
        assert migration_with_caching.MAX_CACHE_SIZE_PER_TYPE > 0
        assert migration_with_caching.MAX_TOTAL_CACHE_SIZE > 0
        assert 0 < migration_with_caching.CACHE_CLEANUP_THRESHOLD < 1

    def test_cache_cleanup_behavior(self, migration_with_caching):
        """Test cache cleanup behavior when memory limits are approached."""
        # Create scenario that might trigger cleanup
        # Fill cache with multiple entity types
        entity_types = ['type_a', 'type_b', 'type_c', 'type_d', 'type_e']

        for entity_type in entity_types:
            migration_with_caching._get_current_entities_for_type = Mock(
                return_value=[{"id": i, "data": "x" * 50} for i in range(200)]
            )

            result = migration_with_caching.run_with_data_preservation(
                entity_type=entity_type,
                analyze_conflicts=True,
            )
            assert result.success is True

        # Check final cache statistics
        cache_stats = result.details["cache_stats"]
        assert "memory_cleanups" in cache_stats
        # memory_cleanups might be 0 if cache didn't reach threshold

    def test_cache_performance_under_load(self, migration_with_caching):
        """Test cache performance characteristics under load."""
        import time

        # Test with large entity set
        large_entities = [{"id": i, "key": f"TEST-{i}", "data": f"data_{i}"} for i in range(1000)]
        migration_with_caching._get_current_entities_for_type = Mock(
            return_value=large_entities
        )

        start_time = time.time()

        # First call - should cache entities
        result1 = migration_with_caching.run_with_data_preservation(
            entity_type="performance_test",
            analyze_conflicts=True,
        )

        first_call_time = time.time() - start_time

        # Reset for second call
        migration_with_caching.get_entities_call_count = 0
        start_time = time.time()

        # Second call - should benefit from cache within the same run
        result2 = migration_with_caching.run_with_data_preservation(
            entity_type="performance_test",
            analyze_conflicts=True,
        )

        second_call_time = time.time() - start_time

        # Both should succeed
        assert result1.success is True
        assert result2.success is True

        # Verify cache statistics
        assert "cache_stats" in result1.details
        assert "cache_stats" in result2.details

    def test_cache_entity_data_integrity(self, migration_with_caching):
        """Test that cached entity data maintains integrity."""
        # Set up specific test data
        test_entities = [
            {"id": 1, "key": "TEST-1", "summary": "Test Issue 1", "status": "Open"},
            {"id": 2, "key": "TEST-2", "summary": "Test Issue 2", "status": "Closed"},
            {"id": 3, "key": "TEST-3", "summary": "Test Issue 3", "status": "In Progress"},
        ]

        migration_with_caching._get_current_entities_for_type = Mock(
            return_value=test_entities
        )

        result = migration_with_caching.run_with_data_preservation(
            entity_type="integrity_test",
            analyze_conflicts=True,
        )

        assert result.success is True

        # Verify the cached data wasn't corrupted
        # This indirectly tests through successful completion and statistics
        cache_stats = result.details["cache_stats"]
        assert cache_stats["types_cached"] >= 1

    def test_cache_with_empty_entity_lists(self, migration_with_caching):
        """Test cache behavior with empty entity lists."""
        # Mock empty entity list
        migration_with_caching._get_current_entities_for_type = Mock(return_value=[])

        result = migration_with_caching.run_with_data_preservation(
            entity_type="empty_entities",
            analyze_conflicts=True,
        )

        assert result.success is True

        # Should still have cache statistics
        assert "cache_stats" in result.details
        cache_stats = result.details["cache_stats"]
        assert cache_stats["types_cached"] >= 0

    def test_cache_statistics_accumulation(self, migration_with_caching):
        """Test that cache statistics properly accumulate across operations."""
        # Run first operation
        result1 = migration_with_caching.run_with_data_preservation(
            entity_type="accumulation_test_1",
            analyze_conflicts=True,
        )

        # Run second operation
        result2 = migration_with_caching.run_with_data_preservation(
            entity_type="accumulation_test_2",
            analyze_conflicts=True,
        )

        # Both should succeed
        assert result1.success is True
        assert result2.success is True

        # Both should have cache statistics
        for result in [result1, result2]:
            cache_stats = result.details["cache_stats"]
            assert all(stat in cache_stats for stat in [
                "cache_hits", "cache_misses", "types_cached", "cache_invalidations"
            ])

            # All statistics should be non-negative
            assert all(cache_stats[stat] >= 0 for stat in cache_stats if isinstance(cache_stats[stat], int)) 