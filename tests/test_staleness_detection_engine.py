#!/usr/bin/env python3
"""Comprehensive tests for staleness detection engine in EnhancedUserAssociationMigrator.

Tests cover TTL-based staleness detection, automatic refresh mechanisms, batch operations,
configuration parsing, exception handling, and integration with migration methods.
"""

from datetime import UTC, datetime, timedelta
from typing import Never
from unittest.mock import MagicMock, patch

import pytest

from src.utils.enhanced_user_association_migrator import (
    EnhancedUserAssociationMigrator,
    StaleMappingError,
    UserAssociationMapping,
)

# ============================================================================
# Test Fixtures and Mocks
# ============================================================================


class MockJiraClient:
    """Mock Jira client for testing."""

    def __init__(self, users=None, raise_exception=False) -> None:
        self._users = users or {}
        self._raise_exception = raise_exception

    def get_user_info(self, username):
        """Mock get_user_info method."""
        if self._raise_exception:
            msg = "Jira API error"
            raise RuntimeError(msg)
        return self._users.get(username)

    def get(self, path):
        """Mock generic get method used during initialization."""
        return MagicMock(status_code=404, json=list)


class MockOpenProjectClient:
    """Mock OpenProject client for testing."""

    def __init__(self, users_by_email=None, raise_exception=False) -> None:
        self._users_by_email = users_by_email or {}
        self._raise_exception = raise_exception

    def get_user_by_email(self, email):
        """Mock get_user_by_email method."""
        if self._raise_exception:
            msg = "OpenProject API error"
            raise RuntimeError(msg)
        return self._users_by_email.get(email)

    def get_user(self, user_id):
        """Mock get_user method."""
        return {"id": user_id, "status": "active"}

    def get_users(self, **kwargs):
        """Mock get_users method."""
        return []


@pytest.fixture
def mock_config(tmp_path, monkeypatch):
    """Mock config to avoid file I/O during tests."""
    monkeypatch.setattr(
        "src.utils.enhanced_user_association_migrator.config.get_path",
        lambda x: tmp_path,
    )
    monkeypatch.setattr(
        "src.utils.enhanced_user_association_migrator.config.logger",
        MagicMock(),
    )

    # Mock migration config for staleness settings
    mock_migration_config = {
        "mapping": {"refresh_interval": "1h", "fallback_strategy": "skip"},
    }
    monkeypatch.setattr(
        "src.utils.enhanced_user_association_migrator.config.migration_config",
        mock_migration_config,
    )
    return mock_migration_config


@pytest.fixture
def basic_migrator(mock_config):
    """Create basic migrator instance for testing."""
    jira_client = MockJiraClient()
    op_client = MockOpenProjectClient()

    migrator = EnhancedUserAssociationMigrator(jira_client, op_client, user_mapping={})
    migrator.refresh_interval_seconds = 3600  # 1 hour for testing

    return migrator


@pytest.fixture
def migrator_with_mappings(basic_migrator):
    """Migrator with sample mappings for testing."""
    current_time = datetime.now(tz=UTC)

    # Fresh mapping (recently refreshed)
    basic_migrator.enhanced_user_mappings["fresh_user"] = UserAssociationMapping(
        jira_username="fresh_user",
        jira_user_id="fresh_user_id",
        jira_display_name="Fresh User",
        jira_email="fresh@example.com",
        openproject_user_id=1,
        openproject_username="fresh_user",
        openproject_email="fresh@example.com",
        mapping_status="mapped",
        fallback_user_id=None,
        metadata={"jira_active": True, "openproject_active": True},
        lastRefreshed=current_time.isoformat(),
    )

    # Stale mapping (old timestamp)
    stale_time = current_time - timedelta(hours=2)
    basic_migrator.enhanced_user_mappings["stale_user"] = UserAssociationMapping(
        jira_username="stale_user",
        jira_user_id="stale_user_id",
        jira_display_name="Stale User",
        jira_email="stale@example.com",
        openproject_user_id=2,
        openproject_username="stale_user",
        openproject_email="stale@example.com",
        mapping_status="mapped",
        fallback_user_id=None,
        metadata={"jira_active": True, "openproject_active": True},
        lastRefreshed=stale_time.isoformat(),
    )

    # Mapping without lastRefreshed (considered stale)
    basic_migrator.enhanced_user_mappings["no_timestamp"] = UserAssociationMapping(
        jira_username="no_timestamp",
        jira_user_id="no_timestamp_id",
        jira_display_name="No Timestamp User",
        jira_email="notimestamp@example.com",
        openproject_user_id=3,
        openproject_username="no_timestamp",
        openproject_email="notimestamp@example.com",
        mapping_status="mapped",
        fallback_user_id=None,
        metadata={"jira_active": True, "openproject_active": True},
        lastRefreshed=None,
    )

    return basic_migrator


# ============================================================================
# StaleMappingError Exception Tests
# ============================================================================


