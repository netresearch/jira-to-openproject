"""Tests for staleness detection functionality in EnhancedUserAssociationMigrator."""

import json
import pytest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
from types import SimpleNamespace

from src.utils.enhanced_user_association_migrator import (
    EnhancedUserAssociationMigrator,
    UserAssociationMapping,
    FallbackStrategy
)


class TestStalenessDetection:
    """Test suite for staleness detection functionality."""

    @pytest.fixture
    def mock_clients(self):
        """Create mock Jira and OpenProject clients."""
        jira_client = MagicMock()
        op_client = MagicMock()
        return jira_client, op_client

    @pytest.fixture
    def sample_config(self):
        """Sample mapping configuration."""
        return {
            "mapping": {
                "refresh_interval": "24h",
                "fallback_strategy": "skip",
                "fallback_admin_user_id": 123
            }
        }

    @pytest.fixture
    def fresh_mapping(self):
        """Create a fresh mapping entry."""
        current_time = datetime.now(tz=UTC).isoformat()
        return UserAssociationMapping(
            jira_username="john.doe",
            jira_user_id="jdoe-123",
            jira_display_name="John Doe",
            jira_email="john.doe@example.com",
            openproject_user_id=123,
            openproject_username="john.doe",
            openproject_email="john.doe@company.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={"jira_active": True, "openproject_active": True},
            lastRefreshed=current_time
        )

    @pytest.fixture
    def stale_mapping(self):
        """Create a stale mapping entry."""
        old_time = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat()
        return UserAssociationMapping(
            jira_username="jane.smith",
            jira_user_id="jsmith-456",
            jira_display_name="Jane Smith",
            jira_email="jane.smith@example.com",
            openproject_user_id=456,
            openproject_username="jane.smith",
            openproject_email="jane.smith@company.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={"jira_active": True, "openproject_active": True},
            lastRefreshed=old_time
        )

    @pytest.fixture
    def legacy_mapping(self):
        """Create a legacy mapping without lastRefreshed field."""
        return {
            "jira_username": "legacy.user",
            "jira_user_id": "legacy-789",
            "jira_display_name": "Legacy User",
            "jira_email": "legacy.user@example.com",
            "openproject_user_id": 789,
            "openproject_username": "legacy.user",
            "openproject_email": "legacy.user@company.com",
            "mapping_status": "mapped",
            "fallback_user_id": None,
            "metadata": {"jira_active": True, "openproject_active": True}
            # Note: No lastRefreshed field for backwards compatibility test
        }


class TestDurationParsing:
    """Test duration parsing functionality."""

    @pytest.fixture
    def migrator_instance(self, mock_clients):
        """Create a minimal migrator instance for testing."""
        jira_client, op_client = mock_clients
        with patch('src.config.migration_config', {}), \
             patch('src.config.get_path') as mock_path:
            mock_path.return_value = Path("/tmp/test")
            migrator = EnhancedUserAssociationMigrator.__new__(EnhancedUserAssociationMigrator)
            migrator.logger = MagicMock()
            return migrator

    @pytest.mark.parametrize("duration_str,expected_seconds", [
        ("1s", 1),
        ("30s", 30),
        ("1m", 60),
        ("30m", 1800),
        ("1h", 3600),
        ("24h", 86400),
        ("1d", 86400),
        ("7d", 604800),
        ("999h", 3596400),
    ])
    def test_parse_duration_valid_formats(self, migrator_instance, duration_str, expected_seconds):
        """Test parsing valid duration formats."""
        result = migrator_instance._parse_duration(duration_str)
        assert result == expected_seconds

    @pytest.mark.parametrize("invalid_duration", [
        "",              # Empty string
        "1",             # Missing unit
        "1x",            # Invalid unit
        "abc",           # Non-numeric
        "1.5h",          # Decimal not supported
        "-1h",           # Negative value
        "1 h",           # Space in between
        "1H",            # Uppercase unit
        "1sec",          # Full word unit
    ])
    def test_parse_duration_invalid_formats(self, migrator_instance, invalid_duration):
        """Test parsing invalid duration formats raises ValueError."""
        with pytest.raises(ValueError, match="Invalid duration format"):
            migrator_instance._parse_duration(invalid_duration)

    def test_parse_duration_empty_string(self, migrator_instance):
        """Test parsing empty string raises specific error."""
        with pytest.raises(ValueError, match="Duration string cannot be empty"):
            migrator_instance._parse_duration("")

    @pytest.mark.parametrize("zero_duration", ["0s", "0m", "0h", "0d"])
    def test_parse_duration_rejects_zero_value(self, migrator_instance, zero_duration):
        """Test that zero-value durations are correctly rejected."""
        with pytest.raises(ValueError, match="Duration must be positive"):
            migrator_instance._parse_duration(zero_duration)


