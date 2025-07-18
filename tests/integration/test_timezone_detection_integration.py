#!/usr/bin/env python3
"""Integration tests for timezone detection fix in EnhancedTimestampMigrator."""

import json
from datetime import datetime, timezone
from unittest.mock import Mock, patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.utils.enhanced_timestamp_migrator import EnhancedTimestampMigrator
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient


class TestTimezoneDetectionIntegration:
    """Integration tests for timezone detection functionality."""

    @pytest.fixture
    def mock_jira_client(self):
        """Create a mock Jira client with realistic behavior."""
        mock_client = Mock(spec=JiraClient)
        
        # Setup nested jira attribute
        mock_client.jira = Mock()
        mock_client.jira.server_info = Mock()
        
        # Setup other common methods
        mock_client.get_projects = Mock(return_value=[])
        mock_client.get_custom_fields = Mock(return_value=[])
        
        return mock_client

    @pytest.fixture
    def mock_op_client(self):
        """Create a mock OpenProject client."""
        mock_client = Mock(spec=OpenProjectClient)
        
        # Setup common methods
        mock_client.get = Mock()
        mock_client.post = Mock()
        mock_client.patch = Mock()
        
        return mock_client

    @pytest.fixture
    def sample_jira_issue_berlin(self):
        """Create a sample Jira issue with Berlin timezone timestamps."""
        issue = Mock()
        issue.key = "PROJ-123"
        issue.fields = Mock()
        
        # Berlin time (CET/CEST) timestamps
        issue.fields.created = "2023-07-15T14:30:00.000+0200"  # CEST (summer time)
        issue.fields.updated = "2023-07-16T09:45:30.000+0200"
        issue.fields.duedate = "2023-07-20"
        issue.fields.resolutiondate = "2023-07-18T16:20:15.000+0200"
        
        # Custom datetime fields
        issue.fields.customfield_10001 = "2023-07-17T12:00:00.000+0200"
        issue.fields.customfield_10002 = "2023-07-19"
        
        return issue

    @pytest.fixture
    def sample_jira_issue_utc(self):
        """Create a sample Jira issue with UTC timestamps (legacy behavior)."""
        issue = Mock()
        issue.key = "PROJ-124"
        issue.fields = Mock()
        
        # UTC timestamps (what was incorrectly migrated before)
        issue.fields.created = "2023-07-15T12:30:00.000+0000"
        issue.fields.updated = "2023-07-16T07:45:30.000+0000"
        issue.fields.duedate = "2023-07-20"
        issue.fields.resolutiondate = "2023-07-18T14:20:15.000+0000"
        
        return issue

    def test_timezone_detection_from_jira_server_info(self, mock_jira_client, mock_op_client):
        """Test that timezone is correctly detected from Jira server info."""
        # Mock Jira server returning Berlin timezone
        mock_jira_client.jira.server_info.return_value = {
            "version": "8.20.10",
            "serverTimeZone": {
                "timeZoneId": "Europe/Berlin"
            },
            "serverTime": "2023-07-15T14:30:00.000+0200"
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client
            )
            
            # Verify correct timezone was detected
            assert migrator.jira_timezone == "Europe/Berlin"
            
            # Verify server_info was called correctly
            mock_jira_client.jira.server_info.assert_called_once()

    def test_timezone_detection_with_server_info_error(self, mock_jira_client, mock_op_client):
        """Test fallback behavior when server_info fails."""
        # Mock server_info failure
        mock_jira_client.jira.server_info.side_effect = Exception("Connection timeout")
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client
            )
            
            # Should fallback to UTC
            assert migrator.jira_timezone == "UTC"
            
            # Should log warning about fallback
            mock_config.logger.warning.assert_called()

    def test_timezone_detection_with_client_not_connected(self, mock_jira_client, mock_op_client):
        """Test handling when JiraClient.jira is None (not connected)."""
        # Simulate client not connected
        mock_jira_client.jira = None
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client
            )
            
            # Should fallback to UTC
            assert migrator.jira_timezone == "UTC"
            
            # Should log warning
            mock_config.logger.warning.assert_called()

    def test_full_migration_pipeline_with_berlin_timezone(
        self, mock_jira_client, mock_op_client, sample_jira_issue_berlin
    ):
        """Test full migration pipeline with Berlin timezone detection."""
        # Mock Jira server returning Berlin timezone
        mock_jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Europe/Berlin"}
        }
        
        # Mock OpenProject work package creation
        mock_op_client.post.return_value = {
            "id": 1,
            "subject": "Migrated issue",
            "createdAt": "2023-07-15T14:30:00+02:00"
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client
            )
            
            # Process the issue
            result = migrator.migrate_timestamps(sample_jira_issue_berlin)
            
            # Verify timezone was correctly detected
            assert migrator.jira_timezone == "Europe/Berlin"
            
            # Verify migration result contains correct timezone information
            assert result is not None
            assert result.jira_timezone == "Europe/Berlin"

    def test_timestamp_transformation_with_correct_timezone(
        self, mock_jira_client, mock_op_client, sample_jira_issue_berlin
    ):
        """Test that timestamps are correctly transformed with detected timezone."""
        # Mock Berlin timezone detection
        mock_jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Europe/Berlin"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client
            )
            
            # Test timestamp transformation
            jira_timestamp = "2023-07-15T14:30:00.000+0200"  # Berlin summer time
            
            # Transform timestamp
            transformed = migrator._transform_timestamp_field(jira_timestamp)
            
            # Should preserve the timezone information
            assert "+02:00" in transformed
            
            # Parse and verify
            dt = datetime.fromisoformat(transformed.replace('Z', '+00:00'))
            berlin_tz = ZoneInfo("Europe/Berlin")
            
            # The timestamp should represent the same moment in time
            original_dt = datetime.fromisoformat(jira_timestamp)
            assert dt.astimezone(berlin_tz) == original_dt

    def test_timezone_mapping_functionality(self, mock_jira_client, mock_op_client):
        """Test timezone mapping for common abbreviations."""
        test_cases = [
            ("PST", "America/Los_Angeles"),
            ("EST", "America/New_York"),
            ("CET", "Europe/Paris"),
            ("GMT", "UTC"),
            ("JST", "Asia/Tokyo"),
        ]
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            for jira_tz, expected_tz in test_cases:
                # Reset mock for each test case
                mock_jira_client.jira.server_info.reset_mock()
                mock_jira_client.jira.server_info.return_value = {
                    "serverTimeZone": {"timeZoneId": jira_tz}
                }
                
                migrator = EnhancedTimestampMigrator(
                    jira_client=mock_jira_client,
                    op_client=mock_op_client
                )
                
                assert migrator.jira_timezone == expected_tz, f"Failed mapping {jira_tz} -> {expected_tz}"

    def test_integration_with_work_package_migration(self, mock_jira_client, mock_op_client):
        """Test integration with WorkPackageMigration class."""
        # Mock Berlin timezone detection
        mock_jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Europe/Berlin"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            # Test that WorkPackageMigration can use the fixed migrator
            from src.migrations.work_package_migration import WorkPackageMigration
            
            with patch.object(WorkPackageMigration, '_get_enhanced_timestamp_migrator') as mock_get_migrator:
                migrator = EnhancedTimestampMigrator(
                    jira_client=mock_jira_client,
                    op_client=mock_op_client
                )
                mock_get_migrator.return_value = migrator
                
                # Create work package migration instance
                wp_migration = WorkPackageMigration(
                    jira_client=mock_jira_client,
                    op_client=mock_op_client
                )
                
                # Verify the migrator was created with correct timezone
                enhanced_migrator = wp_migration._get_enhanced_timestamp_migrator()
                assert enhanced_migrator.jira_timezone == "Europe/Berlin"

    def test_error_resilience_with_malformed_server_info(self, mock_jira_client, mock_op_client):
        """Test error resilience with various malformed server info responses."""
        malformed_responses = [
            {},  # Empty response
            {"version": "8.0.0"},  # Missing serverTimeZone
            {"serverTimeZone": "Invalid format"},  # Wrong format
            {"serverTimeZone": {"wrongField": "Europe/Berlin"}},  # Missing timeZoneId
            {"serverTimeZone": {"timeZoneId": ""}},  # Empty timezone
            {"serverTimeZone": {"timeZoneId": None}},  # Null timezone
        ]
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            for malformed_response in malformed_responses:
                # Reset mock
                mock_jira_client.jira.server_info.reset_mock()
                mock_jira_client.jira.server_info.return_value = malformed_response
                mock_config.logger.reset_mock()
                
                migrator = EnhancedTimestampMigrator(
                    jira_client=mock_jira_client,
                    op_client=mock_op_client
                )
                
                # Should fallback to UTC for all malformed responses
                assert migrator.jira_timezone == "UTC", f"Failed for response: {malformed_response}"
                
                # Should log warning for malformed responses (except empty which logs different message)
                if malformed_response:  # Only check for non-empty responses
                    mock_config.logger.warning.assert_called()

    def test_backward_compatibility_with_existing_migrations(self, mock_jira_client, mock_op_client):
        """Test that the fix is backward compatible with existing migration code."""
        # Mock timezone detection
        mock_jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "America/New_York"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client
            )
            
            # Verify all existing methods still work
            assert hasattr(migrator, 'migrate_timestamps')
            assert hasattr(migrator, '_transform_timestamp_field')
            assert hasattr(migrator, 'jira_timezone')
            assert hasattr(migrator, 'target_timezone')
            
            # Verify the detected timezone is used
            assert migrator.jira_timezone == "America/New_York"

    def test_performance_with_multiple_timezone_detections(self, mock_jira_client, mock_op_client):
        """Test that timezone detection doesn't impact performance with multiple migrator instances."""
        # Mock timezone detection
        mock_jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Europe/London"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            # Create multiple migrator instances
            migrators = []
            for i in range(5):
                migrator = EnhancedTimestampMigrator(
                    jira_client=mock_jira_client,
                    op_client=mock_op_client
                )
                migrators.append(migrator)
            
            # Verify all have correct timezone
            for migrator in migrators:
                assert migrator.jira_timezone == "Europe/London"
            
            # Verify server_info was called for each instance (no caching by default)
            assert mock_jira_client.jira.server_info.call_count == 5

    def test_integration_with_dependency_injection(self, mock_jira_client, mock_op_client):
        """Test that the fix works correctly with dependency injection from Task 33."""
        # Mock timezone detection
        mock_jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Asia/Singapore"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            # Simulate dependency injection pattern from Task 33
            # JiraClient should have jira property set after connection
            assert mock_jira_client.jira is not None
            
            # Create migrator with injected clients
            migrator = EnhancedTimestampMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client
            )
            
            # Verify timezone detection worked with injected client
            assert migrator.jira_timezone == "Asia/Singapore"
            
            # Verify the correct method was called on the injected client
            mock_jira_client.jira.server_info.assert_called_once() 