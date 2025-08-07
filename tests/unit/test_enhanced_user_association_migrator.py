#!/usr/bin/env python3
"""Unit tests for EnhancedUserAssociationMigrator."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.utils.enhanced_user_association_migrator import (
    EnhancedUserAssociationMigrator,
    UserAssociationMapping,
)
from tests.utils.mock_factory import (
    create_mock_jira_client,
    create_mock_openproject_client,
)


class TestEnhancedUserAssociationMigrator:
    """Test suite for EnhancedUserAssociationMigrator."""

    @pytest.fixture
    def temp_data_dir(self, tmp_path):
        """Create temporary data directory."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        return data_dir

    @pytest.fixture
    def mock_clients(self):
        """Create mock clients."""
        jira_client = create_mock_jira_client()
        op_client = create_mock_openproject_client()
        return jira_client, op_client

    @pytest.fixture
    def sample_user_mapping(self):
        """Create sample user mapping data."""
        return {
            "john.doe": 123,
            "jane.smith": 456,
            "deleted.user": None,
            "inactive.user": 789,
        }

    @pytest.fixture
    def sample_enhanced_mapping(self):
        """Create sample enhanced user mapping."""
        current_time = "2024-01-15T12:00:00+00:00"
        return {
            "john.doe": UserAssociationMapping(
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
                lastRefreshed=current_time,
            ),
            "jane.smith": UserAssociationMapping(
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
                lastRefreshed=current_time,
            ),
            "inactive.user": UserAssociationMapping(
                jira_username="inactive.user",
                jira_user_id="inactive-789",
                jira_display_name="Inactive User",
                jira_email="inactive@example.com",
                openproject_user_id=789,
                openproject_username="inactive.user",
                openproject_email="inactive@company.com",
                mapping_status="mapped",
                fallback_user_id=None,
                metadata={"jira_active": False, "openproject_active": False},
                lastRefreshed=current_time,
            ),
        }

    @pytest.fixture
    def sample_jira_issue(self):
        """Create sample Jira issue with user associations."""
        issue = Mock()
        issue.key = "TEST-123"
        issue.fields = Mock()

        # Assignee
        assignee = Mock()
        assignee.name = "john.doe"
        assignee.accountId = "jdoe-123"
        assignee.displayName = "John Doe"
        assignee.emailAddress = "john.doe@example.com"
        assignee.active = True
        issue.fields.assignee = assignee

        # Reporter
        reporter = Mock()
        reporter.name = "jane.smith"
        reporter.accountId = "jsmith-456"
        reporter.displayName = "Jane Smith"
        reporter.emailAddress = "jane.smith@example.com"
        reporter.active = True
        issue.fields.reporter = reporter

        # Creator
        creator = Mock()
        creator.name = "john.doe"
        creator.accountId = "jdoe-123"
        creator.displayName = "John Doe"
        creator.emailAddress = "john.doe@example.com"
        creator.active = True
        issue.fields.creator = creator

        # Watches
        watches = Mock()
        watches.watchCount = 2
        issue.fields.watches = watches

        return issue

    @pytest.fixture
    @patch("src.utils.enhanced_user_association_migrator.config")
    def migrator_with_mocks(
        self,
        mock_config,
        mock_clients,
        temp_data_dir,
        sample_user_mapping,
    ):
        """Create migrator instance with mocked dependencies."""
        mock_config.get_path.return_value = temp_data_dir
        mock_config.logger = Mock()

        jira_client, op_client = mock_clients

        # Setup OpenProject client to return fallback users
        op_client.get_users.side_effect = [
            [{"id": 1, "login": "admin"}],  # admin users
            [],  # system users
            [{"id": 999, "login": "migration_user"}],  # migration users
        ]

        return EnhancedUserAssociationMigrator(
            jira_client=jira_client,
            op_client=op_client,
            user_mapping=sample_user_mapping,
        )

    def test_initialization_with_user_mapping(self, migrator_with_mocks) -> None:
        """Test migrator initialization with provided user mapping."""
        assert migrator_with_mocks.user_mapping == {
            "john.doe": 123,
            "jane.smith": 456,
            "deleted.user": None,
            "inactive.user": 789,
        }
        assert migrator_with_mocks.fallback_users == {"admin": 1, "migration": 999}
        assert len(migrator_with_mocks._rails_operations_cache) == 0

    @patch("src.utils.enhanced_user_association_migrator.config")
    def test_initialization_loads_mapping_from_file(
        self,
        mock_config,
        mock_clients,
        temp_data_dir,
    ) -> None:
        """Test migrator loads user mapping from file when not provided."""
        mock_config.get_path.return_value = temp_data_dir
        mock_config.logger = Mock()

        # Create user mapping file
        mapping_file = temp_data_dir / "user_mapping.json"
        mapping_data = {"test.user": 100}
        with mapping_file.open("w") as f:
            json.dump(mapping_data, f)

        jira_client, op_client = mock_clients
        op_client.get_users.return_value = []

        migrator = EnhancedUserAssociationMigrator(
            jira_client=jira_client,
            op_client=op_client,
        )

        assert migrator.user_mapping == mapping_data

    @patch("src.utils.enhanced_user_association_migrator.config")
    def test_initialization_handles_missing_mapping_file(
        self,
        mock_config,
        mock_clients,
        temp_data_dir,
    ) -> None:
        """Test migrator handles missing user mapping file gracefully."""
        mock_config.get_path.return_value = temp_data_dir
        mock_config.logger = Mock()

        jira_client, op_client = mock_clients
        op_client.get_users.return_value = []

        migrator = EnhancedUserAssociationMigrator(
            jira_client=jira_client,
            op_client=op_client,
        )

        assert migrator.user_mapping == {}

    def test_create_enhanced_mappings(self, migrator_with_mocks, mock_clients) -> None:
        """Test creation of enhanced user mappings from basic mapping."""
        jira_client, op_client = mock_clients

        # Mock user info responses
        jira_client.get_user_info.side_effect = [
            {
                "accountId": "jdoe-123",
                "displayName": "John Doe",
                "emailAddress": "john.doe@example.com",
                "active": True,
            },
            {
                "accountId": "jsmith-456",
                "displayName": "Jane Smith",
                "emailAddress": "jane.smith@example.com",
                "active": True,
            },
            None,  # deleted.user
            {
                "accountId": "inactive-789",
                "displayName": "Inactive User",
                "emailAddress": "inactive@example.com",
                "active": False,
            },
        ]

        op_client.get_user.side_effect = [
            {
                "id": 123,
                "login": "john.doe",
                "mail": "john.doe@company.com",
                "status": 1,
            },
            {
                "id": 456,
                "login": "jane.smith",
                "mail": "jane.smith@company.com",
                "status": 1,
            },
            {
                "id": 789,
                "login": "inactive.user",
                "mail": "inactive@company.com",
                "status": 3,
            },
        ]

        # Trigger enhanced mapping creation
        migrator_with_mocks._create_enhanced_mappings()

        # Verify enhanced mappings created
        assert "john.doe" in migrator_with_mocks.enhanced_user_mappings
        john_mapping = migrator_with_mocks.enhanced_user_mappings["john.doe"]
        assert john_mapping["jira_username"] == "john.doe"
        assert john_mapping["openproject_user_id"] == 123
        assert john_mapping["mapping_status"] == "mapped"
        assert john_mapping["metadata"]["jira_active"] is True
        assert john_mapping["metadata"]["openproject_active"] is True

    def test_extract_user_associations_complete_issue(
        self,
        migrator_with_mocks,
        sample_jira_issue,
        mock_clients,
    ) -> None:
        """Test extracting user associations from complete Jira issue."""
        jira_client, _ = mock_clients

        # Mock watchers response
        jira_client.get_issue_watchers.return_value = [
            {
                "name": "watcher1",
                "accountId": "w1-123",
                "displayName": "Watcher One",
                "emailAddress": "w1@example.com",
                "active": True,
            },
            {
                "name": "watcher2",
                "accountId": "w2-456",
                "displayName": "Watcher Two",
                "emailAddress": "w2@example.com",
                "active": True,
            },
        ]

        associations = migrator_with_mocks._extract_user_associations(sample_jira_issue)

        # Verify assignee extraction
        assert associations["assignee"]["username"] == "john.doe"
        assert associations["assignee"]["account_id"] == "jdoe-123"
        assert associations["assignee"]["display_name"] == "John Doe"
        assert associations["assignee"]["active"] is True

        # Verify reporter extraction
        assert associations["reporter"]["username"] == "jane.smith"
        assert associations["reporter"]["account_id"] == "jsmith-456"

        # Verify creator extraction
        assert associations["creator"]["username"] == "john.doe"
        assert associations["creator"]["account_id"] == "jdoe-123"

        # Verify watchers extraction
        assert len(associations["watchers"]) == 2
        assert associations["watchers"][0]["username"] == "watcher1"
        assert associations["watchers"][1]["username"] == "watcher2"

    def test_extract_user_associations_minimal_issue(self, migrator_with_mocks) -> None:
        """Test extracting user associations from minimal Jira issue."""
        # Issue with no user associations
        issue = Mock()
        issue.key = "TEST-456"
        issue.fields = Mock()
        issue.fields.assignee = None
        issue.fields.reporter = None
        issue.fields.creator = None
        issue.fields.watches = None

        associations = migrator_with_mocks._extract_user_associations(issue)

        # Should handle None values gracefully
        assert "assignee" not in associations
        assert "reporter" not in associations
        assert "creator" not in associations
        assert associations["watchers"] == []

    def test_migrate_assignee_mapped_active_user(
        self,
        migrator_with_mocks,
        sample_enhanced_mapping,
    ) -> None:
        """Test migrating assignee with mapped active user."""
        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping

        assignee_data = {"username": "john.doe"}
        work_package_data = {}

        result = migrator_with_mocks._migrate_assignee(assignee_data, work_package_data)

        assert work_package_data["assigned_to_id"] == 123
        assert result["warnings"] == []

    def test_migrate_assignee_inactive_user_with_fallback(
        self,
        migrator_with_mocks,
        sample_enhanced_mapping,
    ) -> None:
        """Test migrating assignee with inactive user using fallback."""
        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping

        assignee_data = {"username": "inactive.user"}
        work_package_data = {}

        result = migrator_with_mocks._migrate_assignee(assignee_data, work_package_data)

        assert work_package_data["assigned_to_id"] == 999  # fallback user
        assert len(result["warnings"]) == 1
        assert "inactive" in result["warnings"][0]

    def test_migrate_assignee_unmapped_user_with_fallback(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test migrating assignee with unmapped user using fallback."""
        assignee_data = {"username": "unknown.user"}
        work_package_data = {}

        result = migrator_with_mocks._migrate_assignee(assignee_data, work_package_data)

        assert work_package_data["assigned_to_id"] == 999  # fallback user
        assert len(result["warnings"]) == 1
        assert "unmapped" in result["warnings"][0]

    def test_migrate_assignee_no_fallback_available(self, migrator_with_mocks) -> None:
        """Test migrating assignee when no fallback users available."""
        migrator_with_mocks.fallback_users = {}  # No fallback users

        assignee_data = {"username": "unknown.user"}
        work_package_data = {}

        result = migrator_with_mocks._migrate_assignee(assignee_data, work_package_data)

        assert "assigned_to_id" not in work_package_data
        assert len(result["warnings"]) == 1
        assert "no fallback available" in result["warnings"][0]

    def test_migrate_assignee_none_data(self, migrator_with_mocks) -> None:
        """Test migrating assignee with None data."""
        work_package_data = {}

        result = migrator_with_mocks._migrate_assignee(None, work_package_data)

        assert "assigned_to_id" not in work_package_data
        assert result["warnings"] == []

    def test_migrate_author_with_rails_preservation(
        self,
        migrator_with_mocks,
        sample_enhanced_mapping,
    ) -> None:
        """Test migrating author with Rails console preservation."""
        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping

        reporter_data = {"username": "jane.smith"}
        creator_data = {"username": "john.doe"}
        work_package_data = {"jira_key": "TEST-123"}

        result = migrator_with_mocks._migrate_author(
            reporter_data,
            creator_data,
            work_package_data,
            preserve_via_rails=True,
        )

        assert work_package_data["author_id"] == 456  # jane.smith
        assert result["warnings"] == []

        # Verify Rails operation was queued
        assert len(migrator_with_mocks._rails_operations_cache) == 1
        queued_op = migrator_with_mocks._rails_operations_cache[0]
        assert queued_op["type"] == "set_author"
        assert queued_op["jira_key"] == "TEST-123"
        assert queued_op["author_id"] == 456

    def test_migrate_author_without_rails_preservation(
        self,
        migrator_with_mocks,
        sample_enhanced_mapping,
    ) -> None:
        """Test migrating author without Rails console preservation."""
        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping

        reporter_data = {"username": "jane.smith"}
        work_package_data = {}

        result = migrator_with_mocks._migrate_author(
            reporter_data,
            None,
            work_package_data,
            preserve_via_rails=False,
        )

        assert work_package_data["author_id"] == 456
        assert result["warnings"] == []
        assert len(migrator_with_mocks._rails_operations_cache) == 0

    def test_migrate_author_prefer_reporter_over_creator(
        self,
        migrator_with_mocks,
        sample_enhanced_mapping,
    ) -> None:
        """Test that author migration prefers reporter over creator."""
        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping

        reporter_data = {"username": "jane.smith"}
        creator_data = {"username": "john.doe"}
        work_package_data = {}

        migrator_with_mocks._migrate_author(
            reporter_data,
            creator_data,
            work_package_data,
            preserve_via_rails=False,
        )

        # Should use reporter (jane.smith) not creator (john.doe)
        assert work_package_data["author_id"] == 456  # jane.smith

    def test_migrate_author_fallback_on_unmapped(self, migrator_with_mocks) -> None:
        """Test author migration uses fallback for unmapped user."""
        reporter_data = {"username": "unknown.user"}
        work_package_data = {}

        result = migrator_with_mocks._migrate_author(
            reporter_data,
            None,
            work_package_data,
            preserve_via_rails=False,
        )

        assert work_package_data["author_id"] == 999  # fallback user
        assert len(result["warnings"]) == 1
        assert "unmapped" in result["warnings"][0]

    def test_migrate_author_no_data_uses_fallback(self, migrator_with_mocks) -> None:
        """Test author migration uses fallback when no author data available."""
        work_package_data = {}

        result = migrator_with_mocks._migrate_author(
            None,
            None,
            work_package_data,
            preserve_via_rails=False,
        )

        assert work_package_data["author_id"] == 999  # fallback user
        assert len(result["warnings"]) == 1
        assert "No author data available" in result["warnings"][0]

    def test_migrate_watchers_mixed_users(
        self,
        migrator_with_mocks,
        sample_enhanced_mapping,
    ) -> None:
        """Test migrating watchers with mix of mapped, unmapped, and inactive users."""
        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping

        watchers_data = [
            {"username": "john.doe"},  # mapped active
            {"username": "jane.smith"},  # mapped active
            {"username": "inactive.user"},  # mapped inactive
            {"username": "unknown.user"},  # unmapped
            {"username": None},  # invalid
        ]
        work_package_data = {}

        result = migrator_with_mocks._migrate_watchers(watchers_data, work_package_data)

        # Should only include mapped active users
        assert work_package_data["watcher_ids"] == [123, 456]  # john.doe, jane.smith
        assert len(result["warnings"]) == 2  # inactive and unmapped warnings

    def test_migrate_watchers_no_valid_watchers(self, migrator_with_mocks) -> None:
        """Test migrating watchers when no valid watchers exist."""
        watchers_data = [
            {"username": "unknown1"},
            {"username": "unknown2"},
        ]
        work_package_data = {}

        result = migrator_with_mocks._migrate_watchers(watchers_data, work_package_data)

        assert "watcher_ids" not in work_package_data
        assert len(result["warnings"]) == 2

    def test_migrate_watchers_empty_list(self, migrator_with_mocks) -> None:
        """Test migrating empty watchers list."""
        work_package_data = {}

        result = migrator_with_mocks._migrate_watchers([], work_package_data)

        assert "watcher_ids" not in work_package_data
        assert result["warnings"] == []

    def test_get_fallback_user_priority_order(self, migrator_with_mocks) -> None:
        """Test fallback user selection follows priority order."""
        # Setup fallback users in different priority
        migrator_with_mocks.fallback_users = {
            "system": 1,
            "admin": 2,
            "migration": 3,
        }

        # Should return migration user (highest priority)
        fallback = migrator_with_mocks._get_fallback_user("any_role")
        assert fallback == 3

    def test_get_fallback_user_no_fallbacks(self, migrator_with_mocks) -> None:
        """Test fallback user when no fallbacks available."""
        migrator_with_mocks.fallback_users = {}

        fallback = migrator_with_mocks._get_fallback_user("any_role")
        assert fallback is None

    def test_queue_rails_author_operation(self, migrator_with_mocks) -> None:
        """Test queuing Rails operation for author preservation."""
        author_data = {"username": "john.doe", "display_name": "John Doe"}

        migrator_with_mocks._queue_rails_author_operation("TEST-123", 456, author_data)

        assert len(migrator_with_mocks._rails_operations_cache) == 1
        operation = migrator_with_mocks._rails_operations_cache[0]
        assert operation["type"] == "set_author"
        assert operation["jira_key"] == "TEST-123"
        assert operation["author_id"] == 456
        assert operation["author_metadata"] == author_data
        assert "timestamp" in operation

    def test_generate_author_preservation_script(self, migrator_with_mocks) -> None:
        """Test generation of Rails script for author preservation."""
        # Queue some operations
        migrator_with_mocks._queue_rails_author_operation(
            "TEST-123",
            456,
            {"username": "john.doe"},
        )
        migrator_with_mocks._queue_rails_author_operation(
            "TEST-456",
            789,
            {"username": "jane.smith"},
        )

        # Mock work package mapping
        work_package_mapping = {
            "wp1": {"jira_key": "TEST-123", "openproject_id": 1001},
            "wp2": {"jira_key": "TEST-456", "openproject_id": 1002},
        }

        script = migrator_with_mocks._generate_author_preservation_script(
            work_package_mapping,
        )

        # Verify script contains expected operations
        assert "WorkPackage.find(1001)" in script
        assert "wp.author_id = 456" in script
        assert "WorkPackage.find(1002)" in script
        assert "wp.author_id = 789" in script
        assert "wp.save(validate: false)" in script
        assert "Enhanced Author Preservation Script" in script

    def test_execute_rails_author_operations_success(
        self,
        migrator_with_mocks,
        mock_clients,
    ) -> None:
        """Test successful execution of Rails author operations."""
        jira_client, op_client = mock_clients

        # Queue an operation
        migrator_with_mocks._queue_rails_author_operation(
            "TEST-123",
            456,
            {"username": "john.doe"},
        )

        # Mock successful Rails execution
        op_client.rails_client.execute_script.return_value = {"status": "success"}

        work_package_mapping = {"wp1": {"jira_key": "TEST-123", "openproject_id": 1001}}

        result = migrator_with_mocks.execute_rails_author_operations(
            work_package_mapping,
        )

        assert result["processed"] == 1
        assert result["errors"] == []
        assert len(migrator_with_mocks._rails_operations_cache) == 0  # Cache cleared

    def test_execute_rails_author_operations_failure(
        self,
        migrator_with_mocks,
        mock_clients,
    ) -> None:
        """Test Rails author operations execution failure."""
        jira_client, op_client = mock_clients

        # Queue an operation
        migrator_with_mocks._queue_rails_author_operation(
            "TEST-123",
            456,
            {"username": "john.doe"},
        )

        # Mock Rails execution failure
        op_client.rails_client.execute_script.side_effect = Exception(
            "Rails connection failed",
        )

        work_package_mapping = {"wp1": {"jira_key": "TEST-123", "openproject_id": 1001}}

        result = migrator_with_mocks.execute_rails_author_operations(
            work_package_mapping,
        )

        assert result["processed"] == 0
        assert len(result["errors"]) == 1
        assert "Rails connection failed" in result["errors"][0]

    def test_execute_rails_author_operations_empty_cache(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test Rails operations execution with empty cache."""
        result = migrator_with_mocks.execute_rails_author_operations({})

        assert result["processed"] == 0
        assert result["errors"] == []

    @patch("src.utils.enhanced_user_association_migrator.config")
    def test_save_enhanced_mappings(
        self,
        mock_config,
        migrator_with_mocks,
        temp_data_dir,
        sample_enhanced_mapping,
    ) -> None:
        """Test saving enhanced mappings to file."""
        mock_config.get_path.return_value = temp_data_dir

        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping

        migrator_with_mocks.save_enhanced_mappings()

        # Verify file was created
        mapping_file = temp_data_dir / "enhanced_user_mappings.json"
        assert mapping_file.exists()

        # Verify content
        with mapping_file.open() as f:
            saved_data = json.load(f)

        assert "john.doe" in saved_data
        assert saved_data["john.doe"]["jira_username"] == "john.doe"
        assert saved_data["john.doe"]["openproject_user_id"] == 123

    def test_generate_association_report(
        self,
        migrator_with_mocks,
        sample_enhanced_mapping,
    ) -> None:
        """Test generation of association report."""
        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping
        migrator_with_mocks._queue_rails_author_operation(
            "TEST-123",
            456,
            {"username": "john.doe"},
        )

        report = migrator_with_mocks.generate_association_report()

        assert report["summary"]["total_users"] == 3
        assert report["summary"]["mapped_users"] == 3
        assert report["summary"]["unmapped_users"] == 0
        assert report["summary"]["mapping_percentage"] == 100.0
        assert report["rails_operations_pending"] == 1
        assert "generated_at" in report
        assert "detailed_mappings" in report

    def test_migrate_user_associations_full_workflow(
        self,
        migrator_with_mocks,
        sample_jira_issue,
        sample_enhanced_mapping,
        mock_clients,
    ) -> None:
        """Test complete user association migration workflow."""
        jira_client, _ = mock_clients
        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping

        # Mock watchers response
        jira_client.get_issue_watchers.return_value = [
            {
                "name": "john.doe",
                "accountId": "jdoe-123",
                "displayName": "John Doe",
                "active": True,
            },
        ]

        work_package_data = {"jira_key": "TEST-123"}

        result = migrator_with_mocks.migrate_user_associations(
            sample_jira_issue,
            work_package_data,
            preserve_creator_via_rails=True,
        )

        # Verify associations were migrated
        assert work_package_data["assigned_to_id"] == 123  # john.doe
        assert work_package_data["author_id"] == 456  # jane.smith (reporter)
        assert work_package_data["watcher_ids"] == [123]  # john.doe

        # Verify result structure
        assert result["status"] == "success"
        assert "original_association" in result
        assert "mapped_association" in result
        assert len(result["warnings"]) == 0

        # Verify Rails operation queued for author
        assert len(migrator_with_mocks._rails_operations_cache) == 1

    def test_migrate_user_associations_with_warnings(
        self,
        migrator_with_mocks,
        sample_jira_issue,
        mock_clients,
    ) -> None:
        """Test user association migration with warnings for unmapped users."""
        jira_client, _ = mock_clients

        # Empty enhanced mappings - all users unmapped
        migrator_with_mocks.enhanced_user_mappings = {}

        jira_client.get_issue_watchers.return_value = [
            {
                "name": "unknown.user",
                "accountId": "unknown-123",
                "displayName": "Unknown",
                "active": True,
            },
        ]

        work_package_data = {"jira_key": "TEST-123"}

        result = migrator_with_mocks.migrate_user_associations(
            sample_jira_issue,
            work_package_data,
            preserve_creator_via_rails=False,
        )

        # All users should use fallback
        assert work_package_data["assigned_to_id"] == 999  # fallback
        assert work_package_data["author_id"] == 999  # fallback
        assert "watcher_ids" not in work_package_data  # no valid watchers

        # Should have warnings for unmapped users
        assert result["status"] == "fallback_used"
        assert len(result["warnings"]) >= 2  # assignee and author warnings

    def test_migrate_user_associations_watcher_fetch_failure(
        self,
        migrator_with_mocks,
        sample_jira_issue,
        sample_enhanced_mapping,
        mock_clients,
    ) -> None:
        """Test user association migration when watcher fetch fails."""
        jira_client, _ = mock_clients
        migrator_with_mocks.enhanced_user_mappings = sample_enhanced_mapping

        # Mock watcher fetch failure
        jira_client.get_issue_watchers.side_effect = Exception("API error")

        work_package_data = {"jira_key": "TEST-123"}

        result = migrator_with_mocks.migrate_user_associations(
            sample_jira_issue,
            work_package_data,
            preserve_creator_via_rails=False,
        )

        # Should still migrate other associations successfully
        assert work_package_data["assigned_to_id"] == 123  # john.doe
        assert work_package_data["author_id"] == 456  # jane.smith
        assert (
            work_package_data.get("watcher_ids", []) == []
        )  # empty due to fetch failure

        # Should still be successful overall
        assert result["status"] == "success"

    def test_error_handling_in_user_info_fetch(
        self,
        migrator_with_mocks,
        mock_clients,
    ) -> None:
        """Test error handling when fetching user info fails."""
        jira_client, op_client = mock_clients

        # Mock user info fetch failures
        jira_client.get_user_info.side_effect = Exception("Jira API error")
        op_client.get_user.side_effect = Exception("OpenProject API error")

        # Should handle exceptions gracefully
        jira_info = migrator_with_mocks._get_jira_user_info("test.user")
        op_info = migrator_with_mocks._get_openproject_user_info(123)

        assert jira_info is None
        assert op_info is None

    def test_identify_fallback_users_error_handling(self, mock_clients) -> None:
        """Test fallback user identification with API errors."""
        jira_client, op_client = mock_clients

        # Mock API failure
        op_client.get_users.side_effect = Exception("API error")

        with patch(
            "src.utils.enhanced_user_association_migrator.config",
        ) as mock_config:
            mock_config.logger = Mock()
            mock_config.get_path.return_value = Path("/tmp")

            migrator = EnhancedUserAssociationMigrator(
                jira_client=jira_client,
                op_client=op_client,
                user_mapping={},
            )

            # Should handle failure gracefully
            assert migrator.fallback_users == {}