class TestStaleMappingError:
    """Test StaleMappingError exception class."""

    def test_exception_initialization_default_reason(self) -> None:
        """Test StaleMappingError with default reason."""
        username = "test_user"
        error = StaleMappingError(username)

        assert error.username == username
        assert error.reason == "Mapping is stale"
        assert str(error) == f"Stale mapping detected for user '{username}': Mapping is stale"

    def test_exception_initialization_custom_reason(self) -> None:
        """Test StaleMappingError with custom reason."""
        username = "test_user"
        reason = "Age 7200s exceeds TTL 3600s"
        error = StaleMappingError(username, reason)

        assert error.username == username
        assert error.reason == reason
        assert str(error) == f"Stale mapping detected for user '{username}': {reason}"

    def test_exception_inheritance(self) -> None:
        """Test that StaleMappingError inherits from Exception."""
        error = StaleMappingError("test_user")
        assert isinstance(error, Exception)


# ============================================================================
# TTL-based Staleness Detection Tests
# ============================================================================


class TestStalenessDetection:
    """Test core staleness detection logic."""

    def test_is_mapping_stale_fresh_mapping(self, migrator_with_mappings) -> None:
        """Test fresh mapping is not stale."""
        migrator = migrator_with_mappings
        assert not migrator.is_mapping_stale("fresh_user")

    def test_is_mapping_stale_stale_mapping(self, migrator_with_mappings) -> None:
        """Test old mapping is stale."""
        migrator = migrator_with_mappings
        assert migrator.is_mapping_stale("stale_user")

    def test_is_mapping_stale_missing_mapping(self, basic_migrator) -> None:
        """Test missing mapping is considered stale."""
        migrator = basic_migrator
        assert migrator.is_mapping_stale("nonexistent_user")

    def test_is_mapping_stale_no_timestamp(self, migrator_with_mappings) -> None:
        """Test mapping without lastRefreshed is stale."""
        migrator = migrator_with_mappings
        assert migrator.is_mapping_stale("no_timestamp")

    def test_is_mapping_stale_boundary_condition(self, basic_migrator) -> None:
        """Test boundary condition where age equals TTL."""
        migrator = basic_migrator
        current_time = datetime.now(tz=UTC)
        boundary_time = current_time - timedelta(
            seconds=migrator.refresh_interval_seconds,
        )

        migrator.enhanced_user_mappings["boundary_user"] = UserAssociationMapping(
            jira_username="boundary_user",
            jira_user_id="boundary_id",
            jira_display_name="Boundary User",
            jira_email="boundary@example.com",
            openproject_user_id=99,
            openproject_username="boundary_user",
            openproject_email="boundary@example.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=boundary_time.isoformat(),
        )

        # Age exactly equals TTL, should be stale (>= threshold)
        assert migrator.is_mapping_stale("boundary_user", current_time)

    def test_is_mapping_stale_invalid_timestamp(self, basic_migrator, caplog) -> None:
        """Test invalid timestamp format returns stale with warning."""
        migrator = basic_migrator

        migrator.enhanced_user_mappings["invalid_ts"] = UserAssociationMapping(
            jira_username="invalid_ts",
            jira_user_id="invalid_id",
            jira_display_name="Invalid TS User",
            jira_email="invalid@example.com",
            openproject_user_id=98,
            openproject_username="invalid_ts",
            openproject_email="invalid@example.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed="invalid-timestamp-format",
        )

        assert migrator.is_mapping_stale("invalid_ts")
        assert "Invalid lastRefreshed timestamp" in caplog.text

    def test_is_mapping_stale_custom_current_time(self, basic_migrator) -> None:
        """Test staleness detection with custom current time."""
        migrator = basic_migrator
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        refresh_time = base_time - timedelta(minutes=30)
        check_time = base_time

        migrator.enhanced_user_mappings["time_test"] = UserAssociationMapping(
            jira_username="time_test",
            jira_user_id="time_test_id",
            jira_display_name="Time Test User",
            jira_email="timetest@example.com",
            openproject_user_id=97,
            openproject_username="time_test",
            openproject_email="timetest@example.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=refresh_time.isoformat(),
        )

        # 30 minutes old, refresh interval is 1 hour, should be fresh
        assert not migrator.is_mapping_stale("time_test", check_time)


# ============================================================================
# Staleness Check and Handling Tests
# ============================================================================


class TestStalenessCheckHandling:
    """Test check_and_handle_staleness method."""

    def test_check_fresh_mapping_returns_mapping(self, migrator_with_mappings) -> None:
        """Test fresh mapping is returned."""
        migrator = migrator_with_mappings
        mapping = migrator.check_and_handle_staleness(
            "fresh_user",
            raise_on_stale=False,
        )

        assert mapping is not None
        assert mapping["jira_username"] == "fresh_user"

    def test_check_missing_mapping_raises_exception(self, basic_migrator) -> None:
        """Test missing mapping raises StaleMappingError when raise_on_stale=True."""
        migrator = basic_migrator

        with pytest.raises(StaleMappingError) as exc_info:
            migrator.check_and_handle_staleness("nonexistent", raise_on_stale=True)

        assert "Mapping does not exist" in str(exc_info.value)
        assert exc_info.value.username == "nonexistent"

    def test_check_missing_mapping_returns_none(self, basic_migrator) -> None:
        """Test missing mapping returns None when raise_on_stale=False."""
        migrator = basic_migrator
        mapping = migrator.check_and_handle_staleness(
            "nonexistent",
            raise_on_stale=False,
        )

        assert mapping is None

    def test_check_stale_mapping_raises_exception(self, migrator_with_mappings) -> None:
        """Test stale mapping raises StaleMappingError when raise_on_stale=True."""
        migrator = migrator_with_mappings

        with pytest.raises(StaleMappingError) as exc_info:
            migrator.check_and_handle_staleness("stale_user", raise_on_stale=True)

        assert "exceeds TTL" in str(exc_info.value)
        assert exc_info.value.username == "stale_user"

    def test_check_stale_mapping_returns_none(self, migrator_with_mappings) -> None:
        """Test stale mapping returns None when raise_on_stale=False."""
        migrator = migrator_with_mappings
        mapping = migrator.check_and_handle_staleness(
            "stale_user",
            raise_on_stale=False,
        )

        assert mapping is None