class TestFallbackStrategyValidation:
    """Test fallback strategy validation."""

    @pytest.fixture
    def migrator_instance(self, mock_clients):
        """Create a minimal migrator instance for testing."""
        jira_client, op_client = mock_clients
        migrator = EnhancedUserAssociationMigrator.__new__(EnhancedUserAssociationMigrator)
        migrator.logger = MagicMock()
        return migrator

    @pytest.mark.parametrize("strategy", [
        "skip",
        "assign_admin", 
        "create_placeholder"
    ])
    def test_validate_fallback_strategy_valid(self, migrator_instance, strategy):
        """Test validation of valid fallback strategies."""
        result = migrator_instance._validate_fallback_strategy(strategy)
        assert result == strategy

    @pytest.mark.parametrize("invalid_strategy", [
        "SKIP",              # Wrong case
        "Skip",              # Wrong case
        "unknown",           # Invalid strategy
        "",                  # Empty string
        "assign-admin",      # Wrong format
        "create_user",       # Invalid strategy
    ])
    def test_validate_fallback_strategy_invalid(self, migrator_instance, invalid_strategy):
        """Test validation of invalid fallback strategies."""
        with pytest.raises(ValueError, match="Invalid fallback_strategy"):
            migrator_instance._validate_fallback_strategy(invalid_strategy)


class TestStalenessConfigurationLoading:
    """Test staleness configuration loading."""

    @pytest.fixture
    def migrator_instance(self, mock_clients):
        """Create a minimal migrator instance for testing."""
        jira_client, op_client = mock_clients
        migrator = EnhancedUserAssociationMigrator.__new__(EnhancedUserAssociationMigrator)
        migrator.logger = MagicMock()
        return migrator

    def test_load_staleness_config_defaults(self, migrator_instance):
        """Test loading staleness config with default values."""
        with patch('src.config.migration_config', {}):
            migrator_instance._load_staleness_config()
            
            assert migrator_instance.refresh_interval_seconds == 86400  # 24h
            assert migrator_instance.fallback_strategy == "skip"
            assert migrator_instance.admin_user_id is None

    def test_load_staleness_config_custom_values(self, migrator_instance):
        """Test loading staleness config with custom values."""
        config = {
            "mapping": {
                "refresh_interval": "2h",
                "fallback_strategy": "assign_admin",
                "fallback_admin_user_id": 456
            }
        }
        
        with patch('src.config.migration_config', config):
            migrator_instance._load_staleness_config()
            
            assert migrator_instance.refresh_interval_seconds == 7200  # 2h
            assert migrator_instance.fallback_strategy == "assign_admin"
            assert migrator_instance.admin_user_id == 456

    def test_load_staleness_config_assign_admin_without_user_id(self, migrator_instance):
        """Test warning when assign_admin strategy has no admin user ID."""
        config = {
            "mapping": {
                "fallback_strategy": "assign_admin"
                # No fallback_admin_user_id provided
            }
        }
        
        with patch('src.config.migration_config', config):
            migrator_instance._load_staleness_config()
            
            assert migrator_instance.fallback_strategy == "assign_admin"
            assert migrator_instance.admin_user_id is None
            migrator_instance.logger.warning.assert_called_once()

    def test_load_staleness_config_invalid_duration(self, migrator_instance):
        """Test fallback to defaults when duration is invalid."""
        config = {
            "mapping": {
                "refresh_interval": "invalid_duration"
            }
        }
        
        with patch('src.config.migration_config', config):
            migrator_instance._load_staleness_config()
            
            # Should fall back to defaults
            assert migrator_instance.refresh_interval_seconds == 86400  # 24h
            assert migrator_instance.fallback_strategy == "skip"
            migrator_instance.logger.warning.assert_called_once()

    def test_load_staleness_config_invalid_strategy(self, migrator_instance):
        """Test fallback to defaults when strategy is invalid."""
        config = {
            "mapping": {
                "fallback_strategy": "invalid_strategy"
            }
        }
        
        with patch('src.config.migration_config', config):
            migrator_instance._load_staleness_config()
            
            # Should fall back to defaults
            assert migrator_instance.refresh_interval_seconds == 86400  # 24h
            assert migrator_instance.fallback_strategy == "skip"
            migrator_instance.logger.warning.assert_called_once()


