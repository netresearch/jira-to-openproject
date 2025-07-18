"""Regression tests to ensure security fixes don't break existing functionality."""

from unittest.mock import Mock, patch
from datetime import datetime, UTC

import pytest
from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator
from src.utils.enhanced_timestamp_migrator import EnhancedTimestampMigrator


@pytest.fixture
def ua_migrator(mock_jira_client, mock_op_client):
    """Create user association migrator with mocked dependencies."""
    return EnhancedUserAssociationMigrator(mock_jira_client, mock_op_client)


@pytest.fixture
def ts_migrator(mock_jira_client, mock_op_client):
    """Create timestamp migrator with mocked dependencies."""
    return EnhancedTimestampMigrator(mock_jira_client, mock_op_client)


def _create_work_package_mapping(jira_key="PROJ-123", wp_id=42):
    """Create basic work package mapping for testing."""
    return {
        wp_id: {
            "jira_key": jira_key,
            "openproject_id": wp_id,
        }
    }


# ---------- User Association Migrator Regression Tests ----------

@pytest.mark.regression
@pytest.mark.security
def test_ua_normal_operation_workflow_still_works(ua_migrator):
    """Test that normal user association workflow continues to work."""
    jira_key = "NORMAL-123"
    author_id = 42
    wp_id = 100
    
    ua_migrator._rails_operations_cache.clear()
    
    # Queue normal operation
    ua_migrator._queue_rails_author_operation(
        jira_key=jira_key,
        author_id=author_id,
        author_data={"display_name": "John Doe", "email": "john@example.com"}
    )
    
    # Verify operation was queued correctly
    assert len(ua_migrator._rails_operations_cache) == 1
    operation = ua_migrator._rails_operations_cache[0]
    assert operation["jira_key"] == jira_key
    assert operation["author_id"] == author_id
    assert operation["type"] == "set_author"
    assert "timestamp" in operation
    
    # Generate script
    wp_map = _create_work_package_mapping(jira_key=jira_key, wp_id=wp_id)
    script = ua_migrator._generate_author_preservation_script(wp_map)
    
    # Verify script contains expected content
    assert f"WorkPackage.find({wp_id})" in script
    assert f"wp.author_id = {author_id}" in script
    assert "operations << {" in script
    assert "rescue => e" in script
    assert "puts \"Author preservation completed:\"" in script


@pytest.mark.regression
@pytest.mark.security
def test_ua_multiple_operations_batch_processing(ua_migrator):
    """Test that batch processing of multiple operations works correctly."""
    operations_data = [
        ("PROJ-001", 10, {"name": "Alice"}),
        ("PROJ-002", 20, {"name": "Bob"}),
        ("PROJ-003", 30, {"name": "Charlie"}),
    ]
    
    ua_migrator._rails_operations_cache.clear()
    
    # Queue multiple operations
    for jira_key, author_id, author_data in operations_data:
        ua_migrator._queue_rails_author_operation(jira_key, author_id, author_data)
    
    # Verify all operations queued
    assert len(ua_migrator._rails_operations_cache) == len(operations_data)
    
    # Create mapping for all
    wp_map = {}
    for i, (jira_key, _, _) in enumerate(operations_data):
        wp_id = 200 + i
        wp_map[wp_id] = {"jira_key": jira_key, "openproject_id": wp_id}
    
    # Generate script
    script = ua_migrator._generate_author_preservation_script(wp_map)
    
    # Verify all operations present in script
    for jira_key, author_id, _ in operations_data:
        assert f"wp.author_id = {author_id}" in script
    
    # Should have correct number of operation blocks
    assert script.count("begin") == len(operations_data)
    assert script.count("rescue => e") == len(operations_data)


@pytest.mark.regression
@pytest.mark.security
@patch('src.utils.enhanced_user_association_migrator.config')
def test_ua_error_handling_preserved(mock_config, ua_migrator):
    """Test that error handling in normal operations is preserved."""
    # This tests that our security changes don't affect error handling
    jira_key = "ERROR-123"
    
    ua_migrator._rails_operations_cache.clear()
    ua_migrator._queue_rails_author_operation(jira_key, author_id=99, author_data={})
    
    # Generate script with no mapping (should handle gracefully)
    wp_map = {}  # Empty mapping
    script = ua_migrator._generate_author_preservation_script(wp_map)
    
    # Should still generate valid script structure
    assert "operations = []" in script
    assert "errors = []" in script
    assert "puts \"Author preservation completed:\"" in script
    assert "puts \"Errors: #{errors.length}\"" in script
    
    # Should not contain any WorkPackage operations
    assert "WorkPackage.find" not in script
    assert "wp.author_id =" not in script


# ---------- Timestamp Migrator Regression Tests ----------

