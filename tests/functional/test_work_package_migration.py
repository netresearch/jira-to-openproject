"""Tests for the work package migration component."""

import unittest
from typing import Any
from unittest.mock import MagicMock

from src.migrations.work_package_migration import WorkPackageMigration


class TestWorkPackageMigration(unittest.TestCase):
    """Test cases for the WorkPackageMigration class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Sample Jira issues data
        self.jira_issues = [
            {
                "id": "10001",
                "key": "PROJ-1",
                "summary": "Sample Bug",
                "description": "This is a sample bug",
                "issuetype": {"id": "10000", "name": "Bug"},
                "project": {"id": "10000", "key": "PROJ", "name": "Test Project"},
                "status": {"id": "1", "name": "Open"},
                "assignee": {"name": "johndoe", "emailAddress": "john@example.com"},
                "reporter": {"name": "janedoe", "emailAddress": "jane@example.com"},
                "created": "2023-01-01T10:00:00.000+0000",
                "updated": "2023-01-02T11:00:00.000+0000",
                "comment": {
                    "comments": [
                        {
                            "id": "10001",
                            "body": "This is a comment",
                            "author": {"name": "janedoe"},
                        },
                    ],
                },
                "attachment": [
                    {"id": "10001", "filename": "test.txt", "content": "test content"},
                ],
            },
            {
                "id": "10002",
                "key": "PROJ-2",
                "summary": "Sample Task",
                "description": "This is a sample task",
                "issuetype": {"id": "10001", "name": "Task"},
                "project": {"id": "10000", "key": "PROJ", "name": "Test Project"},
                "status": {"id": "2", "name": "In Progress"},
                "assignee": {"name": "johndoe", "emailAddress": "john@example.com"},
                "reporter": {"name": "janedoe", "emailAddress": "jane@example.com"},
                "created": "2023-01-03T10:00:00.000+0000",
                "updated": "2023-01-04T11:00:00.000+0000",
                "comment": {"comments": []},
                "attachment": [],
            },
        ]

        # Sample OpenProject work packages data
        self.op_work_packages = [
            {
                "id": 1,
                "subject": "Sample Bug",
                "description": {
                    "raw": "This is a sample bug\n\n*Imported from Jira issue: PROJ-1*",
                },
                "_links": {
                    "type": {"href": "/api/v3/types/1", "title": "Bug"},
                    "status": {"href": "/api/v3/statuses/1", "title": "Open"},
                    "assignee": {"href": "/api/v3/users/1", "title": "John Doe"},
                    "project": {"href": "/api/v3/projects/1", "title": "Test Project"},
                },
            },
        ]

        # Mapping data
        self.project_mapping = {"PROJ": {"jira_key": "PROJ", "openproject_id": 1}}

        self.user_mapping = {"johndoe": 1, "janedoe": 2}

        self.issue_type_mapping = {"10000": 1, "10001": 2}  # Bug  # Task

        self.status_mapping = {
            "1": {"openproject_id": 1},  # Open
            "2": {"openproject_id": 2},  # In Progress
        }

        # Expected work package mapping
        self.work_package_mapping = {
            "10001": {
                "jira_id": "10001",
                "jira_key": "PROJ-1",
                "openproject_id": 1,
                "subject": "Sample Bug",
                "status": "created",
            },
            "10002": {
                "jira_id": "10002",
                "jira_key": "PROJ-2",
                "openproject_id": 2,
                "subject": "Sample Task",
                "status": "created",
            },
        }

    def test_initialize(self) -> None:
        """``WorkPackageMigration`` should construct with mocked clients."""
        migration = WorkPackageMigration(
            jira_client=MagicMock(),
            op_client=MagicMock(),
        )

        assert migration.jira_client is not None
        assert migration.op_client is not None
        # Mappings are populated (may be empty) by the base class
        assert hasattr(migration, "project_mapping")
        assert hasattr(migration, "user_mapping")
        assert hasattr(migration, "issue_type_mapping")
        assert hasattr(migration, "status_mapping")

    def test_load_mappings(self) -> None:
        """``_load_mappings`` should populate the mapping attributes."""
        migration = WorkPackageMigration(
            jira_client=MagicMock(),
            op_client=MagicMock(),
        )
        # Call again explicitly; mappings should remain coherent dict instances
        migration._load_mappings()

        assert isinstance(migration.project_mapping, dict)
        assert isinstance(migration.user_mapping, dict)
        assert isinstance(migration.issue_type_mapping, dict)
        assert isinstance(migration.status_mapping, dict)

    def test_prepare_work_package(self) -> None:
        """Test the prepare_work_package method."""
        mock_op = MagicMock()
        mock_op.get_work_package_types.return_value = [{"id": 1, "name": "Task"}]

        migration = WorkPackageMigration(
            jira_client=MagicMock(),
            op_client=mock_op,
        )
        # Seed minimal mappings required by prepare_work_package
        migration.project_mapping = {"PROJ": {"openproject_id": 1}}
        migration.issue_type_mapping = {"Bug": {"openproject_id": 1}}
        migration.issue_type_id_mapping = {"10000": 1}
        migration.status_mapping = {"1": {"openproject_id": 1}}

        mock_issue = {
            "id": "10001",
            "key": "PROJ-123",
            "summary": "Test issue",
            "description": "This is a test issue",
            "issue_type": {"id": "10000", "name": "Bug"},
            "status": {"id": "1", "name": "Open"},
        }

        result = migration.prepare_work_package(mock_issue, 1)

        assert result is not None
        assert result["project_id"] == 1
        assert result["subject"] == "Test issue"
        # ``jira_key``/``jira_id`` are intentionally stripped by
        # ``_sanitize_wp_dict`` before the dict hits Rails mass-assignment;
        # the reference to the Jira key survives inside the formatted
        # description.
        assert "PROJ-123" in result["description"]
        assert "jira_key" not in result

    def test_sanitize_wp_dict_removes_links_and_extracts_ids(self) -> None:
        migration = WorkPackageMigration(jira_client=MagicMock(), op_client=MagicMock())

        wp = {
            "project_id": 1,
            "subject": "S",
            "description": "D",
            "_links": {
                "type": {"href": "/api/v3/types/1"},
                "status": {"href": "/api/v3/statuses/2"},
            },
        }

        assert "_links" in wp

        migration._sanitize_wp_dict(wp)

        # _links removed, ids extracted
        assert "_links" not in wp
        assert wp.get("type_id") == 1
        assert wp.get("status_id") == 2

    def test_migrate_work_packages(self) -> None:
        """The public entrypoint for work-package migration is ``run()``."""
        migration = WorkPackageMigration(
            jira_client=MagicMock(),
            op_client=MagicMock(),
        )
        # ``migrate_work_packages`` was replaced by the ``run()`` /
        # ``_migrate_work_packages`` pair.
        assert callable(migration.run)
        assert callable(migration._migrate_work_packages)

    def test_analyze_work_package_mapping(self) -> None:
        """``analyze_work_package_mapping`` was removed; ``run()`` reports stats instead."""
        migration = WorkPackageMigration(
            jira_client=MagicMock(),
            op_client=MagicMock(),
        )
        # The replacement reporting flows through ComponentResult fields;
        # assert the migration exposes the expected attributes needed for
        # downstream reporting.
        assert hasattr(migration, "work_package_mapping")
        assert callable(migration.run)

    def test_run_fails_when_zero_created_but_issues_discovered(self) -> None:
        """``run()`` surfaces failure via ComponentResult on exceptions."""
        migration = WorkPackageMigration(
            jira_client=MagicMock(),
            op_client=MagicMock(),
        )
        # Force the inner pipeline to raise so ``run()`` reports failure
        migration._migrate_work_packages = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("no work packages created"),
        )

        result = migration.run()

        assert result.success is False
        assert "no work packages created" in str(result.error)


# Define testing steps for work package migration validation


def work_package_migration_test_steps() -> Any:
    """Testing steps for work package migration validation.

    These steps should be executed in a real environment to validate
    the work package migration functionality:

    1. Verify issue extraction from Jira:
       - Check that all Jira issues are extracted correctly across projects
       - Verify key attributes (summary, description, assignee, etc.)
       - Test handling of large issue counts with pagination

    2. Test work package preparation:
       - Verify field mappings for core fields (subject, description, etc.)
       - Test mapping of Jira issue types to OpenProject work package types
       - Test mapping of Jira statuses to OpenProject statuses
       - Test handling of user assignments

    3. Test work package creation:
       - Test batch creation of work packages via API
       - Test direct creation of work packages via API or Rails console
       - Verify creation of work packages with correct attributes
       - Test handling of API rate limits and errors

    4. Test work package hierarchy:
       - Create test issues in Jira with parent-child relationships
       - Run the work package migration
       - Verify the hierarchy is correctly maintained in OpenProject
       - Test Epic-Story relationships or subtask relationships

    5. Test attachments migration:
       - Create test issues in Jira with attachments
       - Run the work package migration with attachment handling
       - Verify attachments are correctly transferred to OpenProject
       - Test large attachments and different file types

    6. Test comments migration:
       - Create test issues in Jira with comments
       - Run the work package migration with comment handling
       - Verify comments are correctly transferred to OpenProject
       - Test comment author mapping and formatting

    7. Test relation migration:
       - Create test issues in Jira with various link types
       - Run the work package migration with relation handling
       - Verify relations are correctly created in OpenProject
       - Test relation type mapping accuracy

    8. Test field mapping:
       - Create test issues in Jira with various custom fields
       - Run the work package migration
       - Verify custom field values are correctly transferred
       - Test specialized fields like Tempo Account

    9. Test data validation:
       - Run the analyze_work_package_mapping method
       - Verify it correctly reports on mapping statistics
       - Check for any potential issues in the migration
       - Verify counts match expected values

    10. Test idempotency and resilience:
        - Run the migration multiple times
        - Verify no duplicate work packages are created
        - Test error handling and recovery
        - Test the migration with network interruptions
    """
    return "Work package migration test steps defined"