class TestStalenessDetectionLogic:
    """Test the core staleness detection logic."""

    @pytest.fixture
    def migrator_instance(self, mock_clients):
        """Create a configured migrator instance."""
        jira_client, op_client = mock_clients
        migrator = EnhancedUserAssociationMigrator.__new__(EnhancedUserAssociationMigrator)
        migrator.logger = MagicMock()
        migrator.enhanced_user_mappings = {}
        migrator.refresh_interval_seconds = 86400  # 24 hours
        return migrator

    def test_is_mapping_stale_missing_user(self, migrator_instance):
        """Test that missing users are considered stale."""
        result = migrator_instance.is_mapping_stale("nonexistent_user")
        assert result is True

    def test_is_mapping_stale_missing_timestamp(self, migrator_instance):
        """Test that mappings without lastRefreshed are considered stale."""
        migrator_instance.enhanced_user_mappings["test_user"] = UserAssociationMapping(
            jira_username="test_user",
            jira_user_id="test-123",
            jira_display_name="Test User",
            jira_email="test@example.com",
            openproject_user_id=123,
            openproject_username="test_user",
            openproject_email="test@company.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=None  # Missing timestamp
        )
        
        result = migrator_instance.is_mapping_stale("test_user")
        assert result is True

    def test_is_mapping_stale_fresh_mapping(self, migrator_instance):
        """Test that fresh mappings are not stale."""
        current_time = datetime.now(tz=UTC).isoformat()
        migrator_instance.enhanced_user_mappings["fresh_user"] = UserAssociationMapping(
            jira_username="fresh_user",
            jira_user_id="fresh-123",
            jira_display_name="Fresh User",
            jira_email="fresh@example.com",
            openproject_user_id=123,
            openproject_username="fresh_user",
            openproject_email="fresh@company.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=current_time
        )
        
        result = migrator_instance.is_mapping_stale("fresh_user")
        assert result is False

    def test_is_mapping_stale_stale_mapping(self, migrator_instance):
        """Test that old mappings are considered stale."""
        # Create a timestamp from 2 days ago (older than 24h refresh interval)
        old_time = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat()
        migrator_instance.enhanced_user_mappings["stale_user"] = UserAssociationMapping(
            jira_username="stale_user",
            jira_user_id="stale-123",
            jira_display_name="Stale User",
            jira_email="stale@example.com",
            openproject_user_id=123,
            openproject_username="stale_user",
            openproject_email="stale@company.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=old_time
        )
        
        result = migrator_instance.is_mapping_stale("stale_user")
        assert result is True

    def test_is_mapping_stale_boundary_case(self, migrator_instance):
        """Test mapping at the exact boundary of staleness."""
        # Create a timestamp exactly at the refresh interval boundary
        boundary_time = (datetime.now(tz=UTC) - timedelta(seconds=86400)).isoformat()
        migrator_instance.enhanced_user_mappings["boundary_user"] = UserAssociationMapping(
            jira_username="boundary_user",
            jira_user_id="boundary-123",
            jira_display_name="Boundary User",
            jira_email="boundary@example.com",
            openproject_user_id=123,
            openproject_username="boundary_user",
            openproject_email="boundary@company.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=boundary_time
        )
        
        result = migrator_instance.is_mapping_stale("boundary_user")
        # Should be stale (age equals refresh interval)
        assert result is True

    def test_is_mapping_stale_invalid_timestamp(self, migrator_instance):
        """Test that invalid timestamps are treated as stale."""
        migrator_instance.enhanced_user_mappings["invalid_user"] = UserAssociationMapping(
            jira_username="invalid_user",
            jira_user_id="invalid-123",
            jira_display_name="Invalid User",
            jira_email="invalid@example.com",
            openproject_user_id=123,
            openproject_username="invalid_user",
            openproject_email="invalid@company.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed="invalid-timestamp"
        )
        
        result = migrator_instance.is_mapping_stale("invalid_user")
        assert result is True
        migrator_instance.logger.warning.assert_called_once()

    @pytest.mark.parametrize("invalid_timestamp", [
        "not-a-date",
        "2024-13-01T00:00:00Z",  # Invalid month
        "2024-01-32T00:00:00Z",  # Invalid day
        "2024-01-01T25:00:00Z",  # Invalid hour
        "",                       # Empty string
        "2024-01-01",            # Missing time
    ])
    def test_is_mapping_stale_various_invalid_timestamps(self, migrator_instance, invalid_timestamp):
        """Test various invalid timestamp formats."""
        migrator_instance.enhanced_user_mappings["test_user"] = UserAssociationMapping(
            jira_username="test_user",
            jira_user_id="test-123",
            jira_display_name="Test User",
            jira_email="test@example.com",
            openproject_user_id=123,
            openproject_username="test_user",
            openproject_email="test@company.com",
            mapping_status="mapped",
            fallback_user_id=None,
            metadata={},
            lastRefreshed=invalid_timestamp
        )
        
        result = migrator_instance.is_mapping_stale("test_user")
        assert result is True