@pytest.mark.regression
@pytest.mark.security
def test_ts_normal_timestamp_operation_works(ts_migrator):
    """Test that normal timestamp operations continue to work."""
    jira_key = "TIMESTAMP-456"
    wp_id = 300
    timestamp = "2023-01-01T12:00:00Z"
    
    ts_migrator._rails_operations_cache.clear()
    
    # Queue normal timestamp operation
    operation = {
        "type": "set_created_at",
        "jira_key": jira_key,
        "timestamp": timestamp,
        "original_value": "2023-01-01T10:00:00Z",
    }
    ts_migrator._rails_operations_cache.append(operation)
    
    # Generate script
    wp_map = _create_work_package_mapping(jira_key=jira_key, wp_id=wp_id)
    script = ts_migrator._generate_timestamp_preservation_script(wp_map)
    
    # Verify script contains expected content
    assert f"WorkPackage.find({wp_id})" in script
    assert f"wp.created_at = DateTime.parse('{timestamp}')" in script
    assert "operations << {" in script
    assert "rescue => e" in script
    assert "puts \"Timestamp preservation completed:\"" in script


@pytest.mark.regression
@pytest.mark.security
def test_ts_different_field_types_work(ts_migrator):
    """Test that different timestamp field types continue to work."""
    jira_key = "FIELDS-789"
    wp_id = 400
    base_timestamp = "2023-01-01T00:00:00Z"
    
    field_operations = [
        "set_created_at",
        "set_updated_at",
        "set_due_date",
        "set_start_date",
    ]
    
    ts_migrator._rails_operations_cache.clear()
    
    # Queue operations for different field types
    for op_type in field_operations:
        operation = {
            "type": op_type,
            "jira_key": jira_key,
            "timestamp": base_timestamp,
            "original_value": base_timestamp,
        }
        ts_migrator._rails_operations_cache.append(operation)
    
    # Generate script
    wp_map = _create_work_package_mapping(jira_key=jira_key, wp_id=wp_id)
    script = ts_migrator._generate_timestamp_preservation_script(wp_map)
    
    # Verify all field types are handled
    for op_type in field_operations:
        field_name = op_type.replace("set_", "")
        assert f"wp.{field_name} = DateTime.parse('{base_timestamp}')" in script
    
    # Should have all operation blocks
    assert script.count("begin") == len(field_operations)
    assert script.count("rescue => e") == len(field_operations)


# ---------- Integration and Performance Regression Tests ----------

@pytest.mark.regression
@pytest.mark.security
def test_combined_operations_ua_and_ts_independently(ua_migrator, ts_migrator):
    """Test that UA and TS migrators work independently without interference."""
    # Setup for user association
    ua_jira_key = "COMBINED-UA-123"
    ua_wp_id = 500
    ua_migrator._rails_operations_cache.clear()
    ua_migrator._queue_rails_author_operation(ua_jira_key, author_id=50, author_data={})
    
    # Setup for timestamp
    ts_jira_key = "COMBINED-TS-456"
    ts_wp_id = 600
    ts_migrator._rails_operations_cache.clear()
    ts_operation = {
        "type": "set_created_at",
        "jira_key": ts_jira_key,
        "timestamp": "2023-01-01T00:00:00Z",
        "original_value": "2023-01-01T00:00:00Z",
    }
    ts_migrator._rails_operations_cache.append(ts_operation)
    
    # Generate scripts independently
    ua_wp_map = _create_work_package_mapping(jira_key=ua_jira_key, wp_id=ua_wp_id)
    ts_wp_map = _create_work_package_mapping(jira_key=ts_jira_key, wp_id=ts_wp_id)
    
    ua_script = ua_migrator._generate_author_preservation_script(ua_wp_map)
    ts_script = ts_migrator._generate_timestamp_preservation_script(ts_wp_map)
    
    # Verify scripts are independent and correct
    assert f"wp.author_id = 50" in ua_script
    assert f"WorkPackage.find({ua_wp_id})" in ua_script
    assert "wp.created_at = DateTime.parse" in ts_script
    assert f"WorkPackage.find({ts_wp_id})" in ts_script
    
    # Scripts should not contain each other's content
    assert "wp.author_id" not in ts_script
    assert "wp.created_at" not in ua_script


@pytest.mark.regression
@pytest.mark.security
def test_performance_baseline_maintained(ua_migrator):
    """Test that performance characteristics are maintained after security fixes."""
    import time
    
    # Create moderate number of operations
    num_operations = 50
    jira_keys = [f"PERF-{i:03d}" for i in range(num_operations)]
    
    ua_migrator._rails_operations_cache.clear()
    
    # Queue operations
    start_time = time.time()
    for i, jira_key in enumerate(jira_keys):
        ua_migrator._queue_rails_author_operation(jira_key, author_id=i, author_data={})
    queue_time = time.time() - start_time
    
    # Create work package mapping
    wp_map = {}
    for i, jira_key in enumerate(jira_keys):
        wp_id = 700 + i
        wp_map[wp_id] = {"jira_key": jira_key, "openproject_id": wp_id}
    
    # Generate script
    start_time = time.time()
    script = ua_migrator._generate_author_preservation_script(wp_map)
    generation_time = time.time() - start_time
    
    # Performance assertions (reasonable thresholds)
    assert queue_time < 1.0, f"Queueing took too long: {queue_time:.2f}s"
    assert generation_time < 2.0, f"Script generation took too long: {generation_time:.2f}s"
    assert len(script) > 0, "Script should not be empty"
    
    # Verify all operations were processed
    assert script.count("begin") == num_operations
    assert script.count("wp.author_id =") == num_operations


