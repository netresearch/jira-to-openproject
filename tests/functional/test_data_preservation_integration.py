#!/usr/bin/env python3
"""Integration test for data preservation workflow."""

import json
from unittest.mock import Mock

import pytest

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.utils.data_preservation_manager import (
    ConflictResolution,
    DataPreservationManager,
)


class TestDataPreservationIntegration:
    """Integration tests for the complete data preservation workflow."""

    @pytest.fixture
    def temp_preservation_dir(self, tmp_path):
        """Create a temporary preservation directory."""
        return tmp_path / "preservation"

    @pytest.fixture
    def mock_clients(self):
        """Create mock clients."""
        jira_client = Mock(spec=JiraClient)
        op_client = Mock(spec=OpenProjectClient)
        return jira_client, op_client

    @pytest.fixture
    def preservation_manager(self, temp_preservation_dir, mock_clients):
        """Create DataPreservationManager instance."""
        jira_client, op_client = mock_clients
        return DataPreservationManager(
            preservation_dir=temp_preservation_dir,
            jira_client=jira_client,
            openproject_client=op_client,
        )

    def test_complete_data_preservation_workflow(
        self,
        preservation_manager,
        mock_clients,
    ) -> None:
        """Test the complete data preservation workflow end-to-end."""
        jira_client, op_client = mock_clients

        # Scenario: User has been manually modified in OpenProject
        # but also updated in Jira, creating a conflict

        # 1. Set up initial state - user was migrated previously
        original_user = {
            "id": 1,
            "firstname": "John",
            "lastname": "Doe",
            "email": "john.doe@example.com",
            "status": "active",
        }
        preservation_manager.store_original_state("1", "users", original_user)

        # 2. User was manually modified in OpenProject
        current_op_user = {
            "id": 1,
            "firstname": "Johnny",  # Changed manually from "John" to "Johnny"
            "lastname": "Smith",  # Changed manually
            "email": "john.smith@example.com",  # Changed manually
            "status": "active",
            "admin": True,  # Added manually
        }

        # 3. User was also updated in Jira
        jira_user_changes = {
            "firstname": "Jonathan",  # Changed in Jira - conflicts with manual change to "Johnny"
            "department": "Engineering",  # New field from Jira
        }

        # 4. Mock OpenProject client to return current state
        op_client.find_record.return_value = current_op_user

        # 5. Detect conflicts
        conflict = preservation_manager.detect_conflicts(
            jira_changes=jira_user_changes,
            entity_id="1",
            entity_type="users",
            current_openproject_data=current_op_user,
        )

        # Verify conflict detection
        assert conflict is not None
        assert conflict["entity_id"] == "1"
        assert conflict["entity_type"] == "users"
        assert "firstname" in conflict["conflicted_fields"]
        assert len(conflict["conflicted_fields"]) == 1  # Only firstname conflicts

        # 6. Resolve the conflict using the configured policy
        # Users policy: OpenProject wins with merge strategy
        jira_full_data = {
            "id": 1,
            "firstname": "Jonathan",  # This is the only conflicted field
            "department": "Engineering",  # Non-conflicted new field from Jira
            "status": "active",  # Common field
        }

        resolved_data = preservation_manager.resolve_conflict(
            conflict=conflict,
            jira_data=jira_full_data,
            openproject_data=current_op_user,
        )

        # 7. Verify resolution follows policy (OpenProject wins for conflicted fields)
        assert (
            resolved_data["firstname"] == "Johnny"
        )  # OpenProject wins (manual change preserved)
        assert resolved_data["lastname"] == "Smith"  # OpenProject value preserved
        assert (
            resolved_data["email"] == "john.smith@example.com"
        )  # OpenProject value preserved
        assert (
            resolved_data["department"] == "Engineering"
        )  # Non-conflicted Jira field added
        assert resolved_data["admin"]  # OpenProject-only field preserved
        assert resolved_data["status"] == "active"  # Common field

        # 8. Create backup before applying changes
        backup_path = preservation_manager.create_backup("1", "users", current_op_user)
        assert backup_path.exists()

        # Verify backup content
        with backup_path.open() as f:
            backup_data = json.load(f)
        assert backup_data["entity_id"] == "1"
        assert backup_data["entity_type"] == "users"
        assert backup_data["data"] == current_op_user

        # 9. Store new state after resolution
        preservation_manager.store_original_state("1", "users", resolved_data)

        print("✅ Data preservation workflow completed successfully!")
        print(f"  - Detected 1 conflict in field: {conflict['conflicted_fields']}")
        print(
            f"  - Applied resolution strategy: {conflict['resolution_strategy'].value}",
        )
        print(f"  - Created backup at: {backup_path}")
        print("  - Preserved manual changes while merging Jira updates")

    def test_no_conflict_scenario(self, preservation_manager, mock_clients) -> None:
        """Test scenario where no conflicts exist."""
        jira_client, op_client = mock_clients

        # Set up original state
        original_user = {
            "id": 1,
            "firstname": "Jane",
            "lastname": "Doe",
            "email": "jane.doe@example.com",
        }
        preservation_manager.store_original_state("1", "users", original_user)

        # User unchanged in OpenProject
        current_op_user = original_user.copy()

        # Jira has new data in different fields
        jira_changes = {
            "department": "Marketing",  # New field, no conflict
            "phone": "+1234567890",  # New field, no conflict
        }

        # Mock OpenProject client
        op_client.find_record.return_value = current_op_user

        # Detect conflicts (should be none)
        conflict = preservation_manager.detect_conflicts(
            jira_changes=jira_changes,
            entity_id="1",
            entity_type="users",
            current_openproject_data=current_op_user,
        )

        # Verify no conflicts detected
        assert conflict is None
        print("✅ No conflict scenario handled correctly!")

    def test_preservation_policy_configuration(self, preservation_manager) -> None:
        """Test preservation policy configuration and updates."""
        # Test default policies are loaded
        assert "users" in preservation_manager.preservation_policies
        assert "projects" in preservation_manager.preservation_policies
        assert "work_packages" in preservation_manager.preservation_policies

        # Verify users policy (OpenProject wins)
        users_policy = preservation_manager.preservation_policies["users"]
        assert (
            users_policy["conflict_resolution"] == ConflictResolution.OPENPROJECT_WINS
        )
        assert "password" in users_policy["protected_fields"]
        assert "firstname" in users_policy["merge_fields"]

        # Test policy update
        preservation_manager.update_preservation_policy(
            "users",
            {
                "conflict_resolution": "merge",
                "protected_fields": [
                    "password",
                    "last_login",
                    "admin_status",
                    "created_on",
                ],
            },
        )

        # Verify update applied
        updated_policy = preservation_manager.preservation_policies["users"]
        assert updated_policy["conflict_resolution"] == ConflictResolution.MERGE
        assert "created_on" in updated_policy["protected_fields"]

        print("✅ Preservation policy configuration working correctly!")

    def test_bulk_conflict_analysis(self, preservation_manager, mock_clients) -> None:
        """Test analyzing conflicts for multiple entities."""
        jira_client, op_client = mock_clients

        # Set up multiple users with different conflict scenarios
        users_data = [
            ("1", {"firstname": "John", "lastname": "Doe"}),
            ("2", {"firstname": "Jane", "lastname": "Smith"}),
            ("3", {"firstname": "Bob", "lastname": "Johnson"}),
        ]

        # Store original states
        for user_id, user_data in users_data:
            preservation_manager.store_original_state(user_id, "users", user_data)

        # Mock current OpenProject state (some users modified)
        op_client.find_record.side_effect = [
            {
                "id": 1,
                "firstname": "John",
                "lastname": "Modified",
            },  # Conflict potential
            {"id": 2, "firstname": "Jane", "lastname": "Smith"},  # Unchanged
            {
                "id": 3,
                "firstname": "Bob",
                "lastname": "Johnson",
            },  # Unchanged (no manual modification)
        ]

        # Jira changes affecting multiple users
        jira_changes = {
            "1": {
                "lastname": "Updated",
                "department": "Engineering",
            },  # Conflict on lastname
            "2": {"department": "Marketing"},  # No conflict - only new field
            "3": {
                "title": "Manager",
            },  # No conflict - only new field, no changes to existing fields
        }

        # Analyze preservation status
        report = preservation_manager.analyze_preservation_status(jira_changes, "users")

        # Verify analysis results
        assert report["total_conflicts"] == 1  # Only user 1 has actual field conflicts
        assert report["conflicts_by_type"]["users"] == 1
        assert len(report["conflicts"]) == 1

        # Verify the conflict details
        conflict = report["conflicts"][0]
        assert conflict["entity_id"] == "1"
        assert "lastname" in conflict["conflicted_fields"]

        print("✅ Bulk conflict analysis working correctly!")
        print(f"  - Analyzed {len(jira_changes)} entities")
        print(f"  - Detected {report['total_conflicts']} conflicts")
        print(f"  - Conflict distribution: {report['conflicts_by_resolution']}")
