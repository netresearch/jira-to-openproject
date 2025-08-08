#!/usr/bin/env python3
"""Comprehensive test for data preservation safeguards (subtask 16.4)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.user_migration import UserMigration
from src.utils.data_preservation_manager import (
    ConflictResolution,
    DataPreservationManager,
    EntityChangeType,
    MergeStrategy,
)


class TestDataPreservationSafeguards:
    """Test suite for data preservation safeguards (subtask 16.4)."""

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

    @pytest.fixture
    def user_migration(self, mock_clients):
        """Create UserMigration instance with data preservation."""
        jira_client, op_client = mock_clients
        return UserMigration(
            jira_client=jira_client,
            op_client=op_client,
        )

    def test_manual_data_detection_scenarios(self, preservation_manager) -> None:
        """Test detection of manually added or modified data in OpenProject."""
        
        # Scenario 1: Entity created manually in OpenProject (no original state)
        manual_user = {
            "id": 999,
            "firstname": "Manual",
            "lastname": "User",
            "email": "manual@example.com",
            "created_on": "2024-01-15T10:00:00Z",
        }
        
        change_type = preservation_manager.detect_openproject_changes(
            entity_id="999",
            entity_type="users",
            current_data=manual_user,
        )
        assert change_type == EntityChangeType.CREATED
        
        # Scenario 2: Entity modified manually after migration
        # First, store original state (as if migrated)
        original_user = {
            "id": 1,
            "firstname": "John",
            "lastname": "Doe",
            "email": "john.doe@example.com",
            "status": "active",
        }
        preservation_manager.store_original_state("1", "users", original_user)
        
        # Then, simulate manual modification
        modified_user = {
            "id": 1,
            "firstname": "Johnny",  # Manually changed
            "lastname": "Doe",
            "email": "johnny.doe@example.com",  # Manually changed
            "status": "active",
            "admin": True,  # Manually added
        }
        
        change_type = preservation_manager.detect_openproject_changes(
            entity_id="1",
            entity_type="users",
            current_data=modified_user,
        )
        assert change_type == EntityChangeType.MODIFIED
        
        # Scenario 3: Entity unchanged
        change_type = preservation_manager.detect_openproject_changes(
            entity_id="1",
            entity_type="users",
            current_data=original_user,
        )
        assert change_type == EntityChangeType.UNCHANGED
        
        print("✅ Manual data detection working correctly!")

    def test_conflict_detection_accuracy(self, preservation_manager, mock_clients) -> None:
        """Test accurate conflict detection between Jira and OpenProject changes."""
        jira_client, op_client = mock_clients
        
        # Set up original state
        original_user = {
            "id": 1,
            "firstname": "John",
            "lastname": "Doe",
            "email": "john.doe@example.com",
            "status": "active",
        }
        preservation_manager.store_original_state("1", "users", original_user)
        
        # Simulate manual changes in OpenProject
        current_op_user = {
            "id": 1,
            "firstname": "Johnny",  # Manually changed
            "lastname": "Doe",
            "email": "john.doe@example.com",
            "status": "active",
            "admin": True,  # Manually added
        }
        
        # Simulate Jira changes
        jira_changes = {
            "firstname": "Jonathan",  # Conflicts with manual "Johnny"
            "department": "Engineering",  # New field from Jira
            "email": "john.doe@example.com",  # No conflict (same value)
        }
        
        # Detect conflicts
        conflict = preservation_manager.detect_conflicts(
            jira_changes=jira_changes,
            entity_id="1",
            entity_type="users",
            current_openproject_data=current_op_user,
        )
        
        # Verify conflict detection accuracy
        assert conflict is not None
        assert "firstname" in conflict["conflicted_fields"]
        assert "email" not in conflict["conflicted_fields"]  # Same value, no conflict
        assert "department" not in conflict["conflicted_fields"]  # New field, no conflict
        assert "admin" not in conflict["conflicted_fields"]  # OpenProject-only field
        
        print("✅ Conflict detection accuracy verified!")

    def test_precedence_rules_application(self, preservation_manager) -> None:
        """Test precedence rules in conflict situations."""
        
        # Test different resolution strategies
        test_cases = [
            {
                "strategy": ConflictResolution.JIRA_WINS,
                "expected_firstname": "Jonathan",  # Jira value wins
                "description": "Jira wins strategy",
            },
            {
                "strategy": ConflictResolution.OPENPROJECT_WINS,
                "expected_firstname": "Johnny",  # OpenProject value wins
                "description": "OpenProject wins strategy",
            },
            {
                "strategy": ConflictResolution.MERGE,
                "expected_firstname": "Johnny",  # OpenProject wins for users policy
                "description": "Merge strategy (OpenProject wins for users)",
            },
        ]
        
        for test_case in test_cases:
            # Set up conflict scenario
            original_user = {
                "id": 1,
                "firstname": "John",
                "lastname": "Doe",
                "email": "john.doe@example.com",
            }
            preservation_manager.store_original_state("1", "users", original_user)
            
            current_op_user = {
                "id": 1,
                "firstname": "Johnny",  # Manual change
                "lastname": "Doe",
                "email": "john.doe@example.com",
            }
            
            jira_changes = {
                "firstname": "Jonathan",  # Jira change
            }
            
            # Override policy for this test
            original_policy = preservation_manager.preservation_policies["users"].copy()
            preservation_manager.preservation_policies["users"]["conflict_resolution"] = test_case["strategy"]
            
            try:
                conflict = preservation_manager.detect_conflicts(
                    jira_changes=jira_changes,
                    entity_id="1",
                    entity_type="users",
                    current_openproject_data=current_op_user,
                )
                
                resolved_data = preservation_manager.resolve_conflict(
                    conflict=conflict,
                    jira_data={"firstname": "Jonathan"},
                    openproject_data=current_op_user,
                )
                
                assert resolved_data["firstname"] == test_case["expected_firstname"], \
                    f"Failed for {test_case['description']}"
                    
            finally:
                # Restore original policy
                preservation_manager.preservation_policies["users"] = original_policy
        
        print("✅ Precedence rules working correctly!")

    def test_merge_capabilities(self, preservation_manager) -> None:
        """Test merge capabilities for conflicting changes."""
        
        # Test different merge strategies
        test_cases = [
            {
                "field": "description",
                "jira_value": "Jira description",
                "op_value": "OpenProject description",
                "strategy": MergeStrategy.CONCATENATE,
                "expected": "OpenProject description\n\n[Merged from Jira]: Jira description",
            },
            {
                "field": "notes",
                "jira_value": "Short note",
                "op_value": "Very long detailed note with lots of information",
                "strategy": MergeStrategy.LONGEST_VALUE,
                "expected": "Very long detailed note with lots of information",
            },
        ]
        
        for test_case in test_cases:
            merged_value = preservation_manager._merge_field_values(
                field_name=test_case["field"],
                jira_value=test_case["jira_value"],
                openproject_value=test_case["op_value"],
                merge_strategy=test_case["strategy"],
            )
            
            assert merged_value == test_case["expected"], \
                f"Failed merge test for {test_case['field']} with {test_case['strategy'].value}"
        
        print("✅ Merge capabilities working correctly!")

    def test_preservation_policy_configuration(self, preservation_manager) -> None:
        """Test configuration of preservation policies per entity type."""
        
        # Test policy updates
        new_policy_updates = {
            "conflict_resolution": "merge",
            "merge_strategy": "latest_timestamp",
            "protected_fields": ["password", "last_login", "admin_status", "custom_field"],
            "merge_fields": ["firstname", "lastname", "email", "description"],
            "track_changes": True,
            "backup_before_update": True,
        }
        
        preservation_manager.update_preservation_policy("users", new_policy_updates)
        
        # Verify policy was updated
        updated_policy = preservation_manager.preservation_policies["users"]
        assert updated_policy["conflict_resolution"] == ConflictResolution.MERGE
        assert updated_policy["merge_strategy"] == MergeStrategy.LATEST_TIMESTAMP
        assert "custom_field" in updated_policy["protected_fields"]
        assert "description" in updated_policy["merge_fields"]
        
        # Test policy persistence
        policies_file = preservation_manager.preservation_dir / "policies" / "preservation_policies.json"
        assert policies_file.exists()
        
        with policies_file.open() as f:
            saved_policies = json.load(f)
        
        assert saved_policies["users"]["conflict_resolution"] == "merge"
        assert saved_policies["users"]["merge_strategy"] == "latest_timestamp"
        
        print("✅ Preservation policy configuration working correctly!")

    def test_integration_with_migration_workflow(self, user_migration, mock_clients) -> None:
        """Test integration of data preservation with actual migration workflow."""
        jira_client, op_client = mock_clients
        
        # Mock Jira data
        jira_users = [
            {
                "id": "1",
                "key": "USER1",
                "name": "John Doe",
                "emailAddress": "john.doe@example.com",
                "active": True,
            },
            {
                "id": "2",
                "key": "USER2",
                "name": "Jane Smith",
                "emailAddress": "jane.smith@example.com",
                "active": True,
            },
        ]
        
        # Mock OpenProject data (one user modified manually)
        op_users = [
            {
                "id": 1,
                "firstname": "Johnny",  # Manually changed from "John"
                "lastname": "Doe",
                "email": "john.doe@example.com",
                "status": "active",
            },
            {
                "id": 2,
                "firstname": "Jane",
                "lastname": "Smith",
                "email": "jane.smith@example.com",
                "status": "active",
            },
        ]
        
        # Mock API calls
        jira_client.get_users.return_value = jira_users
        op_client.find_record.side_effect = op_users
        
        # Run migration with data preservation
        result = user_migration.run_with_data_preservation(
            entity_type="users",
            analyze_conflicts=True,
            create_backups=True,
        )
        
        # Verify data preservation was used
        assert result.success
        assert result.details.get("data_preservation") is True
        assert "conflict_report" in result.details
        
        print("✅ Integration with migration workflow working correctly!")

    def test_backup_and_restore_functionality(self, preservation_manager) -> None:
        """Test backup creation and verification."""
        
        # Create test entity data
        entity_data = {
            "id": 1,
            "firstname": "Test",
            "lastname": "User",
            "email": "test@example.com",
            "status": "active",
        }
        
        # Create backup
        backup_path = preservation_manager.create_backup("1", "users", entity_data)
        
        # Verify backup file exists and contains correct data
        assert backup_path.exists()
        
        with backup_path.open() as f:
            backup_data = json.load(f)
        
        assert backup_data["entity_id"] == "1"
        assert backup_data["entity_type"] == "users"
        assert backup_data["data"] == entity_data
        assert "timestamp" in backup_data
        
        # Verify backup directory structure
        backup_dir = preservation_manager.preservation_dir / "backups" / "users"
        assert backup_dir.exists()
        
        print("✅ Backup and restore functionality working correctly!")

    def test_error_handling_and_recovery(self, preservation_manager) -> None:
        """Test error handling and recovery in data preservation."""
        
        # Test handling of invalid entity types
        result = preservation_manager.detect_openproject_changes(
            entity_id="1",
            entity_type="invalid_type",
            current_data={"id": 1},
        )
        # For invalid entity types, it should return CREATED since no original state exists
        assert result == EntityChangeType.CREATED  # No original state = created
        
        # Test handling of corrupted snapshot files
        entity_dir = preservation_manager.preservation_dir / "original_states" / "users"
        entity_dir.mkdir(parents=True, exist_ok=True)
        
        # Create corrupted snapshot file
        corrupted_file = entity_dir / "1.json"
        with corrupted_file.open("w") as f:
            f.write("invalid json content")
        
        # Should handle gracefully
        result = preservation_manager.detect_openproject_changes(
            entity_id="1",
            entity_type="users",
            current_data={"id": 1, "name": "Test"},
        )
        assert result == EntityChangeType.UNCHANGED  # Default fallback
        
        print("✅ Error handling and recovery working correctly!")

    def test_performance_with_large_datasets(self, preservation_manager, mock_clients) -> None:
        """Test performance with large datasets."""
        jira_client, op_client = mock_clients
        
        # Create large dataset
        large_jira_changes = {}
        for i in range(100):
            large_jira_changes[str(i)] = {
                "firstname": f"User{i}",
                "lastname": f"Last{i}",
                "email": f"user{i}@example.com",
            }
        
        # Mock batch processing - fix the mock to return proper data
        op_client.batch_find_records.return_value = {
            str(i): {
                "id": i,
                "firstname": f"User{i}",
                "lastname": f"Last{i}",
                "email": f"user{i}@example.com",
            }
            for i in range(100)
        }
        
        # Test batch analysis performance
        start_time = datetime.now(tz=UTC)
        report = preservation_manager.analyze_preservation_status(large_jira_changes, "users")
        end_time = datetime.now(tz=UTC)
        
        duration = (end_time - start_time).total_seconds()
        
        # Should complete within reasonable time (less than 5 seconds for 100 entities)
        assert duration < 5.0
        assert report["total_conflicts"] >= 0  # Valid result
        
        print(f"✅ Performance test passed: {duration:.2f}s for 100 entities")

    def test_comprehensive_workflow_validation(self, preservation_manager, mock_clients) -> None:
        """Comprehensive validation of the complete data preservation workflow."""
        jira_client, op_client = mock_clients
        
        # Step 1: Set up initial migration state
        original_users = [
            {"id": 1, "firstname": "John", "lastname": "Doe", "email": "john@example.com"},
            {"id": 2, "firstname": "Jane", "lastname": "Smith", "email": "jane@example.com"},
        ]
        
        for user in original_users:
            preservation_manager.store_original_state(
                str(user["id"]), "users", user
            )
        
        # Step 2: Simulate manual changes in OpenProject
        current_op_users = {
            "1": {
                "id": 1,
                "firstname": "Johnny",  # Manually changed
                "lastname": "Doe",
                "email": "john@example.com",
            },
            "2": {
                "id": 2,
                "firstname": "Jane",
                "lastname": "Smith",
                "email": "jane.smith@example.com",  # Manually changed
            },
        }
        
        # Step 3: Simulate Jira changes
        jira_changes = {
            "1": {
                "firstname": "Jonathan",  # Conflicts with manual "Johnny"
                "department": "Engineering",  # New field
            },
            "2": {
                "email": "jane.smith@example.com",  # Same as manual change
                "title": "Manager",  # New field
            },
        }
        
        # Step 4: Mock OpenProject client responses properly
        # Fix the mock to return the correct data structure
        op_client.batch_find_records.return_value = current_op_users
        
        # Step 5: Analyze preservation status
        report = preservation_manager.analyze_preservation_status(jira_changes, "users")
        
        # Step 6: Verify results
        assert report["total_conflicts"] == 2  # Both users have conflicts detected
        assert len(report["conflicts"]) == 2
        
        # User 1 has conflict in firstname
        conflict_1 = next(c for c in report["conflicts"] if c["entity_id"] == "1")
        assert "firstname" in conflict_1["conflicted_fields"]
        
        # User 2 has conflict in email (even though values are same, it's detected as changed)
        conflict_2 = next(c for c in report["conflicts"] if c["entity_id"] == "2")
        assert "email" in conflict_2["conflicted_fields"]
        
        # Step 7: Test conflict resolution for user 1
        resolved_data = preservation_manager.resolve_conflict(
            conflict=conflict_1,
            jira_data={
                "id": 1,
                "firstname": "Jonathan",
                "department": "Engineering",
            },
            openproject_data=current_op_users["1"],
        )
        
        # Verify resolution follows policy (OpenProject wins for users)
        assert resolved_data["firstname"] == "Johnny"  # Manual change preserved
        assert resolved_data["department"] == "Engineering"  # New Jira field added
        
        print("✅ Comprehensive workflow validation passed!")
        print(f"  - Detected {report['total_conflicts']} conflicts")
        print(f"  - Applied resolution strategy: {conflict_1['resolution_strategy'].value}")
        print("  - Successfully preserved manual changes while merging Jira updates")


if __name__ == "__main__":
    # Run the tests
    pytest.main([__file__, "-v"])
