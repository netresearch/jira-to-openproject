"""Security tests for script generation to prevent injection attacks."""

import json
import re
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from src.utils.enhanced_timestamp_migrator import EnhancedTimestampMigrator
from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator

# ---------- Helper Functions ----------


def _create_work_package_mapping(jira_key="SAFE-1", wp_id=42):
    """Return minimal work_package_mapping structure expected by the generators."""
    return {
        wp_id: {
            "jira_key": jira_key,
            "openproject_id": wp_id,
        },
    }


def _verify_script_safety(script, original_jira_key) -> None:
    """Verify that a script properly escapes the jira_key and contains no dangerous patterns."""
    import json

    script_content = script if isinstance(script, str) else str(script)
    escaped_key = json.dumps(original_jira_key)

    # Original key should not appear unescaped in the script
    # (except possibly in comments which are safe)
    script_without_comments = re.sub(r"#.*$", "", script_content, flags=re.MULTILINE)
    script_without_escaped = script_without_comments.replace(escaped_key, "")

    assert original_jira_key not in script_without_escaped, f"Unescaped jira_key '{original_jira_key}' found in script"

    # Escaped key should be present in the script
    assert escaped_key in script_content, f"Escaped jira_key '{escaped_key}' not found in script"

    # Common injection patterns should not be present
    dangerous_patterns = [
        "DROP TABLE",
        "DELETE FROM",
        "UPDATE SET",
        "INSERT INTO",
        "EXEC ",
        "EXECUTE ",
        "sp_executesql",
        "xp_cmdshell",
        "<script>",
        "javascript:",
        "eval(",
        "system(",
        "rm -rf",
        "del /f",
        "format c:",
        "`",
        "$(",
    ]

    for pattern in dangerous_patterns:
        assert pattern not in script_content.upper(), f"Dangerous pattern '{pattern}' found in script"


def _verify_script_has_no_dangerous_patterns(script) -> None:
    """Verify that a script contains no dangerous patterns (for cases where jira_key may not be present)."""
    script_content = script if isinstance(script, str) else str(script)

    # Common injection patterns should not be present
    dangerous_patterns = [
        "DROP TABLE",
        "DELETE FROM",
        "UPDATE SET",
        "INSERT INTO",
        "EXEC ",
        "EXECUTE ",
        "sp_executesql",
        "xp_cmdshell",
        "<script>",
        "javascript:",
        "eval(",
        "system(",
        "rm -rf",
        "del /f",
        "format c:",
        "`",
        "$(",
    ]

    for pattern in dangerous_patterns:
        assert pattern not in script_content.upper(), f"Dangerous pattern '{pattern}' found in script"


# ---------- Fixtures ----------


@pytest.fixture
def ua_migrator(mock_jira_client, mock_op_client):
    """Create user association migrator with mocked dependencies."""
    return EnhancedUserAssociationMigrator(mock_jira_client, mock_op_client)


@pytest.fixture
def ts_migrator(mock_jira_client, mock_op_client):
    """Create timestamp migrator with mocked dependencies."""
    return EnhancedTimestampMigrator(mock_jira_client, mock_op_client)


@pytest.fixture
def clean_cache() -> None:
    """Ensure clean state before each test."""
    return
    # Cleanup is handled by fixture scope


# ---------- User Association Script Generation Tests ----------


@pytest.mark.integration
@pytest.mark.security
def test_author_script_valid_key_is_escaped(ua_migrator, clean_cache) -> None:
    """Test that valid Jira keys are properly escaped in author preservation script."""
    jira_key = "SAFE-123"
    wp_map = _create_work_package_mapping(jira_key=jira_key, wp_id=42)

    # Clear any existing operations
    ua_migrator._rails_operations_cache.clear()

    # Queue a single valid operation
    ua_migrator._queue_rails_author_operation(jira_key, author_id=7, author_data={})

    script = ua_migrator._generate_author_preservation_script(wp_map)

    # Verify script safety
    _verify_script_safety(script, jira_key)

    # Additional checks
    assert "wp.author_id = 7" in script
    assert "WorkPackage.find(42)" in script
    assert "operations <<" in script
    assert "rescue => e" in script


@pytest.mark.integration
@pytest.mark.security
def test_author_script_malicious_key_rejected_before_generation(
    ua_migrator,
    clean_cache,
) -> None:
    """Test that malicious Jira keys are rejected during script generation."""
    malicious_keys = [
        "BAD'; DROP TABLE users;--",
        "PROJ<script>alert(1)</script>",
        "TEST\\nINJECT",
        "EVIL'; EXEC xp_cmdshell('rm -rf /')--",
        "HACK`; system('pwned'); puts '",
    ]

    for malicious_key in malicious_keys:
        # Clear cache for each test
        ua_migrator._rails_operations_cache.clear()

        wp_map = _create_work_package_mapping(jira_key=malicious_key, wp_id=42)

        # Queue operation with malicious key
        ua_migrator._queue_rails_author_operation(
            malicious_key,
            author_id=7,
            author_data={},
        )

        # Script generation should fail at validation
        with pytest.raises(ValueError, match=".*[Jj]ira.*"):
            ua_migrator._generate_author_preservation_script(wp_map)