# ============================================================================
# Mapping Retrieval with Staleness Check Tests
# ============================================================================


class TestMappingRetrievalWithStalenessCheck:
    """Test get_mapping_with_staleness_check method."""

    def test_get_fresh_mapping_returns_immediately(
        self,
        migrator_with_mappings,
    ) -> None:
        """Test fresh mapping is returned without refresh attempt."""
        migrator = migrator_with_mappings

        with patch.object(migrator, "refresh_user_mapping") as mock_refresh:
            mapping = migrator.get_mapping_with_staleness_check(
                "fresh_user",
                auto_refresh=True,
            )

            assert mapping is not None
            assert mapping["jira_username"] == "fresh_user"
            mock_refresh.assert_not_called()

    def test_get_stale_mapping_no_auto_refresh(self, migrator_with_mappings) -> None:
        """Test stale mapping returns None when auto_refresh=False."""
        migrator = migrator_with_mappings

        with patch.object(migrator, "refresh_user_mapping") as mock_refresh:
            mapping = migrator.get_mapping_with_staleness_check(
                "stale_user",
                auto_refresh=False,
            )

            assert mapping is None
            mock_refresh.assert_not_called()

    def test_get_stale_mapping_auto_refresh_success(
        self,
        migrator_with_mappings,
    ) -> None:
        """Test stale mapping with successful auto-refresh."""
        migrator = migrator_with_mappings

        refreshed_mapping = UserAssociationMapping(
            jira_username="stale_user",
            jira_user_id="stale_user_id",
            jira_display_name="Refreshed User",
            jira_email="stale@example.com",
            openproject_user_id=2,
            openproject_username="stale_user",
            openproject_email="stale@example.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={"refresh_success": True},
            lastRefreshed=datetime.now(tz=UTC).isoformat(),
        )

        with patch.object(
            migrator,
            "refresh_user_mapping",
            return_value=refreshed_mapping,
        ) as mock_refresh:
            mapping = migrator.get_mapping_with_staleness_check(
                "stale_user",
                auto_refresh=True,
            )

            assert mapping is refreshed_mapping
            mock_refresh.assert_called_once_with("stale_user")

    def test_get_stale_mapping_auto_refresh_fails(self, migrator_with_mappings) -> None:
        """Test stale mapping with failed auto-refresh."""
        migrator = migrator_with_mappings

        with patch.object(
            migrator,
            "refresh_user_mapping",
            return_value=None,
        ) as mock_refresh:
            mapping = migrator.get_mapping_with_staleness_check(
                "stale_user",
                auto_refresh=True,
            )

            assert mapping is None
            mock_refresh.assert_called_once_with("stale_user")

    def test_get_mapping_exception_handling(self, migrator_with_mappings) -> None:
        """Test exception handling in staleness check."""
        migrator = migrator_with_mappings

        with patch.object(
            migrator,
            "check_and_handle_staleness",
            side_effect=Exception("Unexpected error"),
        ):
            mapping = migrator.get_mapping_with_staleness_check("fresh_user")

            assert mapping is None


# ============================================================================
# Bulk Staleness Detection Tests
# ============================================================================


class TestBulkStalenessDetection:
    """Test detect_stale_mappings method."""

    def test_detect_stale_mappings_all_users(self, migrator_with_mappings) -> None:
        """Test detecting stale mappings for all users."""
        migrator = migrator_with_mappings
        stale_mappings = migrator.detect_stale_mappings()

        # Should detect stale_user and no_timestamp as stale
        assert "stale_user" in stale_mappings
        assert "no_timestamp" in stale_mappings
        assert "fresh_user" not in stale_mappings

        assert "exceeds TTL" in stale_mappings["stale_user"]
        assert "No lastRefreshed timestamp" in stale_mappings["no_timestamp"]

    def test_detect_stale_mappings_specific_users(self, migrator_with_mappings) -> None:
        """Test detecting stale mappings for specific users."""
        migrator = migrator_with_mappings
        stale_mappings = migrator.detect_stale_mappings(["fresh_user", "stale_user"])

        # Should only check specified users
        assert "stale_user" in stale_mappings
        assert "fresh_user" not in stale_mappings
        assert "no_timestamp" not in stale_mappings

    def test_detect_stale_mappings_no_stale_users(self, basic_migrator) -> None:
        """Test detecting stale mappings when none exist."""
        migrator = basic_migrator
        current_time = datetime.now(tz=UTC)

        # Add only fresh mapping
        migrator.enhanced_user_mappings["fresh_only"] = UserAssociationMapping(
            jira_username="fresh_only",
            jira_user_id="fresh_id",
            jira_display_name="Fresh Only",
            jira_email="fresh@example.com",
            openproject_user_id=1,
            openproject_username="fresh_only",
            openproject_email="fresh@example.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=current_time.isoformat(),
        )

        stale_mappings = migrator.detect_stale_mappings()
        assert len(stale_mappings) == 0

    def test_detect_stale_mappings_error_handling(self, basic_migrator) -> None:
        """Test error handling during staleness detection."""
        migrator = basic_migrator

        # Add mapping that will cause error during staleness check
        migrator.enhanced_user_mappings["error_user"] = {"invalid": "mapping"}

        with patch.object(
            migrator,
            "is_mapping_stale",
            side_effect=Exception("Test error"),
        ):
            stale_mappings = migrator.detect_stale_mappings()

            assert "error_user" in stale_mappings
            assert "Error during check" in stale_mappings["error_user"]


