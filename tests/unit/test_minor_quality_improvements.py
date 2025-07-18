#!/usr/bin/env python3
"""Tests for minor quality improvements in Enhanced User Association Migrator."""

import pytest
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path

from src.config_loader import ConfigLoader
from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator


class TestConfigLoaderSecurityLogging:
    """Tests for secure exception logging in ConfigLoader."""

    def test_exception_logging_does_not_expose_sensitive_path_info(self, temp_dir):
        """Test that exception logging only shows exception type, not sensitive paths."""
        # Arrange
        config_path = temp_dir / "config.yaml"
        config_path.write_text("""
jira:
  url: "https://example.atlassian.net"
  
openproject:
  url: "https://openproject.example.com"
  
migration:
  batch_size: 50
""")
        
        # Create a config loader with a secret path that will fail to read
        loader = ConfigLoader(config_path)
        
        # Mock environment to trigger Docker secret path reading
        with patch.dict('os.environ', {
            'POSTGRES_PASSWORD_FILE': '/nonexistent/secret/path/postgres_password.txt'
        }):
            with patch('src.config_loader.config_logger') as mock_logger:
                # Act
                loader._load_database_config()
                
                # Assert - Warning should log only exception type, not full exception with path
                mock_logger.warning.assert_called_once()
                warning_call = mock_logger.warning.call_args
                
                # Should be called with exception class name, not full exception
                assert warning_call[0][0] == "Failed to read Docker secret: %s"
                assert warning_call[0][1] in ['FileNotFoundError', 'IOError', 'OSError', 'PermissionError']
                
                # Verify that the sensitive path is NOT in the log message
                logged_message = warning_call[0][1]
                assert '/nonexistent/secret/path' not in logged_message
                assert 'postgres_password.txt' not in logged_message

    def test_successful_secret_loading_logs_generic_message(self, temp_dir):
        """Test that successful secret loading doesn't expose the secret content."""
        # Arrange
        secret_file = temp_dir / "postgres_password.txt"
        secret_file.write_text("super_secret_password")
        
        config_path = temp_dir / "config.yaml"
        config_path.write_text("""
jira:
  url: "https://example.atlassian.net"
  
openproject:
  url: "https://openproject.example.com"
""")
        
        loader = ConfigLoader(config_path)
        
        # Mock environment to use our test secret file
        with patch.dict('os.environ', {
            'POSTGRES_PASSWORD_FILE': str(secret_file)
        }):
            with patch('src.config_loader.config_logger') as mock_logger:
                # Act
                loader._load_database_config()
                
                # Assert - Should log success without exposing password content
                debug_calls = [call for call in mock_logger.debug.call_args_list 
                              if 'Successfully loaded PostgreSQL password' in str(call)]
                assert len(debug_calls) > 0
                
                # Verify password content is not in any log message
                for call in mock_logger.debug.call_args_list:
                    log_message = str(call)
                    assert 'super_secret_password' not in log_message


class TestDurationParsingWhitespaceHandling:
    """Tests for defensive whitespace handling in duration parsing."""

    @pytest.fixture
    def migrator_instance(self, mock_clients):
        """Create a minimal migrator instance for testing."""
        jira_client, op_client = mock_clients
        migrator = EnhancedUserAssociationMigrator.__new__(EnhancedUserAssociationMigrator)
        migrator.jira_client = jira_client
        migrator.op_client = op_client
        migrator.logger = MagicMock()
        return migrator

    @pytest.mark.parametrize("duration_with_whitespace, expected_seconds", [
        (" 1h ", 3600),      # Leading and trailing spaces
        ("\t30m\t", 1800),   # Leading and trailing tabs
        ("\n2d\n", 172800),  # Leading and trailing newlines
        ("  5s  ", 5),       # Multiple spaces
        (" 24h", 86400),     # Leading space only
        ("1h ", 3600),       # Trailing space only
        ("   1m   ", 60),    # Multiple leading and trailing spaces
        ("\r\n15m\r\n", 900), # Windows line endings
    ])
    def test_parse_duration_handles_whitespace_defensively(self, migrator_instance, duration_with_whitespace, expected_seconds):
        """Test that duration parsing correctly handles various whitespace patterns."""
        # Act
        result = migrator_instance._parse_duration(duration_with_whitespace)
        
        # Assert
        assert result == expected_seconds

    @pytest.mark.parametrize("invalid_duration_with_whitespace", [
        " ",           # Only whitespace
        "\t\n",        # Only tabs and newlines
        "  1x  ",      # Invalid unit with whitespace
        " abc ",       # Invalid format with whitespace
        "  ",          # Multiple spaces only
        "\t",          # Only tab
        "\n",          # Only newline
    ])
    def test_parse_duration_rejects_whitespace_only_or_invalid_with_whitespace(self, migrator_instance, invalid_duration_with_whitespace):
        """Test that whitespace-only or invalid formats are still properly rejected."""
        # Act & Assert
        with pytest.raises(ValueError):
            migrator_instance._parse_duration(invalid_duration_with_whitespace)

    def test_parse_duration_empty_string_after_strip_raises_error(self, migrator_instance):
        """Test that strings that become empty after stripping raise appropriate error."""
        # Act & Assert
        with pytest.raises(ValueError, match="Duration string cannot be empty"):
            migrator_instance._parse_duration("   ")  # Only spaces

    def test_parse_duration_preserves_original_error_messages_for_invalid_formats(self, migrator_instance):
        """Test that original error messages are preserved for truly invalid formats after whitespace handling."""
        # Act & Assert
        with pytest.raises(ValueError, match="Invalid duration format"):
            migrator_instance._parse_duration("  1x  ")  # Invalid unit, even after strip


class TestQualityImprovementIntegration:
    """Integration tests for quality improvements."""

    def test_config_loader_and_migrator_quality_improvements_work_together(self, temp_dir, mock_clients):
        """Integration test ensuring all quality improvements work together."""
        # Arrange
        config_path = temp_dir / "config.yaml"
        config_path.write_text("""
jira:
  url: "https://example.atlassian.net"
  
openproject:
  url: "https://openproject.example.com"
  
migration:
  mapping:
    refresh_interval: "  24h  "  # Duration with whitespace to test parsing
    fallback_strategy: "skip"
""")
        
        jira_client, op_client = mock_clients
        
        # Act - Create migrator which will load config and parse duration
        with patch('src.utils.enhanced_user_association_migrator.config') as mock_config:
            mock_config.migration_config = {
                "mapping": {
                    "refresh_interval": "  24h  ",  # Whitespace around duration
                    "fallback_strategy": "skip",
                }
            }
            mock_config.get_path.return_value.exists.return_value = False

            migrator = EnhancedUserAssociationMigrator.__new__(EnhancedUserAssociationMigrator)
            migrator.jira_client = jira_client
            migrator.op_client = op_client
            migrator.logger = MagicMock()
            
            # This should handle whitespace in duration parsing
            migrator._load_staleness_config()
            
            # Assert
            assert migrator.refresh_interval_seconds == 86400  # 24 hours in seconds
            assert migrator.fallback_strategy == "skip" 