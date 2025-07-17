#!/usr/bin/env python3
"""Unit tests for DataPreservationManager."""

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest

from src.utils.data_preservation_manager import (
    ConflictInfo,
    ConflictResolution,
    DataPreservationManager,
    EntityChangeType,
    MergeStrategy,
)


class TestDataPreservationManager:
    """Test suite for DataPreservationManager."""

    @pytest.fixture
    def temp_preservation_dir(self, tmp_path):
        """Create a temporary preservation directory."""
        return tmp_path / "preservation"

    @pytest.fixture
    def mock_clients(self):
        """Create mock clients."""
        return Mock(), Mock()

    @pytest.fixture
    def preservation_manager(self, temp_preservation_dir, mock_clients):
        """Create DataPreservationManager instance."""
        jira_client, openproject_client = mock_clients
        return DataPreservationManager(
            preservation_dir=temp_preservation_dir,
            jira_client=jira_client,
            openproject_client=openproject_client,
        )

    def test_initialization(self, preservation_manager, temp_preservation_dir):
        """Test DataPreservationManager initialization."""
        assert preservation_manager.preservation_dir == temp_preservation_dir
        assert preservation_manager.preservation_dir.exists()

        # Check directory structure
        assert (temp_preservation_dir / "original_states").exists()
        assert (temp_preservation_dir / "conflicts").exists()
        assert (temp_preservation_dir / "policies").exists()
        assert (temp_preservation_dir / "backups").exists()

        # Check policies loaded
        assert "users" in preservation_manager.preservation_policies
        assert "projects" in preservation_manager.preservation_policies
        assert "work_packages" in preservation_manager.preservation_policies

    def test_calculate_entity_checksum(self, preservation_manager):
        """Test entity checksum calculation."""
        entity_data = {
            "id": "1",
            "name": "Test Entity",
            "description": "Test description",
            "self": "http://test.com/1",  # Volatile field
            "lastViewed": "2023-01-01T00:00:00Z",  # Volatile field
        }

        checksum = preservation_manager._calculate_entity_checksum(entity_data)

        # Should be deterministic
        checksum2 = preservation_manager._calculate_entity_checksum(entity_data)
        assert checksum == checksum2

        # Should exclude volatile fields
        normalized_data = {
            "id": "1",
            "name": "Test Entity",
            "description": "Test description",
        }
        expected_json = json.dumps(
            normalized_data, sort_keys=True, separators=(",", ":")
        )
        expected_checksum = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
        assert checksum == expected_checksum

    def test_store_original_state(self, preservation_manager):
        """Test storing original state of an entity."""
        entity_data = {"id": "1", "name": "Test", "description": "Test description"}

        preservation_manager.store_original_state(
            entity_id="1", entity_type="users", entity_data=entity_data
        )

        # Check file was created
        snapshot_file = (
            preservation_manager.preservation_dir
            / "original_states"
            / "users"
            / "1.json"
        )
        assert snapshot_file.exists()

        # Check content
        with snapshot_file.open() as f:
            snapshot = json.load(f)

        assert snapshot["entity_id"] == "1"
        assert snapshot["entity_type"] == "users"
        assert snapshot["data"] == entity_data
        assert snapshot["source"] == "migration"
        assert "timestamp" in snapshot
        assert "checksum" in snapshot

    def test_detect_openproject_changes_unchanged(self, preservation_manager):
        """Test detecting unchanged entity."""
        entity_data = {"id": "1", "name": "Test"}

        # Store original state
        preservation_manager.store_original_state("1", "users", entity_data)

        # Check unchanged data
        change_type = preservation_manager.detect_openproject_changes(
            "1", "users", entity_data
        )
        assert change_type == EntityChangeType.UNCHANGED

    def test_detect_openproject_changes_modified(self, preservation_manager):
        """Test detecting modified entity."""
        original_data = {"id": "1", "name": "Test"}
        modified_data = {"id": "1", "name": "Modified Test"}

        # Store original state
        preservation_manager.store_original_state("1", "users", original_data)

        # Check modified data
        change_type = preservation_manager.detect_openproject_changes(
            "1", "users", modified_data
        )
        assert change_type == EntityChangeType.MODIFIED

    def test_detect_openproject_changes_created(self, preservation_manager):
        """Test detecting newly created entity."""
        entity_data = {"id": "1", "name": "Test"}

        # No original state stored
        change_type = preservation_manager.detect_openproject_changes(
            "1", "users", entity_data
        )
        assert change_type == EntityChangeType.CREATED

    def test_detect_conflicts_no_manual_changes(self, preservation_manager):
        """Test conflict detection when no manual changes exist."""
        entity_data = {"id": "1", "name": "Test"}
        jira_changes = {"description": "New description"}

        # Store original state
        preservation_manager.store_original_state("1", "users", entity_data)

        # No manual changes - same data
        conflict = preservation_manager.detect_conflicts(
            jira_changes, "1", "users", entity_data
        )
        assert conflict is None

    def test_detect_conflicts_with_manual_changes_no_overlap(
        self, preservation_manager
    ):
        """Test conflict detection with manual changes but no field overlap."""
        original_data = {"id": "1", "name": "Test"}
        current_data = {"id": "1", "name": "Modified Test"}  # Manual change
        jira_changes = {"description": "New description"}  # Different field

        # Store original state
        preservation_manager.store_original_state("1", "users", original_data)

        # Manual changes but no field conflicts
        conflict = preservation_manager.detect_conflicts(
            jira_changes, "1", "users", current_data
        )
        assert conflict is None

    def test_detect_conflicts_with_field_conflicts(self, preservation_manager):
        """Test conflict detection with actual field conflicts."""
        original_data = {"id": "1", "name": "Test", "description": "Original"}
        current_data = {
            "id": "1",
            "name": "Manual Change",
            "description": "Manual Description",
        }
        jira_changes = {"name": "Jira Change", "email": "test@example.com"}

        # Store original state
        preservation_manager.store_original_state("1", "users", original_data)

        # Conflicted field: "name" changed in both systems
        conflict = preservation_manager.detect_conflicts(
            jira_changes, "1", "users", current_data
        )

        assert conflict is not None
        assert conflict["entity_id"] == "1"
        assert conflict["entity_type"] == "users"
        assert "name" in conflict["conflicted_fields"]
        assert len(conflict["conflicted_fields"]) == 1
        assert conflict["resolution_strategy"] == ConflictResolution.OPENPROJECT_WINS

    def test_resolve_conflict_jira_wins(self, preservation_manager):
        """Test conflict resolution with Jira wins strategy."""
        conflict = ConflictInfo(
            entity_id="1",
            entity_type="custom_fields",  # Uses JIRA_WINS policy
            jira_changes={"name": "Jira Name", "description": "Jira Desc"},
            openproject_changes={"name": "OP Name"},
            conflicted_fields=["name"],
            resolution_strategy=ConflictResolution.JIRA_WINS,
            timestamp=datetime.now(tz=UTC).isoformat(),
        )

        jira_data = {
            "name": "Jira Name",
            "description": "Jira Desc",
            "created_on": "2023-01-01",
        }
        openproject_data = {
            "name": "OP Name",
            "status": "active",
            "created_on": "2023-01-02",
        }

        resolved = preservation_manager.resolve_conflict(
            conflict, jira_data, openproject_data
        )

        # Jira wins for non-protected fields
        assert resolved["name"] == "Jira Name"
        assert resolved["description"] == "Jira Desc"
        # Protected field preserved
        assert resolved["created_on"] == "2023-01-02"  # OpenProject value preserved
        # OP-only field preserved
        assert resolved["status"] == "active"

    def test_resolve_conflict_openproject_wins(self, preservation_manager):
        """Test conflict resolution with OpenProject wins strategy."""
        conflict = ConflictInfo(
            entity_id="1",
            entity_type="users",  # Uses OPENPROJECT_WINS policy
            jira_changes={"name": "Jira Name", "email": "jira@test.com"},
            openproject_changes={"name": "OP Name"},
            conflicted_fields=["name"],
            resolution_strategy=ConflictResolution.OPENPROJECT_WINS,
            timestamp=datetime.now(tz=UTC).isoformat(),
        )

        jira_data = {"name": "Jira Name", "email": "jira@test.com"}
        openproject_data = {"name": "OP Name", "status": "active"}

        resolved = preservation_manager.resolve_conflict(
            conflict, jira_data, openproject_data
        )

        # OpenProject wins for conflicted fields
        assert resolved["name"] == "OP Name"
        # Non-conflicted Jira fields added
        assert resolved["email"] == "jira@test.com"
        # OP-only field preserved
        assert resolved["status"] == "active"

    def test_resolve_conflict_merge_strategy(self, preservation_manager):
        """Test conflict resolution with merge strategy."""
        conflict = ConflictInfo(
            entity_id="1",
            entity_type="projects",  # Uses MERGE policy
            jira_changes={"description": "Jira Desc", "homepage": "jira.com"},
            openproject_changes={"description": "OP Desc", "homepage": "op.com"},
            conflicted_fields=["description", "homepage"],
            resolution_strategy=ConflictResolution.MERGE,
            timestamp=datetime.now(tz=UTC).isoformat(),
        )

        jira_data = {
            "description": "Jira Desc",
            "homepage": "jira.com",
            "category": "development",
        }
        openproject_data = {
            "description": "OP Desc",
            "homepage": "op.com",
            "created_on": "2023-01-01",
        }

        resolved = preservation_manager.resolve_conflict(
            conflict, jira_data, openproject_data
        )

        # Merge fields should be merged
        assert "OP Desc" in resolved["description"]
        assert "Updated from Jira" in resolved["description"]
        assert "Jira Desc" in resolved["description"]

        # Protected field preserved
        assert resolved["created_on"] == "2023-01-01"
        # Non-conflicted field from Jira
        assert resolved["category"] == "development"

    def test_merge_field_values_concatenate(self, preservation_manager):
        """Test field value merging with concatenate strategy."""
        result = preservation_manager._merge_field_values(
            "description", "Jira value", "OP value", MergeStrategy.CONCATENATE
        )

        assert result == "OP value\n\n[Merged from Jira]: Jira value"

    def test_merge_field_values_longest_value(self, preservation_manager):
        """Test field value merging with longest value strategy."""
        result = preservation_manager._merge_field_values(
            "name", "Short", "Much longer value", MergeStrategy.LONGEST_VALUE
        )

        assert result == "Much longer value"

    def test_custom_merge_logic(self, preservation_manager):
        """Test custom merge logic for specific fields."""
        result = preservation_manager._custom_merge_logic(
            "description", "Jira description", "OP description"
        )

        assert "OP description" in result
        assert "Updated from Jira" in result
        assert "Jira description" in result

    def test_create_backup(self, preservation_manager):
        """Test creating entity backup."""
        entity_data = {"id": "1", "name": "Test", "description": "Test entity"}

        backup_path = preservation_manager.create_backup("1", "users", entity_data)

        assert backup_path.exists()
        assert backup_path.parent.name == "users"
        assert "1_" in backup_path.name
        assert backup_path.suffix == ".json"

        # Check backup content
        with backup_path.open() as f:
            backup_data = json.load(f)

        assert backup_data["entity_id"] == "1"
        assert backup_data["entity_type"] == "users"
        assert backup_data["data"] == entity_data
        assert "timestamp" in backup_data

    def test_update_preservation_policy(self, preservation_manager):
        """Test updating preservation policy."""
        original_policy = preservation_manager.preservation_policies["users"].copy()

        policy_updates = {
            "conflict_resolution": "jira_wins",
            "protected_fields": ["id", "email"],
        }

        preservation_manager.update_preservation_policy("users", policy_updates)

        updated_policy = preservation_manager.preservation_policies["users"]
        assert updated_policy["conflict_resolution"] == ConflictResolution.JIRA_WINS
        assert updated_policy["protected_fields"] == ["id", "email"]

        # Other fields unchanged
        assert updated_policy["merge_strategy"] == original_policy["merge_strategy"]

    def test_analyze_preservation_status_no_client(self, preservation_manager):
        """Test preservation status analysis without OpenProject client."""
        preservation_manager.openproject_client = None

        jira_changes = {
            "1": {"name": "Changed Name"},
            "2": {"description": "New Description"},
        }

        report = preservation_manager.analyze_preservation_status(jira_changes, "users")

        assert report["total_conflicts"] == 0
        assert report["conflicts"] == []
        assert "timestamp" in report

    @patch("src.utils.data_preservation_manager.datetime")
    def test_timestamps_use_utc(self, mock_datetime, preservation_manager):
        """Test that all timestamps use UTC."""
        mock_now = Mock()
        mock_now.isoformat.return_value = "2023-01-01T12:00:00+00:00"
        mock_datetime.now.return_value = mock_now

        entity_data = {"id": "1", "name": "Test"}

        preservation_manager.store_original_state("1", "users", entity_data)

        # Should call datetime.now with UTC timezone
        mock_datetime.now.assert_called_with(tz=UTC)

    def test_error_handling_invalid_entity_type(self, preservation_manager):
        """Test error handling for invalid entity types."""
        preservation_manager.update_preservation_policy(
            "invalid_type", {"conflict_resolution": "jira_wins"}
        )

        # Should not crash, just log warning
        assert "invalid_type" not in preservation_manager.preservation_policies

    def test_load_existing_policies(self, temp_preservation_dir):
        """Test loading existing preservation policies from file."""
        # Create policies file with custom policy
        policies_dir = temp_preservation_dir / "policies"
        policies_dir.mkdir(parents=True)

        custom_policies = {
            "custom_entity": {
                "entity_type": "custom_entity",
                "conflict_resolution": "merge",
                "merge_strategy": "concatenate",
                "protected_fields": ["id"],
                "merge_fields": ["description"],
                "track_changes": True,
                "backup_before_update": False,
            }
        }

        policies_file = policies_dir / "preservation_policies.json"
        with policies_file.open("w") as f:
            json.dump(custom_policies, f)

        # Create manager - should load existing policies
        manager = DataPreservationManager(preservation_dir=temp_preservation_dir)

        # Should have both default and custom policies
        assert "users" in manager.preservation_policies  # Default
        assert "custom_entity" in manager.preservation_policies  # Custom

        custom_policy = manager.preservation_policies["custom_entity"]
        assert custom_policy["conflict_resolution"] == ConflictResolution.MERGE
        assert custom_policy["merge_strategy"] == MergeStrategy.CONCATENATE

    def test_get_openproject_entity_data_users(self, preservation_manager):
        """Test fetching user data from OpenProject."""
        # Mock OpenProject client methods
        mock_client = preservation_manager.openproject_client
        mock_client.find_record.return_value = {"id": 1, "email": "test@example.com"}
        mock_client.get_user_by_email.return_value = {"id": 1, "email": "test@example.com"}

        # Test ID-based lookup
        result = preservation_manager._get_openproject_entity_data("1", "users")
        assert result == {"id": 1, "email": "test@example.com"}
        mock_client.find_record.assert_called_with("User", 1)

        # Test email-based lookup (when ID fails)
        mock_client.find_record.side_effect = ValueError("Not an integer")
        result = preservation_manager._get_openproject_entity_data("test@example.com", "users")
        assert result == {"id": 1, "email": "test@example.com"}
        mock_client.get_user_by_email.assert_called_with("test@example.com")

    def test_get_openproject_entity_data_projects(self, preservation_manager):
        """Test fetching project data from OpenProject."""
        # Mock OpenProject client methods
        mock_client = preservation_manager.openproject_client
        mock_client.find_record.return_value = {"id": 1, "identifier": "test-project"}
        mock_client.get_project_by_identifier.return_value = {"id": 1, "identifier": "test-project"}

        # Test ID-based lookup
        result = preservation_manager._get_openproject_entity_data("1", "projects")
        assert result == {"id": 1, "identifier": "test-project"}
        mock_client.find_record.assert_called_with("Project", 1)

        # Test identifier-based lookup (when ID fails)
        mock_client.find_record.side_effect = ValueError("Not an integer")
        result = preservation_manager._get_openproject_entity_data("test-project", "projects")
        assert result == {"id": 1, "identifier": "test-project"}
        mock_client.get_project_by_identifier.assert_called_with("test-project")

    def test_get_openproject_entity_data_work_packages(self, preservation_manager):
        """Test fetching work package data from OpenProject."""
        # Mock OpenProject client methods
        mock_client = preservation_manager.openproject_client
        mock_client.find_record.return_value = {"id": 1, "subject": "Test Work Package"}

        # Test ID-based lookup
        result = preservation_manager._get_openproject_entity_data("1", "work_packages")
        assert result == {"id": 1, "subject": "Test Work Package"}
        mock_client.find_record.assert_called_with("WorkPackage", 1)

    def test_get_openproject_entity_data_custom_fields(self, preservation_manager):
        """Test fetching custom field data from OpenProject."""
        # Mock OpenProject client methods
        mock_client = preservation_manager.openproject_client
        mock_client.find_record.return_value = {"id": 1, "name": "Test Field"}
        mock_client.get_custom_field_by_name.return_value = {"id": 1, "name": "Test Field"}

        # Test ID-based lookup
        result = preservation_manager._get_openproject_entity_data("1", "custom_fields")
        assert result == {"id": 1, "name": "Test Field"}
        mock_client.find_record.assert_called_with("CustomField", 1)

        # Test name-based lookup (when ID fails)
        mock_client.find_record.side_effect = ValueError("Not an integer")
        result = preservation_manager._get_openproject_entity_data("Test Field", "custom_fields")
        assert result == {"id": 1, "name": "Test Field"}
        mock_client.get_custom_field_by_name.assert_called_with("Test Field")

    def test_get_openproject_entity_data_unknown_type(self, preservation_manager):
        """Test fetching data for unknown entity type."""
        # Mock OpenProject client methods
        mock_client = preservation_manager.openproject_client
        mock_client.find_record.return_value = {"id": 1, "name": "Test Entity"}

        # Test generic lookup for unknown type
        result = preservation_manager._get_openproject_entity_data("1", "unknown_types")
        assert result == {"id": 1, "name": "Test Entity"}
        mock_client.find_record.assert_called_with("Unknown_type", 1)

    def test_extract_timestamp_from_string_value(self, preservation_manager):
        """Test timestamp extraction from string values."""
        # Test ISO format with microseconds
        timestamp_str = "2023-12-01T10:30:45.123456Z"
        result = preservation_manager._extract_timestamp_from_value(timestamp_str, "updated")
        assert result is not None
        assert result.year == 2023
        assert result.month == 12
        assert result.day == 1

        # Test ISO format without microseconds
        timestamp_str = "2023-12-01T10:30:45Z"
        result = preservation_manager._extract_timestamp_from_value(timestamp_str, "updated")
        assert result is not None
        assert result.year == 2023

        # Test date only
        timestamp_str = "2023-12-01"
        result = preservation_manager._extract_timestamp_from_value(timestamp_str, "created")
        assert result is not None
        assert result.year == 2023

        # Test invalid timestamp
        result = preservation_manager._extract_timestamp_from_value("invalid", "field")
        assert result is None

    def test_extract_timestamp_from_dict_value(self, preservation_manager):
        """Test timestamp extraction from dictionary values."""
        # Test dict with timestamp field
        value = {
            "id": 1,
            "name": "Test",
            "updated_at": "2023-12-01T10:30:45Z"
        }
        result = preservation_manager._extract_timestamp_from_value(value, "entity")
        assert result is not None
        assert result.year == 2023

        # Test dict without timestamp field
        value = {"id": 1, "name": "Test"}
        result = preservation_manager._extract_timestamp_from_value(value, "entity")
        assert result is None

    def test_merge_field_values_latest_timestamp(self, preservation_manager):
        """Test merging field values using latest timestamp strategy."""
        # Test with timestamp values
        jira_value = "2023-12-01T10:30:45Z"
        op_value = "2023-12-01T09:00:00Z"
        
        result = preservation_manager._merge_field_values(
            "updated", jira_value, op_value, MergeStrategy.LATEST_TIMESTAMP
        )
        assert result == jira_value  # Jira value is more recent

        # Test with reversed timestamps
        jira_value = "2023-12-01T09:00:00Z"
        op_value = "2023-12-01T10:30:45Z"
        
        result = preservation_manager._merge_field_values(
            "updated", jira_value, op_value, MergeStrategy.LATEST_TIMESTAMP
        )
        assert result == op_value  # OpenProject value is more recent

    def test_merge_field_values_concatenate(self, preservation_manager):
        """Test merging field values using concatenate strategy."""
        jira_value = "Description from Jira"
        op_value = "Description from OpenProject"
        
        result = preservation_manager._merge_field_values(
            "description", jira_value, op_value, MergeStrategy.CONCATENATE
        )
        
        expected = f"{op_value}\n\n[Merged from Jira]: {jira_value}"
        assert result == expected

    def test_merge_field_values_longest_value(self, preservation_manager):
        """Test merging field values using longest value strategy."""
        jira_value = "Short"
        op_value = "Much longer description from OpenProject"
        
        result = preservation_manager._merge_field_values(
            "description", jira_value, op_value, MergeStrategy.LONGEST_VALUE
        )
        assert result == op_value  # OpenProject value is longer

    def test_analyze_preservation_status_with_real_data(self, preservation_manager):
        """Test analyzing preservation status with real conflict data."""
        # Mock OpenProject client to return current data
        preservation_manager.openproject_client.find_record.return_value = {
            "id": 1,
            "name": "Modified User",
            "email": "modified@example.com"
        }
        
        # Store original state to create a baseline
        original_data = {"id": 1, "name": "Original User", "email": "original@example.com"}
        preservation_manager.store_original_state("1", "users", original_data)
        
        # Jira changes that conflict with manual changes
        jira_changes = {
            "1": {"name": "Jira User", "description": "New description"}
        }
        
        # Analyze conflicts
        report = preservation_manager.analyze_preservation_status(jira_changes, "users")
        
        assert report["total_conflicts"] == 1
        assert "users" in report["conflicts_by_type"]
        assert report["conflicts_by_type"]["users"] == 1
        assert len(report["conflicts"]) == 1
        
        conflict = report["conflicts"][0]
        assert conflict["entity_id"] == "1"
        assert conflict["entity_type"] == "users"
        assert "name" in conflict["conflicted_fields"]