class TestBackwardsCompatibility:
    """Test backwards compatibility with existing cache files."""

    @pytest.fixture
    def migrator_instance(self, mock_clients):
        """Create a migrator instance for testing."""
        jira_client, op_client = mock_clients
        with patch('src.config.migration_config', {}):
            migrator = EnhancedUserAssociationMigrator.__new__(EnhancedUserAssociationMigrator)
            migrator.logger = MagicMock()
            migrator.enhanced_user_mappings = {}
            return migrator

    def test_load_legacy_cache_file(self, migrator_instance):
        """Test loading cache file without lastRefreshed fields."""
        legacy_cache = {
            "legacy_user": {
                "jira_username": "legacy_user",
                "jira_user_id": "legacy-123",
                "jira_display_name": "Legacy User",
                "jira_email": "legacy@example.com",
                "openproject_user_id": 123,
                "openproject_username": "legacy_user",
                "openproject_email": "legacy@company.com",
                "mapping_status": "mapped",
                "fallback_user_id": None,
                "metadata": {}
                # No lastRefreshed field
            }
        }
        
        mock_file_content = json.dumps(legacy_cache)
        
        with patch('src.config.get_path') as mock_path, \
             patch('builtins.open', mock_open(read_data=mock_file_content)), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('datetime.datetime') as mock_datetime:
            
            mock_path.return_value = Path("/tmp/test")
            mock_now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
            mock_datetime.now.return_value = mock_now
            mock_datetime.fromisoformat = datetime.fromisoformat
            
            migrator_instance._load_enhanced_mappings()
            
            # Check that legacy entry got a timestamp
            assert "legacy_user" in migrator_instance.enhanced_user_mappings
            mapping = migrator_instance.enhanced_user_mappings["legacy_user"]
            assert mapping["lastRefreshed"] == mock_now.isoformat()

    def test_load_mixed_cache_file(self, migrator_instance):
        """Test loading cache file with mix of legacy and new entries."""
        mixed_cache = {
            "legacy_user": {
                "jira_username": "legacy_user",
                "jira_user_id": "legacy-123",
                "jira_display_name": "Legacy User",
                "jira_email": "legacy@example.com",
                "openproject_user_id": 123,
                "openproject_username": "legacy_user",
                "openproject_email": "legacy@company.com",
                "mapping_status": "mapped",
                "fallback_user_id": None,
                "metadata": {}
                # No lastRefreshed field
            },
            "new_user": {
                "jira_username": "new_user",
                "jira_user_id": "new-456",
                "jira_display_name": "New User",
                "jira_email": "new@example.com",
                "openproject_user_id": 456,
                "openproject_username": "new_user",
                "openproject_email": "new@company.com",
                "mapping_status": "mapped",
                "fallback_user_id": None,
                "metadata": {},
                "lastRefreshed": "2024-01-14T12:00:00+00:00"  # Already has timestamp
            }
        }
        
        mock_file_content = json.dumps(mixed_cache)
        
        with patch('src.config.get_path') as mock_path, \
             patch('builtins.open', mock_open(read_data=mock_file_content)), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('datetime.datetime') as mock_datetime:
            
            mock_path.return_value = Path("/tmp/test")
            mock_now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
            mock_datetime.now.return_value = mock_now
            mock_datetime.fromisoformat = datetime.fromisoformat
            
            migrator_instance._load_enhanced_mappings()
            
            # Check that legacy entry got current timestamp
            legacy_mapping = migrator_instance.enhanced_user_mappings["legacy_user"]
            assert legacy_mapping["lastRefreshed"] == mock_now.isoformat()
            
            # Check that new entry kept its original timestamp
            new_mapping = migrator_instance.enhanced_user_mappings["new_user"]
            assert new_mapping["lastRefreshed"] == "2024-01-14T12:00:00+00:00" 