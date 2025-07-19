"""
Comprehensive tests for validation and fallback strategies in Enhanced User Association Migrator.

This test suite covers:
1. User validation logic (_validate_refreshed_user)
2. Fallback strategy routing (_apply_fallback_strategy)
3. Skip fallback execution (_execute_skip_fallback)
4. Admin assignment fallback (_execute_assign_admin_fallback)
5. Placeholder creation fallback (_execute_create_placeholder_fallback)
6. Integration with refresh_user_mapping
7. Edge cases and error handling
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone as UTC

from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator


# Test fixtures and utilities
@pytest.fixture
def mock_config():
    """Mock configuration for testing."""
    config = Mock()
    config.migration_config = {
        "fallback_strategy": "skip",
        "fallback_admin_user_id": None,
        "staleness_config": {
            "refresh_interval": "1d",
            "fallback_strategy": "skip",
            "fallback_admin_user_id": None
        },
        "fallback_users": {
            "admin_user_id": 1,
            "system_user_id": 2,
            "deleted_user_id": 3
        }
    }
    config.get_path.return_value = "/mock/path/enhanced_user_mappings.json"
    return config


@pytest.fixture
def mock_clients():
    """Mock JiraClient and OpenProjectClient for testing."""
    jira_client = Mock()
    op_client = Mock()
    return jira_client, op_client


@pytest.fixture 
def migrator_with_validation(mock_config, mock_clients):
    """Create migrator instance configured for validation and fallback testing."""
    jira_client, op_client = mock_clients
    
    mock_file = Mock()
    mock_file.__enter__ = Mock(return_value=mock_file)
    mock_file.__exit__ = Mock(return_value=None)
    
    with patch('pathlib.Path.exists', return_value=True), \
         patch('pathlib.Path.open', return_value=mock_file), \
         patch('json.load', return_value={}), \
         patch('src.config.migration_config', mock_config.migration_config):
        
        migrator = EnhancedUserAssociationMigrator(
            jira_client=jira_client,
            op_client=op_client
        )
        
    # Setup test state
    migrator.enhanced_user_mappings = {}
    migrator.metrics_collector = Mock()
    migrator.fallback_strategy = "skip"
    migrator.admin_user_id = None
    
    return migrator


@pytest.fixture
def sample_jira_user_data():
    """Sample Jira user data for testing."""
    return {
        "active": True,
        "displayName": "John Doe",
        "emailAddress": "john.doe@company.com",
        "accountId": "account123"
    }


@pytest.fixture
def sample_current_mapping():
    """Sample current mapping for validation testing."""
    return {
        "jira_username": "john.doe",
        "openproject_user_id": 42,
        "mapping_status": "mapped",
        "lastRefreshed": "2024-01-01T10:00:00Z",
        "metadata": {
            "jira_email": "john.doe@company.com",
            "jira_account_id": "account123",
            "jira_active": True
        }
    }


class TestValidateRefreshedUser:
    """Test the _validate_refreshed_user method."""
    
    def test_validate_active_user_with_consistent_data(self, migrator_with_validation, sample_jira_user_data, sample_current_mapping):
        """Test validation passes for active user with consistent data."""
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", sample_jira_user_data, sample_current_mapping
        )
        
        assert result["is_valid"] is True
        assert result["reason"] == "validation_passed"
    
    def test_validate_inactive_user(self, migrator_with_validation, sample_jira_user_data, sample_current_mapping):
        """Test validation fails for inactive user."""
        sample_jira_user_data["active"] = False
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", sample_jira_user_data, sample_current_mapping
        )
        
        assert result["is_valid"] is False
        assert result["reason"] == "user_inactive_in_jira"
    
    def test_validate_missing_active_field_defaults_true(self, migrator_with_validation, sample_current_mapping):
        """Test missing active field defaults to True."""
        jira_data = {
            "displayName": "John Doe",
            "emailAddress": "john.doe@company.com",
            "accountId": "account123"
            # No "active" field
        }
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", jira_data, sample_current_mapping
        )
        
        assert result["is_valid"] is True
        assert result["reason"] == "validation_passed"
    
    def test_validate_email_mismatch(self, migrator_with_validation, sample_jira_user_data, sample_current_mapping):
        """Test validation fails for email mismatch."""
        sample_jira_user_data["emailAddress"] = "different.email@company.com"
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", sample_jira_user_data, sample_current_mapping
        )
        
        assert result["is_valid"] is False
        assert "email_mismatch" in result["reason"]
        assert "john.doe@company.com" in result["reason"]
        assert "different.email@company.com" in result["reason"]
    
    def test_validate_email_case_insensitive(self, migrator_with_validation, sample_jira_user_data, sample_current_mapping):
        """Test email validation is case insensitive."""
        sample_jira_user_data["emailAddress"] = "JOHN.DOE@COMPANY.COM"
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", sample_jira_user_data, sample_current_mapping
        )
        
        assert result["is_valid"] is True
        assert result["reason"] == "validation_passed"
    
    def test_validate_account_id_mismatch(self, migrator_with_validation, sample_jira_user_data, sample_current_mapping):
        """Test validation fails for account ID mismatch."""
        sample_jira_user_data["accountId"] = "different_account456"
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", sample_jira_user_data, sample_current_mapping
        )
        
        assert result["is_valid"] is False
        assert "account_id_mismatch" in result["reason"]
        assert "account123" in result["reason"]
        assert "different_account456" in result["reason"]
    
    def test_validate_missing_previous_email_allows_any(self, migrator_with_validation, sample_jira_user_data):
        """Test validation passes when no previous email to compare."""
        current_mapping = {
            "metadata": {
                "jira_account_id": "account123"
                # No jira_email field
            }
        }
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", sample_jira_user_data, current_mapping
        )
        
        assert result["is_valid"] is True
        assert result["reason"] == "validation_passed"
    
    def test_validate_missing_previous_account_id_allows_any(self, migrator_with_validation, sample_jira_user_data):
        """Test validation passes when no previous account ID to compare."""
        current_mapping = {
            "metadata": {
                "jira_email": "john.doe@company.com"
                # No jira_account_id field
            }
        }
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", sample_jira_user_data, current_mapping
        )
        
        assert result["is_valid"] is True
        assert result["reason"] == "validation_passed"
    
    def test_validate_missing_metadata_allows_any(self, migrator_with_validation, sample_jira_user_data):
        """Test validation passes when no metadata to compare."""
        current_mapping = {
            # No metadata field
        }
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", sample_jira_user_data, current_mapping
        )
        
        assert result["is_valid"] is True
        assert result["reason"] == "validation_passed"
    
    def test_validate_missing_current_email_allows_any(self, migrator_with_validation, sample_current_mapping):
        """Test validation passes when current Jira data has no email."""
        jira_data = {
            "active": True,
            "accountId": "account123"
            # No emailAddress field
        }
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", jira_data, sample_current_mapping
        )
        
        assert result["is_valid"] is True
        assert result["reason"] == "validation_passed"
    
    def test_validate_missing_current_account_id_allows_any(self, migrator_with_validation, sample_current_mapping):
        """Test validation passes when current Jira data has no account ID."""
        jira_data = {
            "active": True,
            "emailAddress": "john.doe@company.com"
            # No accountId field
        }
        
        result = migrator_with_validation._validate_refreshed_user(
            "john.doe", jira_data, sample_current_mapping
        )
        
        assert result["is_valid"] is True
        assert result["reason"] == "validation_passed"


class TestApplyFallbackStrategy:
    """Test the _apply_fallback_strategy routing method."""
    
    def test_apply_skip_fallback_strategy(self, migrator_with_validation):
        """Test fallback strategy routes to skip method."""
        migrator_with_validation.fallback_strategy = "skip"
        
        with patch.object(migrator_with_validation, '_execute_skip_fallback', return_value=None) as mock_skip:
            result = migrator_with_validation._apply_fallback_strategy(
                "test.user", {"active": False}, "user_inactive_in_jira"
            )
            
            mock_skip.assert_called_once_with(
                "test.user", "user_inactive_in_jira", {}
            )
            assert result is None
    
    def test_apply_assign_admin_fallback_strategy(self, migrator_with_validation, sample_jira_user_data):
        """Test fallback strategy routes to assign admin method."""
        migrator_with_validation.fallback_strategy = "assign_admin"
        expected_mapping = {"openproject_user_id": 1}
        
        with patch.object(migrator_with_validation, '_execute_assign_admin_fallback', return_value=expected_mapping) as mock_admin:
            result = migrator_with_validation._apply_fallback_strategy(
                "test.user", sample_jira_user_data, "validation_failed"
            )
            
            mock_admin.assert_called_once_with(
                "test.user", "validation_failed", {}, sample_jira_user_data
            )
            assert result == expected_mapping
    
    def test_apply_create_placeholder_fallback_strategy(self, migrator_with_validation, sample_jira_user_data):
        """Test fallback strategy routes to create placeholder method."""
        migrator_with_validation.fallback_strategy = "create_placeholder"
        expected_mapping = {"openproject_user_id": None}
        
        with patch.object(migrator_with_validation, '_execute_create_placeholder_fallback', return_value=expected_mapping) as mock_placeholder:
            result = migrator_with_validation._apply_fallback_strategy(
                "test.user", sample_jira_user_data, "validation_failed"
            )
            
            mock_placeholder.assert_called_once_with(
                "test.user", "validation_failed", {}, sample_jira_user_data
            )
            assert result == expected_mapping
    
    def test_apply_unknown_fallback_strategy_defaults_to_skip(self, migrator_with_validation, caplog):
        """Test unknown fallback strategy defaults to skip with error logging."""
        migrator_with_validation.fallback_strategy = "unknown_strategy"
        
        with patch.object(migrator_with_validation, '_execute_skip_fallback', return_value=None) as mock_skip:
            result = migrator_with_validation._apply_fallback_strategy(
                "test.user", None, "user_not_found"
            )
            
            mock_skip.assert_called_once_with(
                "test.user", "unknown_strategy_unknown_strategy", {}
            )
            assert result is None
            assert "Unknown fallback strategy: unknown_strategy" in caplog.text


class TestExecuteSkipFallback:
    """Test the _execute_skip_fallback method."""
    
    def test_skip_fallback_removes_existing_mapping(self, migrator_with_validation):
        """Test skip fallback removes user from mappings."""
        migrator_with_validation.enhanced_user_mappings["test.user"] = {"existing": "mapping"}
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'):
            result = migrator_with_validation._execute_skip_fallback(
                "test.user", "user_inactive", {}
            )
            
            assert "test.user" not in migrator_with_validation.enhanced_user_mappings
            assert result is None
    
    def test_skip_fallback_handles_missing_mapping(self, migrator_with_validation):
        """Test skip fallback handles user not in mappings gracefully."""
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'):
            result = migrator_with_validation._execute_skip_fallback(
                "missing.user", "user_inactive", {}
            )
            
            assert result is None
    
    def test_skip_fallback_updates_metrics(self, migrator_with_validation):
        """Test skip fallback updates metrics collector."""
        migrator_with_validation.enhanced_user_mappings["test.user"] = {"existing": "mapping"}
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'):
            migrator_with_validation._execute_skip_fallback(
                "test.user", "validation_failed", {}
            )
            
            migrator_with_validation.metrics_collector.increment_counter.assert_called_once_with(
                'mapping_fallback_total',
                tags={'fallback_strategy': 'skip', 'reason': 'validation_failed'}
            )
    
    def test_skip_fallback_handles_missing_metrics_collector(self, migrator_with_validation):
        """Test skip fallback handles missing metrics collector gracefully."""
        migrator_with_validation.metrics_collector = None
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'):
            result = migrator_with_validation._execute_skip_fallback(
                "test.user", "validation_failed", {}
            )
            
            assert result is None
    
    def test_skip_fallback_handles_save_error(self, migrator_with_validation, caplog):
        """Test skip fallback handles save errors gracefully."""
        migrator_with_validation.enhanced_user_mappings["test.user"] = {"existing": "mapping"}
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings', side_effect=IOError("Disk full")):
            result = migrator_with_validation._execute_skip_fallback(
                "test.user", "validation_failed", {}
            )
            
            assert result is None
            assert "Failed to save after skip fallback" in caplog.text
            assert "Disk full" in caplog.text


class TestExecuteAssignAdminFallback:
    """Test the _execute_assign_admin_fallback method."""
    
    @patch('src.utils.enhanced_user_association_migrator.datetime')
    def test_assign_admin_fallback_creates_admin_mapping(self, mock_datetime, migrator_with_validation, sample_jira_user_data):
        """Test assign admin fallback creates mapping to admin user."""
        mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T12:00:00Z"
        migrator_with_validation.admin_user_id = 999
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'):
            result = migrator_with_validation._execute_assign_admin_fallback(
                "test.user", "validation_failed", {}, sample_jira_user_data
            )
            
            assert result is not None
            assert result["openproject_user_id"] == 999
            assert result["mapping_status"] == "fallback_admin"
            assert result["metadata"]["fallback_strategy"] == "assign_admin"
            assert result["metadata"]["fallback_admin_user_id"] == 999
            assert result["metadata"]["needs_review"] is True
            assert result["metadata"]["jira_email"] == "john.doe@company.com"
    
    def test_assign_admin_fallback_without_admin_config_falls_back_to_skip(self, migrator_with_validation):
        """Test assign admin fallback falls back to skip when no admin configured."""
        migrator_with_validation.admin_user_id = None
        
        with patch.object(migrator_with_validation, '_execute_skip_fallback', return_value=None) as mock_skip:
            result = migrator_with_validation._execute_assign_admin_fallback(
                "test.user", "validation_failed", {}, {"active": False}
            )
            
            mock_skip.assert_called_once_with(
                "test.user", "no_admin_configured_validation_failed", {}
            )
            assert result is None
    
    def test_assign_admin_fallback_handles_none_jira_data(self, migrator_with_validation):
        """Test assign admin fallback handles None jira_user_data gracefully."""
        migrator_with_validation.admin_user_id = 999
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'), \
             patch('src.utils.enhanced_user_association_migrator.datetime') as mock_datetime:
            mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T12:00:00Z"
            
            result = migrator_with_validation._execute_assign_admin_fallback(
                "test.user", "user_not_found", {}, None
            )
            
            assert result["metadata"]["jira_active"] is False
            assert result["metadata"]["jira_display_name"] is None
            assert result["metadata"]["jira_email"] is None
            assert result["metadata"]["jira_account_id"] is None
    
    def test_assign_admin_fallback_updates_metrics(self, migrator_with_validation):
        """Test assign admin fallback updates metrics collector."""
        migrator_with_validation.admin_user_id = 999
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'), \
             patch('src.utils.enhanced_user_association_migrator.datetime'):
            migrator_with_validation._execute_assign_admin_fallback(
                "test.user", "validation_failed", {}, {"active": False}
            )
            
            migrator_with_validation.metrics_collector.increment_counter.assert_called_once_with(
                'mapping_fallback_total',
                tags={'fallback_strategy': 'assign_admin', 'reason': 'validation_failed'}
            )
    
    def test_assign_admin_fallback_handles_save_error(self, migrator_with_validation, caplog):
        """Test assign admin fallback handles save errors gracefully."""
        migrator_with_validation.admin_user_id = 999
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings', side_effect=json.JSONDecodeError("Invalid JSON", "", 0)), \
             patch('src.utils.enhanced_user_association_migrator.datetime'):
            result = migrator_with_validation._execute_assign_admin_fallback(
                "test.user", "validation_failed", {}, {"active": False}
            )
            
            assert result is not None  # Mapping is still returned
            assert "Failed to save after assign_admin fallback" in caplog.text


class TestExecuteCreatePlaceholderFallback:
    """Test the _execute_create_placeholder_fallback method."""
    
    @patch('src.utils.enhanced_user_association_migrator.datetime')
    def test_create_placeholder_fallback_creates_placeholder_mapping(self, mock_datetime, migrator_with_validation, sample_jira_user_data):
        """Test create placeholder fallback creates placeholder mapping."""
        mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T12:00:00Z"
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'):
            result = migrator_with_validation._execute_create_placeholder_fallback(
                "test.user", "validation_failed", {}, sample_jira_user_data
            )
            
            assert result is not None
            assert result["openproject_user_id"] is None
            assert result["mapping_status"] == "placeholder"
            assert result["metadata"]["fallback_strategy"] == "create_placeholder"
            assert result["metadata"]["needs_review"] is True
            assert result["metadata"]["is_placeholder"] is True
            assert result["metadata"]["jira_email"] == "john.doe@company.com"
            assert result["metadata"]["placeholder_created"] == "2024-01-01T12:00:00Z"
    
    def test_create_placeholder_fallback_handles_none_jira_data(self, migrator_with_validation):
        """Test create placeholder fallback handles None jira_user_data gracefully."""
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'), \
             patch('src.utils.enhanced_user_association_migrator.datetime') as mock_datetime:
            mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T12:00:00Z"
            
            result = migrator_with_validation._execute_create_placeholder_fallback(
                "test.user", "user_not_found", {}, None
            )
            
            assert result["metadata"]["jira_active"] is False
            assert result["metadata"]["jira_display_name"] is None
            assert result["metadata"]["jira_email"] is None
            assert result["metadata"]["jira_account_id"] is None
    
    def test_create_placeholder_fallback_preserves_current_metadata(self, migrator_with_validation):
        """Test create placeholder fallback preserves existing metadata."""
        current_mapping = {
            "metadata": {
                "existing_field": "existing_value",
                "another_field": 123
            }
        }
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'), \
             patch('src.utils.enhanced_user_association_migrator.datetime') as mock_datetime:
            mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T12:00:00Z"
            
            result = migrator_with_validation._execute_create_placeholder_fallback(
                "test.user", "validation_failed", current_mapping, {"active": True}
            )
            
            assert result["metadata"]["existing_field"] == "existing_value"
            assert result["metadata"]["another_field"] == 123
            assert result["metadata"]["fallback_strategy"] == "create_placeholder"
    
    def test_create_placeholder_fallback_updates_metrics(self, migrator_with_validation):
        """Test create placeholder fallback updates metrics collector."""
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'), \
             patch('src.utils.enhanced_user_association_migrator.datetime'):
            migrator_with_validation._execute_create_placeholder_fallback(
                "test.user", "validation_failed", {}, {"active": False}
            )
            
            migrator_with_validation.metrics_collector.increment_counter.assert_called_once_with(
                'mapping_fallback_total',
                tags={'fallback_strategy': 'create_placeholder', 'reason': 'validation_failed'}
            )
    
    def test_create_placeholder_fallback_handles_save_error(self, migrator_with_validation, caplog):
        """Test create placeholder fallback handles save errors gracefully."""
        with patch.object(migrator_with_validation, '_save_enhanced_mappings', side_effect=ValueError("Invalid data")), \
             patch('src.utils.enhanced_user_association_migrator.datetime'):
            result = migrator_with_validation._execute_create_placeholder_fallback(
                "test.user", "validation_failed", {}, {"active": False}
            )
            
            assert result is not None  # Mapping is still returned
            assert "Failed to save after create_placeholder fallback" in caplog.text


class TestRefreshUserMappingIntegration:
    """Test integration of validation and fallback with refresh_user_mapping."""
    
    def test_refresh_user_mapping_applies_fallback_on_validation_failure(self, migrator_with_validation):
        """Test refresh_user_mapping applies fallback when validation fails."""
        jira_user_data = {"active": False}  # Will fail validation
        migrator_with_validation.enhanced_user_mappings["test.user"] = {"existing": "mapping"}
        
        with patch.object(migrator_with_validation, '_get_jira_user_with_retry', return_value=jira_user_data), \
             patch.object(migrator_with_validation, '_apply_fallback_strategy', return_value=None) as mock_fallback, \
             patch.object(migrator_with_validation, '_save_enhanced_mappings'):
            
            result = migrator_with_validation.refresh_user_mapping("test.user")
            
            mock_fallback.assert_called_once_with(
                "test.user", jira_user_data, "user_inactive_in_jira"
            )
            assert result is None
    
    def test_refresh_user_mapping_applies_fallback_on_user_not_found(self, migrator_with_validation):
        """Test refresh_user_mapping applies fallback when user not found."""
        with patch.object(migrator_with_validation, '_get_jira_user_with_retry', return_value=None), \
             patch.object(migrator_with_validation, '_apply_fallback_strategy', return_value=None) as mock_fallback:
            
            result = migrator_with_validation.refresh_user_mapping("test.user")
            
            mock_fallback.assert_called_once_with(
                "test.user", None, "user_not_found"
            )
            assert result is None
    
    def test_refresh_user_mapping_continues_on_validation_success(self, migrator_with_validation, sample_jira_user_data):
        """Test refresh_user_mapping continues normally when validation passes."""
        migrator_with_validation.enhanced_user_mappings["test.user"] = {
            "metadata": {
                "jira_email": "john.doe@company.com",
                "jira_account_id": "account123"
            }
        }
        
        with patch.object(migrator_with_validation, '_get_jira_user_with_retry', return_value=sample_jira_user_data), \
             patch.object(migrator_with_validation, '_apply_fallback_strategy') as mock_fallback, \
             patch.object(migrator_with_validation, '_attempt_openproject_mapping') as mock_op_mapping, \
             patch.object(migrator_with_validation, '_save_enhanced_mappings'), \
             patch('src.utils.enhanced_user_association_migrator.datetime') as mock_datetime:
            
            mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T12:00:00Z"
            mock_op_mapping.return_value = {"mapping_status": "mapped"}
            
            result = migrator_with_validation.refresh_user_mapping("test.user")
            
            # Should not call fallback strategy
            mock_fallback.assert_not_called()
            
            # Should call OpenProject mapping attempt
            mock_op_mapping.assert_called_once()
            
            # Should return successful mapping
            assert result is not None
            assert result["mapping_status"] == "mapped"


class TestEdgeCasesAndErrorHandling:
    """Test edge cases and error handling scenarios."""
    
    def test_validation_with_empty_jira_data(self, migrator_with_validation):
        """Test validation handles empty jira user data."""
        result = migrator_with_validation._validate_refreshed_user(
            "test.user", {}, {}
        )
        
        # Should pass because missing active defaults to True
        assert result["is_valid"] is True
        assert result["reason"] == "validation_passed"
    
    def test_fallback_strategy_with_none_jira_data(self, migrator_with_validation):
        """Test fallback strategies handle None jira_user_data properly."""
        migrator_with_validation.fallback_strategy = "assign_admin"
        migrator_with_validation.admin_user_id = 999
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'), \
             patch('src.utils.enhanced_user_association_migrator.datetime'):
            result = migrator_with_validation._apply_fallback_strategy(
                "test.user", None, "user_not_found"
            )
            
            assert result is not None
            assert result["metadata"]["jira_active"] is False
    
    def test_fallback_strategy_logs_execution(self, migrator_with_validation, caplog):
        """Test fallback strategy execution is properly logged."""
        migrator_with_validation.fallback_strategy = "skip"
        
        with patch.object(migrator_with_validation, '_execute_skip_fallback', return_value=None):
            migrator_with_validation._apply_fallback_strategy(
                "test.user", {"active": False}, "user_inactive"
            )
            
            assert "Applying fallback strategy 'skip' for user test.user (reason: user_inactive)" in caplog.text
    
    def test_all_fallback_methods_store_mapping_in_enhanced_mappings(self, migrator_with_validation):
        """Test all fallback methods properly store mappings in enhanced_user_mappings."""
        # Test assign_admin
        migrator_with_validation.admin_user_id = 999
        
        with patch.object(migrator_with_validation, '_save_enhanced_mappings'), \
             patch('src.utils.enhanced_user_association_migrator.datetime'):
            
            result = migrator_with_validation._execute_assign_admin_fallback(
                "admin.user", "test", {}, {"active": True}
            )
            
            assert "admin.user" in migrator_with_validation.enhanced_user_mappings
            assert migrator_with_validation.enhanced_user_mappings["admin.user"] == result
            
            # Test create_placeholder
            result = migrator_with_validation._execute_create_placeholder_fallback(
                "placeholder.user", "test", {}, {"active": True}
            )
            
            assert "placeholder.user" in migrator_with_validation.enhanced_user_mappings
            assert migrator_with_validation.enhanced_user_mappings["placeholder.user"] == result 