@pytest.mark.integration
@pytest.mark.security
def test_author_script_multiple_operations_all_escaped(
    ua_migrator,
    clean_cache,
) -> None:
    """Test that multiple operations with different keys are all properly escaped."""
    operations = [
        ("PROJ-123", 10),
        ("TEST-456", 20),
        ("TEAM-789", 30),
    ]

    ua_migrator._rails_operations_cache.clear()

    # Create mapping for all keys
    wp_map = {}
    for i, (jira_key, author_id) in enumerate(operations):
        wp_id = 100 + i
        wp_map[wp_id] = {"jira_key": jira_key, "openproject_id": wp_id}
        ua_migrator._queue_rails_author_operation(
            jira_key,
            author_id=author_id,
            author_data={},
        )

    script = ua_migrator._generate_author_preservation_script(wp_map)

    # Verify all keys are properly escaped
    for jira_key, author_id in operations:
        _verify_script_safety(script, jira_key)
        assert f"wp.author_id = {author_id}" in script

    # Should have multiple operation blocks
    assert script.count("begin") == len(operations)
    assert script.count("rescue => e") == len(operations)


@pytest.mark.integration
@pytest.mark.security
def test_author_script_no_mapping_safe_handling(ua_migrator, clean_cache) -> None:
    """Test that scripts handle missing mappings safely without including unsafe content."""
    # Use a jira_key that hasn't been mapped
    jira_key = "MISSING-123"
    wp_map = {}  # Empty mapping

    # Queue an operation for unmapped key
    ua_migrator._queue_rails_author_operation(jira_key, author_id=7, author_data={})

    script = ua_migrator._generate_author_preservation_script(wp_map)

    # Should not contain any operations since no mapping exists
    assert "WorkPackage.find" not in script
    assert "wp.author_id =" not in script

    # For safety when no mapping exists, just verify no dangerous patterns
    _verify_script_has_no_dangerous_patterns(script)


# ---------- Timestamp Script Generation Tests ----------


@pytest.mark.integration
@pytest.mark.security
def test_timestamp_script_valid_key_is_escaped(ts_migrator, clean_cache) -> None:
    """Test that valid Jira keys are properly escaped in timestamp preservation script."""
    jira_key = "TIME-999"
    wp_map = _create_work_package_mapping(jira_key=jira_key, wp_id=55)

    ts_migrator._rails_operations_cache.clear()

    # Queue a timestamp operation
    operation = {
        "type": "set_created_at",
        "jira_key": jira_key,
        "timestamp": "2023-01-01T00:00:00Z",
        "original_value": "2023-01-01T00:00:00Z",
    }
    ts_migrator._rails_operations_cache.append(operation)

    script = ts_migrator._generate_timestamp_preservation_script(wp_map)

    # Verify script safety
    _verify_script_safety(script, jira_key)

    # Additional checks
    assert "wp.update_columns(created_at: DateTime.parse('2023-01-01T00:00:00Z'))" in script
    assert "WorkPackage.find(55)" in script
    assert "operations <<" in script


@pytest.mark.integration
@pytest.mark.security
def test_timestamp_script_malicious_key_rejected(ts_migrator, clean_cache) -> None:
    """Test that malicious Jira keys are rejected in timestamp script generation."""
    malicious_keys = [
        "TIME\\nINJECT",
        "STAMP'; DROP TABLE logs;--",
        "DATE<img src=x onerror=alert(1)>",
        "EVIL'; system('cat /etc/passwd')#",
    ]

    for malicious_key in malicious_keys:
        ts_migrator._rails_operations_cache.clear()

        wp_map = _create_work_package_mapping(jira_key=malicious_key, wp_id=55)

        operation = {
            "type": "set_updated_at",
            "jira_key": malicious_key,
            "timestamp": "2023-01-01T00:00:00Z",
            "original_value": "2023-01-01T00:00:00Z",
        }
        ts_migrator._rails_operations_cache.append(operation)

        with pytest.raises(ValueError, match=".*[Jj]ira.*"):
            ts_migrator._generate_timestamp_preservation_script(wp_map)


@pytest.mark.integration
@pytest.mark.security
def test_timestamp_script_field_name_also_escaped(ts_migrator, clean_cache) -> None:
    """Test that field names derived from operation types are also escaped."""
    jira_key = "FIELD-123"
    wp_map = _create_work_package_mapping(jira_key=jira_key, wp_id=77)

    ts_migrator._rails_operations_cache.clear()

    # Test various field types
    field_operations = [
        "set_created_at",
        "set_updated_at",
        "set_due_date",
        "set_start_date",
    ]

    for op_type in field_operations:
        operation = {
            "type": op_type,
            "jira_key": jira_key,
            "timestamp": "2023-01-01T00:00:00Z",
            "original_value": "2023-01-01T00:00:00Z",
        }
        ts_migrator._rails_operations_cache.append(operation)

    script = ts_migrator._generate_timestamp_preservation_script(wp_map)

    # Verify all field names are JSON-escaped
    for op_type in field_operations:
        field_name = op_type.replace("set_", "")
        escaped_field = json.dumps(field_name)
        assert escaped_field in script, f"Field {field_name} not properly escaped"

        # Also verify the field is used correctly in wp update_columns call
        assert f"wp.update_columns({field_name}: DateTime.parse" in script