# ============================================================================
# Batch Refresh Tests
# ============================================================================


class TestBatchRefresh:
    """Test batch_refresh_stale_mappings method."""

    def test_batch_refresh_no_stale_mappings(self, basic_migrator) -> None:
        """Test batch refresh when no stale mappings exist."""
        migrator = basic_migrator

        with patch.object(migrator, "detect_stale_mappings", return_value={}):
            result = migrator.batch_refresh_stale_mappings()

            assert result["total_stale"] == 0
            assert result["refresh_attempted"] == 0
            assert result["refresh_successful"] == 0
            assert result["refresh_failed"] == 0
            assert len(result["results"]) == 0
            assert len(result["errors"]) == 0

    def test_batch_refresh_success_first_try(self, basic_migrator) -> None:
        """Test batch refresh with successful first attempt."""
        migrator = basic_migrator

        stale_mappings = {"user1": "Age 7200s exceeds TTL 3600s"}
        refreshed_mapping = {
            "lastRefreshed": datetime.now(tz=UTC).isoformat(),
            "metadata": {"refresh_success": True},
        }

        with (
            patch.object(
                migrator,
                "detect_stale_mappings",
                return_value=stale_mappings,
            ),
            patch.object(
                migrator,
                "refresh_user_mapping",
                return_value=refreshed_mapping,
            ),
        ):
            result = migrator.batch_refresh_stale_mappings()

            assert result["total_stale"] == 1
            assert result["refresh_attempted"] == 1
            assert result["refresh_successful"] == 1
            assert result["refresh_failed"] == 0
            assert result["results"]["user1"]["status"] == "success"
            assert result["results"]["user1"]["attempts"] == 1

    def test_batch_refresh_retry_then_success(self, basic_migrator) -> None:
        """Test batch refresh with retry logic.

        Note: Retries only occur when exceptions are raised, not on None returns.
        A None return immediately fails the refresh for that user.
        """
        migrator = basic_migrator

        stale_mappings = {"user1": "Stale mapping"}
        refreshed_mapping = {
            "lastRefreshed": datetime.now(tz=UTC).isoformat(),
            "metadata": {"refresh_success": True},
        }

        call_count = 0

        def mock_refresh(username):
            nonlocal call_count
            call_count += 1
            # Raise exception on first call to trigger retry logic
            if call_count < 2:
                msg = "Temporary error"
                raise RuntimeError(msg)
            return refreshed_mapping

        with (
            patch.object(
                migrator,
                "detect_stale_mappings",
                return_value=stale_mappings,
            ),
            patch.object(migrator, "refresh_user_mapping", side_effect=mock_refresh),
        ):
            result = migrator.batch_refresh_stale_mappings(max_retries=2)

            assert result["refresh_successful"] == 1
            assert result["results"]["user1"]["status"] == "success"
            assert result["results"]["user1"]["attempts"] == 2

    def test_batch_refresh_all_retries_fail(self, basic_migrator) -> None:
        """Test batch refresh when all retries fail.

        Note: When refresh_user_mapping returns None, the implementation
        breaks immediately without retrying. Retries only occur on exceptions.
        """
        migrator = basic_migrator

        stale_mappings = {"user1": "Stale mapping"}

        with (
            patch.object(
                migrator,
                "detect_stale_mappings",
                return_value=stale_mappings,
            ),
            patch.object(migrator, "refresh_user_mapping", return_value=None),
        ):
            result = migrator.batch_refresh_stale_mappings(max_retries=1)

            assert result["refresh_failed"] == 1
            assert result["results"]["user1"]["status"] == "failed"
            # None return breaks immediately, so only 1 attempt
            assert result["results"]["user1"]["attempts"] == 1
            assert "user1: Refresh returned None" in result["errors"]

    def test_batch_refresh_exception_handling(self, basic_migrator) -> None:
        """Test batch refresh exception handling."""
        migrator = basic_migrator

        stale_mappings = {"user1": "Stale mapping"}

        with (
            patch.object(
                migrator,
                "detect_stale_mappings",
                return_value=stale_mappings,
            ),
            patch.object(
                migrator,
                "refresh_user_mapping",
                side_effect=Exception("API error"),
            ),
        ):
            result = migrator.batch_refresh_stale_mappings(max_retries=1)

            assert result["refresh_failed"] == 1
            assert result["results"]["user1"]["status"] == "failed"
            assert "API error" in result["results"]["user1"]["error"]


# ============================================================================
# Individual Refresh Tests
# ============================================================================