# ---------- Data Integrity Regression Tests ----------

@pytest.mark.regression
@pytest.mark.security
def test_operation_metadata_preserved(ua_migrator):
    """Test that operation metadata is preserved correctly."""
    jira_key = "METADATA-999"
    author_data = {
        "display_name": "Test User",
        "email": "test@example.com",
        "account_id": "test-123",
        "active": True
    }
    
    ua_migrator._rails_operations_cache.clear()
    
    # Queue operation with metadata
    ua_migrator._queue_rails_author_operation(
        jira_key=jira_key,
        author_id=99,
        author_data=author_data
    )
    
    # Verify metadata is preserved in cache
    operation = ua_migrator._rails_operations_cache[0]
    assert operation["author_metadata"] == author_data
    assert operation["jira_key"] == jira_key
    assert operation["author_id"] == 99
    assert operation["type"] == "set_author"
    assert "timestamp" in operation
    
    # Ensure timestamp is valid ISO format
    timestamp = operation["timestamp"]
    # Should be parseable as datetime
    parsed_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    assert parsed_time is not None


@pytest.mark.regression
@pytest.mark.security
def test_script_output_format_unchanged(ua_migrator, ts_migrator):
    """Test that script output format hasn't changed (for downstream compatibility)."""
    # Test UA script format
    ua_jira_key = "FORMAT-UA"
    ua_migrator._rails_operations_cache.clear()
    ua_migrator._queue_rails_author_operation(ua_jira_key, author_id=123, author_data={})
    
    ua_wp_map = _create_work_package_mapping(jira_key=ua_jira_key, wp_id=800)
    ua_script = ua_migrator._generate_author_preservation_script(ua_wp_map)
    
    # Verify UA script structure
    expected_ua_elements = [
        "# Enhanced Author Preservation Script",
        "require 'json'",
        "operations = []",
        "errors = []",
        "puts \"Author preservation completed:\"",
        "puts \"Successful operations: #{operations.length}\"",
        "puts \"Errors: #{errors.length}\"",
    ]
    
    for element in expected_ua_elements:
        assert element in ua_script, f"Missing element in UA script: {element}"
    
    # Test TS script format
    ts_jira_key = "FORMAT-TS"
    ts_migrator._rails_operations_cache.clear()
    ts_operation = {
        "type": "set_created_at",
        "jira_key": ts_jira_key,
        "timestamp": "2023-01-01T00:00:00Z",
        "original_value": "2023-01-01T00:00:00Z",
    }
    ts_migrator._rails_operations_cache.append(ts_operation)
    
    ts_wp_map = _create_work_package_mapping(jira_key=ts_jira_key, wp_id=900)
    ts_script = ts_migrator._generate_timestamp_preservation_script(ts_wp_map)
    
    # Verify TS script structure
    expected_ts_elements = [
        "# Enhanced Timestamp Preservation Script",
        "require 'json'",
        "operations = []",
        "errors = []",
        "puts \"Timestamp preservation completed:\"",
        "puts \"Successful operations: #{operations.length}\"",
        "puts \"Errors: #{errors.length}\"",
    ]
    
    for element in expected_ts_elements:
        assert element in ts_script, f"Missing element in TS script: {element}"


@pytest.mark.regression
@pytest.mark.security
def test_valid_jira_keys_edge_cases_still_work(ua_migrator):
    """Test that edge cases of valid Jira keys continue to work."""
    edge_case_keys = [
        "A-1",                    # Minimal valid key
        "PROJECT-123456789",      # Long number
        "TEAM-1",                 # Single digit
        "A1B2C3-D4E5F6",         # Mixed alphanumeric
        "123-456",               # All numeric
        "ABC-DEF",               # All letters
        "A" * 95 + "-1234",      # Near length limit (100 chars)
    ]
    
    for jira_key in edge_case_keys:
        ua_migrator._rails_operations_cache.clear()
        ua_migrator._queue_rails_author_operation(jira_key, author_id=1, author_data={})
        
        wp_map = _create_work_package_mapping(jira_key=jira_key, wp_id=42)
        
        # Should not raise any exceptions
        script = ua_migrator._generate_author_preservation_script(wp_map)
        
        # Should contain the operation
        assert "wp.author_id = 1" in script
        assert "WorkPackage.find(42)" in script
        
        # Key should be properly escaped in output
        import json
        escaped_key = json.dumps(jira_key)
        assert escaped_key in script 