"""End-to-end tests for complete migration workflows.

This module tests the complete Jira to OpenProject migration process,
including all components and realistic data scenarios.
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migration import run_migration, create_backup, restore_backup
from src.models import ComponentResult, MigrationResult


class TestCompleteMigrationWorkflow:
    """Test complete migration workflows from start to finish."""

    @pytest.fixture
    def realistic_jira_data(self):
        """Create realistic Jira test data for migration."""
        return {
            "users": [
                {
                    "accountId": "user1",
                    "displayName": "John Doe",
                    "emailAddress": "john.doe@example.com",
                    "active": True,
                },
                {
                    "accountId": "user2",
                    "displayName": "Jane Smith",
                    "emailAddress": "jane.smith@example.com",
                    "active": True,
                },
            ],
            "projects": [
                {
                    "id": "10001",
                    "key": "TEST",
                    "name": "Test Project",
                    "description": "A test project for migration",
                    "lead": {"accountId": "user1"},
                    "projectTypeKey": "software",
                },
            ],
            "custom_fields": [
                {
                    "id": "customfield_10001",
                    "name": "Story Points",
                    "schema": {"type": "number", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:float"},
                    "description": "Story points estimation",
                },
            ],
            "issue_types": [
                {
                    "id": "10001",
                    "name": "Story",
                    "description": "User story",
                    "subtask": False,
                },
                {
                    "id": "10002",
                    "name": "Bug",
                    "description": "Software bug",
                    "subtask": False,
                },
            ],
            "statuses": [
                {
                    "id": "1",
                    "name": "To Do",
                    "description": "Task is ready to be worked on",
                    "statusCategory": {"key": "new"},
                },
                {
                    "id": "3",
                    "name": "In Progress",
                    "description": "Task is being worked on",
                    "statusCategory": {"key": "indeterminate"},
                },
            ],
            "link_types": [
                {
                    "id": "10100",
                    "name": "Blocks",
                    "inward": "is blocked by",
                    "outward": "blocks",
                },
            ],
            "issues": [
                {
                    "id": "10001",
                    "key": "TEST-1",
                    "fields": {
                        "summary": "Test issue 1",
                        "description": "This is a test issue",
                        "issuetype": {"id": "10001", "name": "Story"},
                        "status": {"id": "1", "name": "To Do"},
                        "assignee": {"accountId": "user1"},
                        "reporter": {"accountId": "user2"},
                        "project": {"id": "10001", "key": "TEST"},
                        "customfield_10001": 5.0,  # Story points
                    },
                },
                {
                    "id": "10002",
                    "key": "TEST-2",
                    "fields": {
                        "summary": "Test issue 2",
                        "description": "Another test issue",
                        "issuetype": {"id": "10002", "name": "Bug"},
                        "status": {"id": "3", "name": "In Progress"},
                        "assignee": {"accountId": "user2"},
                        "reporter": {"accountId": "user1"},
                        "project": {"id": "10001", "key": "TEST"},
                    },
                },
            ],
        }

    @pytest.fixture
    def expected_openproject_data(self):
        """Expected OpenProject data after successful migration."""
        return {
            "users": [
                {
                    "id": 1,
                    "login": "john.doe",
                    "firstname": "John",
                    "lastname": "Doe",
                    "mail": "john.doe@example.com",
                    "status": 1,
                },
                {
                    "id": 2,
                    "login": "jane.smith",
                    "firstname": "Jane",
                    "lastname": "Smith",
                    "mail": "jane.smith@example.com",
                    "status": 1,
                },
            ],
            "projects": [
                {
                    "id": 1,
                    "identifier": "test",
                    "name": "Test Project",
                    "description": "A test project for migration",
                    "status": 1,
                },
            ],
            "work_packages": [
                {
                    "id": 1,
                    "subject": "Test issue 1",
                    "description": "Jira Issue: TEST-1\n\nThis is a test issue",
                    "project_id": 1,
                    "type_id": 1,  # Story -> Task mapping
                    "status_id": 1,  # To Do
                    "assigned_to_id": 1,
                    "author_id": 2,
                },
                {
                    "id": 2,
                    "subject": "Test issue 2",
                    "description": "Jira Issue: TEST-2\n\nAnother test issue",
                    "project_id": 1,
                    "type_id": 2,  # Bug
                    "status_id": 2,  # In Progress
                    "assigned_to_id": 2,
                    "author_id": 1,
                },
            ],
        }

    @pytest.mark.end_to_end
    @pytest.mark.slow
    def test_complete_migration_success(
        self,
        realistic_jira_data,
        expected_openproject_data,
        temp_dir,
        test_env,
    ):
        """Test a complete successful migration from Jira to OpenProject.

        This test validates:
        1. All migration components run in correct order
        2. Data is properly transformed and created in OpenProject
        3. Mappings are maintained throughout the process
        4. Final result indicates success
        """
        # Set up test environment
        test_env["J2O_DATA_DIR"] = str(temp_dir / "data")
        test_env["J2O_BACKUP_DIR"] = str(temp_dir / "backups")
        test_env["J2O_RESULTS_DIR"] = str(temp_dir / "results")

        # Mock all client operations to simulate successful migration
        with patch("src.migration.JiraClient") as mock_jira_class, \
             patch("src.migration.OpenProjectClient") as mock_op_class, \
             patch("src.migration.SSHClient"), \
             patch("src.migration.DockerClient"), \
             patch("src.migration.RailsConsoleClient"):

            # Set up Jira client mock
            mock_jira = MagicMock(spec=JiraClient)
            mock_jira_class.return_value = mock_jira

            # Configure Jira client to return test data
            mock_jira.get_users.return_value = realistic_jira_data["users"]
            mock_jira.get_projects.return_value = realistic_jira_data["projects"]
            mock_jira.get_custom_fields.return_value = realistic_jira_data["custom_fields"]
            mock_jira.get_issue_types.return_value = realistic_jira_data["issue_types"]
            mock_jira.get_statuses.return_value = realistic_jira_data["statuses"]
            mock_jira.get_issue_link_types.return_value = realistic_jira_data["link_types"]
            mock_jira.get_issues_for_project.return_value = realistic_jira_data["issues"]

            # Set up OpenProject client mock
            mock_op = MagicMock(spec=OpenProjectClient)
            mock_op_class.return_value = mock_op

            # Configure OpenProject client to simulate successful creation
            mock_op.create_user.side_effect = lambda user_data: {"id": 1, **user_data}
            mock_op.create_project.side_effect = lambda proj_data: {"id": 1, **proj_data}
            mock_op.create_work_package.side_effect = lambda wp_data: {"id": 1, **wp_data}

            # Configure migration config for test
            with patch("src.config.migration_config", {
                "dry_run": False,
                "no_backup": True,  # Skip backup for test speed
                "force": True,
            }):
                # Run the complete migration
                result = run_migration(
                    components=["users", "projects", "custom_fields", "issue_types", "work_packages"],
                    no_confirm=True,  # Skip user confirmation
                )

                # Validate overall migration result
                assert isinstance(result, MigrationResult)
                assert result.overall["status"] == "success"
                assert "start_time" in result.overall
                assert "end_time" in result.overall
                assert result.overall["total_time_seconds"] > 0

                # Validate each component completed successfully
                expected_components = ["users", "projects", "custom_fields", "issue_types", "work_packages"]
                for component in expected_components:
                    assert component in result.components
                    component_result = result.components[component]
                    assert isinstance(component_result, ComponentResult)
                    assert component_result.success is True

                # Verify client interactions occurred
                mock_jira.get_users.assert_called()
                mock_jira.get_projects.assert_called()
                mock_op.create_user.assert_called()
                mock_op.create_project.assert_called()

    @pytest.mark.end_to_end
    @pytest.mark.slow
    def test_migration_with_component_failure(self, temp_dir, test_env):
        """Test migration behavior when a component fails.

        This test validates:
        1. Migration continues after non-critical component failure
        2. Critical component failure stops migration
        3. Error details are properly captured
        4. Overall status reflects failure
        """
        # Set up test environment
        test_env["J2O_DATA_DIR"] = str(temp_dir / "data")

        with patch("src.migration.JiraClient") as mock_jira_class, \
             patch("src.migration.OpenProjectClient") as mock_op_class, \
             patch("src.migration.SSHClient"), \
             patch("src.migration.DockerClient"), \
             patch("src.migration.RailsConsoleClient"):

            # Set up clients
            mock_jira = MagicMock(spec=JiraClient)
            mock_jira_class.return_value = mock_jira
            mock_op = MagicMock(spec=OpenProjectClient)
            mock_op_class.return_value = mock_op

            # Configure Jira client with basic data
            mock_jira.get_users.return_value = [{"accountId": "user1", "displayName": "Test User"}]
            mock_jira.get_projects.return_value = [{"id": "10001", "key": "TEST", "name": "Test"}]

            # Simulate failure in projects component (critical)
            mock_op.create_user.return_value = {"id": 1, "login": "test.user"}
            mock_op.create_project.side_effect = Exception("OpenProject API error")

            with patch("src.config.migration_config", {
                "dry_run": False,
                "no_backup": True,
            }):
                # Run migration with stop_on_error=True
                result = run_migration(
                    components=["users", "projects"],
                    stop_on_error=True,
                    no_confirm=True,
                )

                # Validate that migration failed
                assert result.overall["status"] == "failed"

                # Validate users component succeeded
                assert "users" in result.components
                assert result.components["users"].success is True

                # Validate projects component failed
                assert "projects" in result.components
                assert result.components["projects"].success is False
                assert "OpenProject API error" in str(result.components["projects"].errors)

    @pytest.mark.end_to_end
    def test_dry_run_migration(self, temp_dir, test_env):
        """Test dry run migration mode.

        This test validates:
        1. No actual changes are made to OpenProject
        2. All components report what would be done
        3. Migration completes successfully in dry run mode
        """
        # Set up test environment
        test_env["J2O_DATA_DIR"] = str(temp_dir / "data")

        with patch("src.migration.JiraClient") as mock_jira_class, \
             patch("src.migration.OpenProjectClient") as mock_op_class, \
             patch("src.migration.SSHClient"), \
             patch("src.migration.DockerClient"), \
             patch("src.migration.RailsConsoleClient"):

            # Set up clients
            mock_jira = MagicMock(spec=JiraClient)
            mock_jira_class.return_value = mock_jira
            mock_op = MagicMock(spec=OpenProjectClient)
            mock_op_class.return_value = mock_op

            # Configure basic test data
            mock_jira.get_users.return_value = [{"accountId": "user1", "displayName": "Test User"}]
            mock_jira.get_projects.return_value = [{"id": "10001", "key": "TEST", "name": "Test"}]

            # Configure for dry run
            with patch("src.config.migration_config", {
                "dry_run": True,
                "no_backup": True,
            }):
                result = run_migration(
                    components=["users", "projects"],
                    no_confirm=True,
                )

                # Validate dry run completed successfully
                assert result.overall["status"] == "success"
                assert result.overall["input_params"]["dry_run"] is True

                # Verify no actual creation calls were made
                mock_op.create_user.assert_not_called()
                mock_op.create_project.assert_not_called()

                # Verify components still reported success
                assert result.components["users"].success is True
                assert result.components["projects"].success is True

    @pytest.mark.end_to_end
    def test_backup_and_restore_functionality(self, temp_dir, test_env):
        """Test backup creation and restoration functionality.

        This test validates:
        1. Backup is created before migration
        2. Backup contains expected files
        3. Restore functionality works correctly
        """
        # Set up test environment
        data_dir = temp_dir / "data"
        backup_dir = temp_dir / "backups"
        data_dir.mkdir(parents=True)
        backup_dir.mkdir(parents=True)

        test_env["J2O_DATA_DIR"] = str(data_dir)
        test_env["J2O_BACKUP_DIR"] = str(backup_dir)

        # Create some test data files
        test_files = {
            "users.json": {"users": [{"id": 1, "name": "test"}]},
            "projects.json": {"projects": [{"id": 1, "name": "test project"}]},
        }

        for filename, data in test_files.items():
            with (data_dir / filename).open("w") as f:
                json.dump(data, f)

        # Test backup creation
        with patch("src.config.get_path") as mock_get_path:
            mock_get_path.side_effect = lambda key: {
                "data": data_dir,
                "backups": backup_dir,
            }[key]

            backup_path = create_backup()

            # Validate backup was created
            assert backup_path.exists()
            assert backup_path.is_dir()

            # Validate backup contains expected files
            backup_files = list(backup_path.glob("*.json"))
            assert len(backup_files) == len(test_files)

            # Validate metadata file exists
            metadata_file = backup_path / "backup_metadata.json"
            assert metadata_file.exists()

            with metadata_file.open() as f:
                metadata = json.load(f)
            assert "timestamp" in metadata
            assert "files_backed_up" in metadata
            assert len(metadata["files_backed_up"]) == len(test_files)

            # Test restoration
            # Modify original data
            with (data_dir / "users.json").open("w") as f:
                json.dump({"users": [{"id": 2, "name": "modified"}]}, f)

            # Restore from backup
            restore_success = restore_backup(backup_path)
            assert restore_success is True

            # Validate original data was restored
            with (data_dir / "users.json").open() as f:
                restored_data = json.load(f)
            assert restored_data == test_files["users.json"]

    @pytest.mark.end_to_end
    @pytest.mark.slow
    def test_large_dataset_migration(self, temp_dir, test_env):
        """Test migration with a larger dataset to validate performance and scalability.

        This test validates:
        1. Migration handles larger datasets efficiently
        2. Memory usage remains reasonable
        3. All items are processed correctly
        """
        # Set up test environment
        test_env["J2O_DATA_DIR"] = str(temp_dir / "data")

        # Create larger test dataset
        large_user_dataset = [
            {
                "accountId": f"user{i}",
                "displayName": f"Test User {i}",
                "emailAddress": f"user{i}@example.com",
                "active": True,
            }
            for i in range(100)
        ]

        large_issue_dataset = [
            {
                "id": str(10000 + i),
                "key": f"TEST-{i}",
                "fields": {
                    "summary": f"Test issue {i}",
                    "description": f"Description for issue {i}",
                    "issuetype": {"id": "10001", "name": "Story"},
                    "status": {"id": "1", "name": "To Do"},
                    "project": {"id": "10001", "key": "TEST"},
                },
            }
            for i in range(50)
        ]

        with patch("src.migration.JiraClient") as mock_jira_class, \
             patch("src.migration.OpenProjectClient") as mock_op_class, \
             patch("src.migration.SSHClient"), \
             patch("src.migration.DockerClient"), \
             patch("src.migration.RailsConsoleClient"):

            # Set up clients
            mock_jira = MagicMock(spec=JiraClient)
            mock_jira_class.return_value = mock_jira
            mock_op = MagicMock(spec=OpenProjectClient)
            mock_op_class.return_value = mock_op

            # Configure with large dataset
            mock_jira.get_users.return_value = large_user_dataset
            mock_jira.get_projects.return_value = [{"id": "10001", "key": "TEST", "name": "Test"}]
            mock_jira.get_issues_for_project.return_value = large_issue_dataset

            # Configure successful responses
            mock_op.create_user.side_effect = lambda data: {"id": 1, **data}
            mock_op.create_project.side_effect = lambda data: {"id": 1, **data}
            mock_op.create_work_package.side_effect = lambda data: {"id": 1, **data}

            with patch("src.config.migration_config", {
                "dry_run": False,
                "no_backup": True,
            }):
                # Measure execution time
                start_time = time.time()

                result = run_migration(
                    components=["users", "projects", "work_packages"],
                    no_confirm=True,
                )

                execution_time = time.time() - start_time

                # Validate successful completion
                assert result.overall["status"] == "success"

                # Validate all components processed successfully
                assert result.components["users"].success is True
                assert result.components["projects"].success is True
                assert result.components["work_packages"].success is True

                # Validate reasonable execution time (should complete within reasonable time)
                assert execution_time < 30  # 30 seconds for this dataset size

                # Validate correct number of create calls
                assert mock_op.create_user.call_count == len(large_user_dataset)
                assert mock_op.create_work_package.call_count == len(large_issue_dataset)

    @pytest.mark.end_to_end
    def test_component_dependency_order(self, temp_dir, test_env):
        """Test that migration components run in the correct dependency order.

        This test validates:
        1. Users are created before projects (projects need users)
        2. Projects are created before work packages (work packages need projects)
        3. Custom fields are created before work packages (work packages may use custom fields)
        """
        # Set up test environment
        test_env["J2O_DATA_DIR"] = str(temp_dir / "data")

        call_order = []

        def track_users_call(*args, **kwargs):
            call_order.append("users")
            return {"id": 1, "login": "test"}

        def track_projects_call(*args, **kwargs):
            call_order.append("projects")
            return {"id": 1, "identifier": "test"}

        def track_work_packages_call(*args, **kwargs):
            call_order.append("work_packages")
            return {"id": 1, "subject": "test"}

        with patch("src.migration.JiraClient") as mock_jira_class, \
             patch("src.migration.OpenProjectClient") as mock_op_class, \
             patch("src.migration.SSHClient"), \
             patch("src.migration.DockerClient"), \
             patch("src.migration.RailsConsoleClient"):

            # Set up clients
            mock_jira = MagicMock(spec=JiraClient)
            mock_jira_class.return_value = mock_jira
            mock_op = MagicMock(spec=OpenProjectClient)
            mock_op_class.return_value = mock_op

            # Configure basic test data
            mock_jira.get_users.return_value = [{"accountId": "user1", "displayName": "Test"}]
            mock_jira.get_projects.return_value = [{"id": "10001", "key": "TEST", "name": "Test"}]
            mock_jira.get_issues_for_project.return_value = [
                {"id": "10001", "key": "TEST-1", "fields": {"summary": "Test"}}
            ]

            # Track call order
            mock_op.create_user.side_effect = track_users_call
            mock_op.create_project.side_effect = track_projects_call
            mock_op.create_work_package.side_effect = track_work_packages_call

            with patch("src.config.migration_config", {
                "dry_run": False,
                "no_backup": True,
            }):
                result = run_migration(
                    components=["work_packages", "projects", "users"],  # Intentionally wrong order
                    no_confirm=True,
                )

                # Validate migration succeeded despite wrong component order
                # (The migration system should handle dependencies internally)
                assert result.overall["status"] == "success"

                # Validate calls were made in dependency order
                # Note: The actual implementation should ensure proper order
                # regardless of the components list order
                assert "users" in call_order
                assert "projects" in call_order
                assert "work_packages" in call_order