class TestIndividualRefresh:
    """Test refresh_user_mapping method."""

    def test_refresh_user_mapping_success(self, basic_migrator) -> None:
        """Test successful user mapping refresh."""
        migrator = basic_migrator
        migrator.jira_client = MockJiraClient(
            {
                "test_user": {
                    "accountId": "test_account_id",
                    "displayName": "Test User",
                    "emailAddress": "test@example.com",
                    "active": True,
                },
            },
        )

        with patch.object(migrator, "_save_enhanced_mappings"):
            result = migrator.refresh_user_mapping("test_user")

            assert result is not None
            assert result["metadata"]["jira_active"] is True
            assert result["metadata"]["jira_display_name"] == "Test User"
            assert result["metadata"]["refresh_success"] is True
            assert result["lastRefreshed"] is not None

    def test_refresh_user_mapping_user_not_found(self, basic_migrator) -> None:
        """Test refresh when user not found in Jira."""
        migrator = basic_migrator
        migrator.jira_client = MockJiraClient()  # Empty users dict

        with patch.object(migrator, "_save_enhanced_mappings"):
            result = migrator.refresh_user_mapping("nonexistent_user")

            # When user not found, implementation raises exception and returns False
            # (returns None only if jira_client has get_user_info_with_timeout)
            assert result is False
            # Error info is saved to enhanced_user_mappings
            assert "nonexistent_user" in migrator.enhanced_user_mappings
            error_mapping = migrator.enhanced_user_mappings["nonexistent_user"]
            assert error_mapping["metadata"]["refresh_success"] is False
            assert "No user data returned" in error_mapping["metadata"]["refresh_error"]

    def test_refresh_user_mapping_inactive_user(self, basic_migrator) -> None:
        """Test refresh with inactive Jira user."""
        migrator = basic_migrator
        migrator.jira_client = MockJiraClient(
            {
                "inactive_user": {
                    "accountId": "inactive_id",
                    "displayName": "Inactive User",
                    "emailAddress": "inactive@example.com",
                    "active": False,
                },
            },
        )

        with patch.object(migrator, "_save_enhanced_mappings"):
            result = migrator.refresh_user_mapping("inactive_user")

            # Inactive users fail validation and return None
            # (the validation fails and fallback is applied)
            assert result is None

    def test_refresh_user_mapping_api_error(self, basic_migrator) -> None:
        """Test refresh with Jira API error."""
        migrator = basic_migrator
        migrator.jira_client = MockJiraClient(raise_exception=True)

        with patch.object(migrator, "_save_enhanced_mappings"):
            result = migrator.refresh_user_mapping("test_user")

            # Returns False when jira_client doesn't have get_user_info_with_timeout
            assert result is False
            # Should have error mapping saved
            assert "test_user" in migrator.enhanced_user_mappings
            error_mapping = migrator.enhanced_user_mappings["test_user"]
            assert error_mapping["metadata"]["refresh_success"] is False


# ============================================================================
# OpenProject Mapping Attempt Tests
# ============================================================================


class TestOpenProjectMappingAttempt:
    """Test _attempt_openproject_mapping method."""

    def test_attempt_openproject_mapping_success(self, basic_migrator) -> None:
        """Test successful OpenProject mapping discovery."""
        migrator = basic_migrator
        migrator.op_client = MockOpenProjectClient(
            {
                "test@example.com": {
                    "id": 123,
                    "email": "test@example.com",
                    "firstname": "Test",
                    "lastname": "User",
                    "status": "active",
                },
            },
        )

        mapping = UserAssociationMapping(
            jira_username="test_user",
            jira_user_id="test_id",
            jira_display_name="Test User",
            jira_email="test@example.com",
            openproject_user_id=None,
            openproject_username=None,
            openproject_email=None,
            mapping_status="unmapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=None,
        )

        jira_data = {"emailAddress": "test@example.com"}
        result = migrator._attempt_openproject_mapping(mapping, jira_data)

        assert result["openproject_user_id"] == 123
        assert result["mapping_status"] == "mapped"
        assert result["metadata"]["mapping_method"] == "email_refresh"

    def test_attempt_openproject_mapping_no_match(self, basic_migrator) -> None:
        """Test OpenProject mapping when no user found."""
        migrator = basic_migrator
        migrator.op_client = MockOpenProjectClient()  # Empty users

        mapping = UserAssociationMapping(
            jira_username="test_user",
            jira_user_id="test_id",
            jira_display_name="Test User",
            jira_email="test@example.com",
            openproject_user_id=None,
            openproject_username=None,
            openproject_email=None,
            mapping_status="unmapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=None,
        )

        jira_data = {"emailAddress": "test@example.com"}
        result = migrator._attempt_openproject_mapping(mapping, jira_data)

        assert result["mapping_status"] == "no_openproject_match"
        assert result["metadata"]["openproject_search_attempted"] is True

    def test_attempt_openproject_mapping_no_email(self, basic_migrator) -> None:
        """Test OpenProject mapping when no email available."""
        migrator = basic_migrator

        mapping = UserAssociationMapping(
            jira_username="test_user",
            jira_user_id="test_id",
            jira_display_name="Test User",
            jira_email=None,
            openproject_user_id=None,
            openproject_username=None,
            openproject_email=None,
            mapping_status="unmapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=None,
        )

        jira_data = {}  # No email
        result = migrator._attempt_openproject_mapping(mapping, jira_data)

        assert result["mapping_status"] == "no_email"


