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

        assert "types_cached" in cache_stats
        assert "cache_invalidations" in cache_stats

        # Should have cached 1 entity type
        assert cache_stats["types_cached"] == 1
        # Should have 1 invalidation (after successful migration)
        assert cache_stats["cache_invalidations"] == 1

    def test_no_entity_type_bypasses_caching(self, migration_with_caching):
        """Test that providing no entity type bypasses caching entirely."""
        # Reset call counter
        migration_with_caching.get_entities_call_count = 0

        result = migration_with_caching.run_with_data_preservation()

        # Should fall back to standard run() without caching
        assert result.success is True
        assert migration_with_caching.run_called is True

        # No API calls should be made for entity fetching
        assert migration_with_caching.get_entities_call_count == 0

        # Cache statistics should not be present
        assert "cache_stats" not in result.details

    def test_cache_with_failed_migration_no_invalidation(self, migration_with_caching):
        """Test that failed migration doesn't trigger cache invalidation."""
        # Mock run() to return failure
        migration_with_caching.run = Mock(
            return_value=ComponentResult(
                success=False,
                message="Migration failed",
                success_count=0,
                failed_count=3,
                total_count=3,
                errors=["Test error"],
            )
        )

        result = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=True,
        )

        # Verify migration failed
        assert result.success is False

        # Verify no cache invalidation occurred (only initial calls)
        # conflict analysis: 1 call
        # should_skip_migration: cache hit (no additional call)
        # No invalidation since migration failed
        assert migration_with_caching.get_entities_call_count == 1

        # Cache stats should show no invalidations
        assert result.details["cache_stats"]["cache_invalidations"] == 0

    def test_cache_debug_logging(self, migration_with_caching, caplog):
        """Test that cache debug logging works correctly."""
        # Mock should_skip_migration to return False
        migration_with_caching.should_skip_migration = Mock(
            return_value=(False, {"total_changes": 1, "changes_by_type": "test"})
        )

        with caplog.at_level("DEBUG"):
            result = migration_with_caching.run_with_data_preservation(
                entity_type="issues",
                analyze_conflicts=True,
            )

        assert result.success is True

        # Check for cache-related debug messages
        debug_messages = [record.message for record in caplog.records]

        # Should contain cache miss and cache hit messages
        cache_messages = [msg for msg in debug_messages if "cached" in msg.lower()]
        assert len(cache_messages) >= 2

        # Should log cache invalidation
        invalidation_messages = [
            msg for msg in debug_messages if "invalidated cache" in msg.lower()
        ]
        assert len(invalidation_messages) >= 1

    def test_cache_conflict_analysis_disabled(self, migration_with_caching):
        """Test caching behavior when conflict analysis is disabled."""
        result = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=False,  # Disable conflict analysis
        )

        assert result.success is True

        # When conflict analysis is disabled, should_skip_migration is the first cache usage
        assert migration_with_caching.get_entities_call_count == 2  # should_skip + store_states after migration

        # API calls should be:
        # should_skip_migration: 1 call
        # After migration: 1 call (cache invalidated)
        # store original states and create snapshot: cached
        assert migration_with_caching.get_entities_call_count == 2

    def test_cache_exception_during_conflict_analysis(self, migration_with_caching):
        """Test cache behavior when conflict analysis raises an exception."""
        # Mock analyze_preservation_status to raise an exception
        migration_with_caching.analyze_preservation_status = Mock(
            side_effect=Exception("Conflict analysis failed")
        )

        result = migration_with_caching.run_with_data_preservation(
            entity_type="issues",
            analyze_conflicts=True,
        )

        # Migration should still succeed despite conflict analysis failure
        assert result.success is True

        # Cache should still work for subsequent operations
        # should_skip_migration: 1 call
        # conflict analysis: 1 call (even though it failed)
        # After migration: cached calls for remaining operations
        assert migration_with_caching.get_entities_call_count == 2 