# ---------- Edge Cases and Regression Tests ----------


@pytest.mark.integration
@pytest.mark.security
def test_empty_operations_cache_safe(ua_migrator, ts_migrator) -> None:
    """Test that empty operations cache produces safe empty script."""
    wp_map = _create_work_package_mapping()

    # Ensure caches are empty
    ua_migrator._rails_operations_cache.clear()
    ts_migrator._rails_operations_cache.clear()

    ua_script = ua_migrator._generate_author_preservation_script(wp_map)
    ts_script = ts_migrator._generate_timestamp_preservation_script(wp_map)

    # Scripts should be safe minimal structures
    assert "operations = []" in ua_script
    assert "errors = []" in ua_script
    assert "operations = []" in ts_script
    assert "errors = []" in ts_script

    # Should not contain any dynamic content
    assert "WorkPackage.find" not in ua_script
    assert "WorkPackage.find" not in ts_script


@pytest.mark.integration
@pytest.mark.security
@patch("src.utils.enhanced_user_association_migrator.datetime")
def test_script_generation_deterministic_with_mocked_time(
    mock_datetime,
    ua_migrator,
    clean_cache,
) -> None:
    """Test that script generation is deterministic when time is mocked."""
    # Mock datetime.now to return fixed time
    fixed_time = datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)
    mock_datetime.now.return_value = fixed_time
    mock_datetime.UTC = UTC

    jira_key = "DETERMINISTIC-123"
    wp_map = _create_work_package_mapping(jira_key=jira_key, wp_id=99)

    ua_migrator._rails_operations_cache.clear()
    ua_migrator._queue_rails_author_operation(jira_key, author_id=42, author_data={})

    # Generate script twice
    script1 = ua_migrator._generate_author_preservation_script(wp_map)

    # Clear and re-queue
    ua_migrator._rails_operations_cache.clear()
    ua_migrator._queue_rails_author_operation(jira_key, author_id=42, author_data={})
    script2 = ua_migrator._generate_author_preservation_script(wp_map)

    # Scripts should be identical (deterministic)
    assert script1 == script2
    _verify_script_safety(script1, jira_key)
    _verify_script_safety(script2, jira_key)


@pytest.mark.integration
@pytest.mark.security
def test_large_volume_operations_performance_and_safety(
    ua_migrator,
    clean_cache,
) -> None:
    """Test that large number of operations maintains security and performance."""
    ua_migrator._rails_operations_cache.clear()

    # Create many operations with valid keys
    num_operations = 100
    wp_map = {}

    for i in range(num_operations):
        jira_key = f"BULK-{i:03d}"
        wp_id = 1000 + i
        wp_map[wp_id] = {"jira_key": jira_key, "openproject_id": wp_id}
        ua_migrator._queue_rails_author_operation(
            jira_key,
            author_id=i % 10,
            author_data={},
        )

    script = ua_migrator._generate_author_preservation_script(wp_map)

    # Verify all keys are safely escaped
    for i in range(num_operations):
        jira_key = f"BULK-{i:03d}"
        _verify_script_safety(script, jira_key)

    # Should contain all operations
    assert script.count("begin") == num_operations
    assert script.count("WorkPackage.find") == num_operations

    # Performance check - script shouldn't be excessively large
    assert len(script) < 1000000  # Less than 1MB for 100 operations


@pytest.mark.integration
@pytest.mark.security
def test_unicode_in_valid_keys_handled_safely(ua_migrator, clean_cache) -> None:
    """Test that valid Unicode characters are handled safely."""
    # These should be valid according to the regex but test Unicode handling
    unicode_keys = [
        "PROJ-123",  # ASCII only (baseline)
        "ÜBER-123",  # This should actually fail validation (non A-Z)
        "TEST-∅",  # This should fail (non 0-9)
    ]

    ua_migrator._rails_operations_cache.clear()

    for jira_key in unicode_keys:
        wp_map = _create_work_package_mapping(jira_key=jira_key, wp_id=42)
        ua_migrator._rails_operations_cache.clear()
        ua_migrator._queue_rails_author_operation(jira_key, author_id=1, author_data={})

        try:
            script = ua_migrator._generate_author_preservation_script(wp_map)
            # If we get here, key was valid - verify it's safe
            _verify_script_safety(script, jira_key)
        except ValueError:
            # Expected for non-ASCII characters
            pass