# ============================================================================
# Manual Refresh Trigger Tests
# ============================================================================


class TestManualRefreshTrigger:
    """Test trigger_mapping_refresh method."""

    def test_trigger_refresh_fresh_mapping_no_force(
        self,
        migrator_with_mappings,
    ) -> None:
        """Test trigger refresh on fresh mapping without force."""
        migrator = migrator_with_mappings

        with patch.object(migrator, "refresh_user_mapping") as mock_refresh:
            result = migrator.trigger_mapping_refresh("fresh_user", force=False)

            assert result is True
            mock_refresh.assert_not_called()

    def test_trigger_refresh_fresh_mapping_with_force(
        self,
        migrator_with_mappings,
    ) -> None:
        """Test trigger refresh on fresh mapping with force."""
        migrator = migrator_with_mappings

        refreshed_mapping = {"lastRefreshed": datetime.now(tz=UTC).isoformat()}
        with patch.object(
            migrator,
            "refresh_user_mapping",
            return_value=refreshed_mapping,
        ) as mock_refresh:
            result = migrator.trigger_mapping_refresh("fresh_user", force=True)

            assert result is True
            mock_refresh.assert_called_once_with("fresh_user")

    def test_trigger_refresh_stale_mapping_success(
        self,
        migrator_with_mappings,
    ) -> None:
        """Test trigger refresh on stale mapping with success."""
        migrator = migrator_with_mappings

        refreshed_mapping = {"lastRefreshed": datetime.now(tz=UTC).isoformat()}
        with patch.object(
            migrator,
            "refresh_user_mapping",
            return_value=refreshed_mapping,
        ) as mock_refresh:
            result = migrator.trigger_mapping_refresh("stale_user")

            assert result is True
            mock_refresh.assert_called_once_with("stale_user")

    def test_trigger_refresh_failure(self, migrator_with_mappings) -> None:
        """Test trigger refresh failure."""
        migrator = migrator_with_mappings

        with patch.object(
            migrator,
            "refresh_user_mapping",
            return_value=None,
        ) as mock_refresh:
            result = migrator.trigger_mapping_refresh("stale_user")

            assert result is False
            mock_refresh.assert_called_once_with("stale_user")

    def test_trigger_refresh_exception(self, basic_migrator) -> None:
        """Test trigger refresh with exception."""
        migrator = basic_migrator

        with patch.object(migrator, "is_mapping_stale", side_effect=Exception("Error")):
            result = migrator.trigger_mapping_refresh("test_user")

            assert result is False


# ============================================================================
# Mapping Validation Tests
# ============================================================================


class TestMappingValidation:
    """Test validate_mapping_freshness method."""

    def test_validate_mapping_freshness_all_users(self, migrator_with_mappings) -> None:
        """Test validation of all user mappings."""
        migrator = migrator_with_mappings
        result = migrator.validate_mapping_freshness()

        assert result["total_checked"] == 3
        assert result["fresh_mappings"] == 1  # fresh_user
        assert result["stale_mappings"] == 2  # stale_user, no_timestamp
        assert result["missing_mappings"] == 0

        assert "fresh_user" not in result["stale_users"]
        assert "stale_user" in result["stale_users"]
        assert "no_timestamp" in result["stale_users"]

    def test_validate_mapping_freshness_specific_users(
        self,
        migrator_with_mappings,
    ) -> None:
        """Test validation of specific users."""
        migrator = migrator_with_mappings
        result = migrator.validate_mapping_freshness(["fresh_user", "nonexistent"])

        assert result["total_checked"] == 2
        assert result["fresh_mappings"] == 1
        assert result["stale_mappings"] == 0
        assert result["missing_mappings"] == 1

        assert "nonexistent" in result["missing_users"]

    def test_validate_mapping_recommendations(self, migrator_with_mappings) -> None:
        """Test validation recommendations generation."""
        migrator = migrator_with_mappings
        result = migrator.validate_mapping_freshness()

        # Should have recommendations for each stale user
        stale_recommendations = [r for r in result["recommendations"] if r["action"] == "refresh"]
        assert len(stale_recommendations) == 2

        # Should have batch refresh recommendation
        batch_recommendations = [r for r in result["recommendations"] if r["action"] == "batch_refresh"]
        assert len(batch_recommendations) == 1

    def test_validate_mapping_error_handling(self, basic_migrator) -> None:
        """Test validation error handling."""
        migrator = basic_migrator

        # Add invalid mapping that will cause error
        migrator.enhanced_user_mappings["error_user"] = {"invalid": "mapping"}

        with patch.object(
            migrator,
            "is_mapping_stale",
            side_effect=Exception("Test error"),
        ):
            result = migrator.validate_mapping_freshness()

            assert result["error_mappings"] == 1


# ============================================================================
# Configuration Tests
# ============================================================================


class TestConfigurationParsing:
    """Test configuration parsing methods."""

    @pytest.mark.parametrize(
        ("duration_str", "expected_seconds"),
        [
            ("30s", 30),
            ("5m", 300),
            ("2h", 7200),
            ("1d", 86400),
            ("  3h  ", 10800),  # Test whitespace handling
        ],
    )
    def test_parse_duration_valid_formats(self, duration_str, expected_seconds) -> None:
        """Test parsing valid duration formats."""
        migrator = EnhancedUserAssociationMigrator
        result = migrator._parse_duration(None, duration_str)
        assert result == expected_seconds

    @pytest.mark.parametrize(
        "invalid_duration",
        [
            "0h",  # Zero duration
            "-5m",  # Negative duration
            "99x",  # Invalid unit
            "abc",  # Invalid format
            "",  # Empty string
            "5",  # Missing unit
            "h5",  # Wrong order
        ],
    )
    def test_parse_duration_invalid_formats(self, invalid_duration) -> None:
        """Test parsing invalid duration formats."""
        migrator = EnhancedUserAssociationMigrator
        with pytest.raises(ValueError):
            migrator._parse_duration(None, invalid_duration)

    @pytest.mark.parametrize("strategy", ["skip", "assign_admin", "create_placeholder"])
    def test_validate_fallback_strategy_valid(self, strategy) -> None:
        """Test validation of valid fallback strategies."""
        migrator = EnhancedUserAssociationMigrator
        result = migrator._validate_fallback_strategy(None, strategy)
        assert result == strategy

    @pytest.mark.parametrize("invalid_strategy", ["invalid", "delete", "ignore", ""])
    def test_validate_fallback_strategy_invalid(self, invalid_strategy) -> None:
        """Test validation of invalid fallback strategies."""
        migrator = EnhancedUserAssociationMigrator
        with pytest.raises(ValueError):
            migrator._validate_fallback_strategy(None, invalid_strategy)

    def test_load_staleness_config_defaults(self, basic_migrator) -> None:
        """Test loading staleness config with defaults."""
        migrator = basic_migrator

        # Should have loaded default values
        assert migrator.refresh_interval_seconds == 3600  # 1 hour
        assert migrator.fallback_strategy == "skip"


# ============================================================================
# Migration Integration Tests
# ============================================================================


class TestMigrationIntegration:
    """Test integration with migration methods."""

    def test_migrate_assignee_with_staleness_detection(self, basic_migrator) -> None:
        """Test _migrate_assignee uses staleness detection."""
        migrator = basic_migrator

        assignee_data = {"username": "test_assignee"}
        work_package_data = {}

        fresh_mapping = UserAssociationMapping(
            jira_username="test_assignee",
            jira_user_id="test_id",
            jira_display_name="Test Assignee",
            jira_email="assignee@example.com",
            openproject_user_id=123,
            openproject_username="test_assignee",
            openproject_email="assignee@example.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={"openproject_active": True},
            lastRefreshed=datetime.now(tz=UTC).isoformat(),
        )

        with patch.object(
            migrator,
            "get_mapping_with_staleness_check",
            return_value=fresh_mapping,
        ) as mock_check:
            result = migrator._migrate_assignee(assignee_data, work_package_data)

            mock_check.assert_called_once_with("test_assignee", auto_refresh=False)
            assert work_package_data["assigned_to_id"] == 123
            assert len(result["warnings"]) == 0

    def test_migrate_assignee_stale_mapping_fallback(self, basic_migrator) -> None:
        """Test _migrate_assignee fallback when mapping is stale."""
        migrator = basic_migrator
        migrator.fallback_users = {"admin": 999}

        assignee_data = {"username": "stale_assignee"}
        work_package_data = {}

        def mock_staleness_check(username, auto_refresh=False) -> Never:
            raise StaleMappingError(username, "Mapping is stale")

        with patch.object(
            migrator,
            "get_mapping_with_staleness_check",
            side_effect=mock_staleness_check,
        ):
            result = migrator._migrate_assignee(assignee_data, work_package_data)

            assert work_package_data["assigned_to_id"] == 999  # Fallback user
            assert any("mapping stale" in w for w in result["warnings"])

    def test_migrate_author_with_staleness_detection(self, basic_migrator) -> None:
        """Test _migrate_author uses staleness detection."""
        migrator = basic_migrator

        reporter_data = {"username": "test_author"}
        work_package_data = {}

        fresh_mapping = UserAssociationMapping(
            jira_username="test_author",
            jira_user_id="author_id",
            jira_display_name="Test Author",
            jira_email="author@example.com",
            openproject_user_id=456,
            openproject_username="test_author",
            openproject_email="author@example.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={"openproject_active": True},
            lastRefreshed=datetime.now(tz=UTC).isoformat(),
        )

        with patch.object(
            migrator,
            "get_mapping_with_staleness_check",
            return_value=fresh_mapping,
        ) as mock_check:
            result = migrator._migrate_author(
                reporter_data,
                None,
                work_package_data,
                False,
            )

            mock_check.assert_called_once_with("test_author", auto_refresh=True)
            assert work_package_data["author_id"] == 456
            assert len(result["warnings"]) == 0

    def test_migrate_watchers_with_staleness_detection(self, basic_migrator) -> None:
        """Test _migrate_watchers uses staleness detection."""
        migrator = basic_migrator

        watchers_data = [{"username": "watcher1"}, {"username": "watcher2"}]
        work_package_data = {}

        mappings = {
            "watcher1": UserAssociationMapping(
                jira_username="watcher1",
                jira_user_id="watcher1_id",
                jira_display_name="Watcher 1",
                jira_email="watcher1@example.com",
                openproject_user_id=101,
                openproject_username="watcher1",
                openproject_email="watcher1@example.com",
                mapping_status="mapped",
                fallback_user_id=None,
                metadata={"openproject_active": True},
                lastRefreshed=datetime.now(tz=UTC).isoformat(),
            ),
            "watcher2": UserAssociationMapping(
                jira_username="watcher2",
                jira_user_id="watcher2_id",
                jira_display_name="Watcher 2",
                jira_email="watcher2@example.com",
                openproject_user_id=102,
                openproject_username="watcher2",
                openproject_email="watcher2@example.com",
                mapping_status="mapped",
                fallback_user_id=None,
                metadata={"openproject_active": True},
                lastRefreshed=datetime.now(tz=UTC).isoformat(),
            ),
        }

        def mock_staleness_check(username, auto_refresh=False):
            return mappings.get(username)

        with patch.object(
            migrator,
            "get_mapping_with_staleness_check",
            side_effect=mock_staleness_check,
        ) as mock_check:
            result = migrator._migrate_watchers(watchers_data, work_package_data)

            assert mock_check.call_count == 2
            assert work_package_data["watcher_ids"] == [101, 102]
            assert len(result["warnings"]) == 0


# ============================================================================
# Edge Case and Error Handling Tests
# ============================================================================


class TestEdgeCasesAndErrorHandling:
    """Test edge cases and error handling scenarios."""

    def test_unicode_usernames(self, basic_migrator) -> None:
        """Test handling of unicode characters in usernames."""
        migrator = basic_migrator
        unicode_username = "用户名_émojis_🦄"

        # Should not crash with unicode usernames
        assert migrator.is_mapping_stale(unicode_username)  # Missing, so stale

        mapping = migrator.check_and_handle_staleness(
            unicode_username,
            raise_on_stale=False,
        )
        assert mapping is None

    def test_very_large_mapping_dataset(self, basic_migrator) -> None:
        """Test performance with large number of mappings."""
        migrator = basic_migrator
        current_time = datetime.now(tz=UTC)

        # Add many mappings
        for i in range(1000):
            username = f"user_{i}"
            is_stale = i % 2 == 0  # Half stale, half fresh
            refresh_time = current_time - timedelta(hours=2 if is_stale else 0)

            migrator.enhanced_user_mappings[username] = UserAssociationMapping(
                jira_username=username,
                jira_user_id=f"id_{i}",
                jira_display_name=f"User {i}",
                jira_email=f"user{i}@example.com",
                openproject_user_id=i,
                openproject_username=username,
                openproject_email=f"user{i}@example.com",
                mapping_status="mapped",
                fallback_user_id=None,
                metadata={},
                lastRefreshed=refresh_time.isoformat(),
            )

        # Test bulk operations
        stale_mappings = migrator.detect_stale_mappings()
        assert len(stale_mappings) == 500  # Half should be stale

        validation_result = migrator.validate_mapping_freshness()
        assert validation_result["total_checked"] == 1000
        assert validation_result["stale_mappings"] == 500
        assert validation_result["fresh_mappings"] == 500

    def test_concurrent_modifications(self, basic_migrator) -> None:
        """Test handling of concurrent mapping modifications."""
        migrator = basic_migrator

        # Simulate concurrent modification during staleness check
        migrator.enhanced_user_mappings.copy()

        def modify_during_check(*args, **kwargs) -> bool:
            # Modify mappings during iteration
            migrator.enhanced_user_mappings.clear()
            return True

        migrator.enhanced_user_mappings["test_user"] = UserAssociationMapping(
            jira_username="test_user",
            jira_user_id="test_id",
            jira_display_name="Test User",
            jira_email="test@example.com",
            openproject_user_id=1,
            openproject_username="test_user",
            openproject_email="test@example.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=None,
        )

        with patch.object(
            migrator,
            "is_mapping_stale",
            side_effect=modify_during_check,
        ):
            # Should handle concurrent modification gracefully
            stale_mappings = migrator.detect_stale_mappings()
            assert isinstance(stale_mappings, dict)

    def test_memory_efficiency_batch_operations(self, basic_migrator) -> None:
        """Test memory efficiency of batch operations."""
        migrator = basic_migrator

        # Test that batch operations don't hold unnecessary references
        import gc

        # Add some mappings
        for i in range(10):
            username = f"mem_test_{i}"
            migrator.enhanced_user_mappings[username] = UserAssociationMapping(
                jira_username=username,
                jira_user_id=f"id_{i}",
                jira_display_name=f"User {i}",
                jira_email=f"user{i}@example.com",
                openproject_user_id=i,
                openproject_username=username,
                openproject_email=f"user{i}@example.com",
                mapping_status="mapped",
                fallback_user_id=None,
                metadata={},
                lastRefreshed=None,  # Make them stale
            )

        # Mock refresh to return None (simulating failures)
        with patch.object(migrator, "refresh_user_mapping", return_value=None):
            initial_objects = len(gc.get_objects())

            # Run batch refresh
            result = migrator.batch_refresh_stale_mappings()

            # Force garbage collection
            gc.collect()

            final_objects = len(gc.get_objects())

            # Should not have significantly increased object count
            # Allow some growth for result structures
            assert final_objects - initial_objects < 100

            # Verify results structure is reasonable
            assert result["total_stale"] == 10
            assert result["refresh_failed"] == 10
