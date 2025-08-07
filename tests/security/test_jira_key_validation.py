# Comprehensive security tests for JIRA key validation
# Testing against various attack vectors and edge cases

import os
import sys
from unittest.mock import Mock

import pytest

# Add the src directory to the path so we can import our modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from utils.enhanced_timestamp_migrator import EnhancedTimestampMigrator  # noqa: E402
from utils.enhanced_user_association_migrator import (  # noqa: E402
    EnhancedUserAssociationMigrator,
)


class TestJiraKeyValidation:
    """Test JIRA key validation against security vulnerabilities."""

    @pytest.fixture
    def ua_migrator(self):
        """Create UA migrator instance for testing."""
        mock_jira = Mock()
        mock_op = Mock()
        return EnhancedUserAssociationMigrator(mock_jira, mock_op)

    @pytest.fixture
    def ts_migrator(self):
        """Create TS migrator instance for testing."""
        mock_jira = Mock()
        mock_op = Mock()
        return EnhancedTimestampMigrator(mock_jira, mock_op)

    # Valid JIRA key test cases
    @pytest.mark.parametrize(
        "valid_key",
        [
            "PROJ-123",
            "TEST-456",
            "ABC-1",
            "PROJECT-999",
            "TEAM-42",
            "A-1",
            "AA-BB",
            "123-456",
            "A1B2-C3D4",
            "A" * 95 + "-12",  # 99 chars total - should be valid
            "PROJ-" + "1" * 90,  # 100 chars total - should be valid
        ],
    )
    def test_ua_migrator_validate_jira_key_accepts_valid_inputs(
        self,
        ua_migrator,
        valid_key,
    ) -> None:
        """Test that valid JIRA keys are accepted by UA migrator."""
        # Should not raise any exception
        ua_migrator._validate_jira_key(valid_key)

    @pytest.mark.parametrize(
        "valid_key",
        [
            "PROJ-123",
            "TEST-456",
            "ABC-1",
            "PROJECT-999",
            "TEAM-42",
            "A-1",
            "AA-BB",
            "123-456",
            "A1B2-C3D4",
            "A" * 95 + "-12",  # 99 chars total - should be valid
            "PROJ-" + "1" * 90,  # 100 chars total - should be valid
        ],
    )
    def test_ts_migrator_validate_jira_key_accepts_valid_inputs(
        self,
        ts_migrator,
        valid_key,
    ) -> None:
        """Test that valid JIRA keys are accepted by TS migrator."""
        # Should not raise any exception
        ts_migrator._validate_jira_key(valid_key)

    # Invalid JIRA key test cases - comprehensive attack vectors
    @pytest.mark.parametrize(
        ("invalid_key", "description"),
        [
            # Basic format violations
            ("proj-123", "lowercase"),
            ("Proj-123", "mixed case"),
            ("PROJ-123a", "lowercase letter"),
            # SQL injection attempts
            ("PROJ'; DROP TABLE users;", "SQL injection"),
            ("TEST'; DELETE FROM work_packages; --", "SQL injection with comment"),
            ("PROJ\\'; EXEC sp_executesql N'...'", "SQL injection with escape"),
            # HTML/Script injection
            ("TEST<script>alert('xss')</script>", "HTML/script injection"),
            ("PROJ<img src=x onerror=alert(1)>", "HTML injection"),
            # Command injection
            ("TEST`rm -rf /`", "command injection"),
            ("PROJ$(whoami)", "command substitution"),
            ("TEST|cat /etc/passwd", "pipe injection"),
            ("PROJ&& rm -rf /", "command chaining"),
            # Control characters
            ("TEST\nBAD", "newline injection"),
            ("PROJ\rBAD", "carriage return"),
            ("TEST\tBAD", "tab character"),
            ("PROJ\x00NULL", "null byte"),
            ("TEST\x01CTRL", "control character"),
            ("PROJ\x1fCTRL", "unit separator"),
            ("TEST\x7fDEL", "delete character"),
            # Special characters not allowed in JIRA keys
            ("PROJ_123", "underscore"),
            ("TEST 123", "space"),
            ("PROJ.123", "dot"),
            ("TEST@123", "at symbol"),
            ("PROJ#123", "hash"),
            ("TEST%123", "percent"),
            ("PROJ$123", "dollar"),
            ("TEST*123", "asterisk"),
            ("PROJ+123", "plus"),
            ("TEST=123", "equals"),
            ("PROJ/123", "slash"),
            ("TEST\\123", "backslash"),
            ("PROJ[123]", "brackets"),
            ("TEST{123}", "braces"),
            ("PROJ(123)", "parentheses"),
            ("TEST:123", "colon"),
            ("PROJ;123", "semicolon"),
            ("TEST'123", "single quote"),
            ('PROJ"123', "double quote"),
            # Empty/whitespace
            ("", "empty string"),
            ("   ", "whitespace only"),
            # Length violations
            ("A" * 101, "too long"),
            ("PROJ-" + "1" * 96, "101 chars"),
            ("A" * 150, "way too long"),
            ("A" * 1000, "extremely long"),
            # Unicode attacks
            ("TEST\u0000", "unicode null"),
            ("PROJ\u000a", "unicode newline"),
            ("TEST\u0022", "unicode quote"),
            ("PROJ\u0027", "unicode apostrophe"),
            ("TEST\u003c\u003e", "unicode brackets"),
            # Format string attacks
            ("TEST%s%n%x", "format strings"),
            ("PROJ%08x", "format padding"),
            ("TEST%999999999999999999999999999999999999999999d", "format overflow"),
            # Path traversal
            ("../../../etc/passwd", "path traversal"),
            ("..\\..\\..\\windows\\system32", "windows path traversal"),
            ("/etc/passwd", "absolute path"),
            ("C:\\Windows\\System32", "windows absolute path"),
            # Protocol injection
            ("javascript:", "javascript protocol"),
            ("data:", "data protocol"),
            ("file:", "file protocol"),
            ("http://", "http protocol"),
            ("ftp://", "ftp protocol"),
            # LDAP injection
            ("PROJ)(uid=*", "LDAP injection"),
            ("TEST*)(uid=admin", "LDAP wildcard"),
            # NoSQL injection
            ("PROJ'; db.users.drop();", "MongoDB injection"),
            ('TEST"; this.sleep(5000);', "JavaScript injection"),
        ],
    )
    def test_ua_migrator_validate_jira_key_rejects_invalid_inputs(
        self,
        ua_migrator,
        invalid_key,
        description,
    ) -> None:
        """Test that invalid/malicious JIRA keys are rejected by UA migrator."""
        with pytest.raises(ValueError):
            ua_migrator._validate_jira_key(invalid_key)

    @pytest.mark.parametrize(
        ("invalid_key", "description"),
        [
            # Basic format violations
            ("proj-123", "lowercase"),
            ("Proj-123", "mixed case"),
            ("PROJ-123a", "lowercase letter"),
            # SQL injection attempts
            ("PROJ'; DROP TABLE users;", "SQL injection"),
            ("TEST'; DELETE FROM work_packages; --", "SQL injection with comment"),
            ("PROJ\\'; EXEC sp_executesql N'...'", "SQL injection with escape"),
            # HTML/Script injection
            ("TEST<script>alert('xss')</script>", "HTML/script injection"),
            ("PROJ<img src=x onerror=alert(1)>", "HTML injection"),
            # Command injection
            ("TEST`rm -rf /`", "command injection"),
            ("PROJ$(whoami)", "command substitution"),
            ("TEST|cat /etc/passwd", "pipe injection"),
            ("PROJ&& rm -rf /", "command chaining"),
            # Control characters
            ("TEST\nBAD", "newline injection"),
            ("PROJ\rBAD", "carriage return"),
            ("TEST\tBAD", "tab character"),
            ("PROJ\x00NULL", "null byte"),
            ("TEST\x01CTRL", "control character"),
            ("PROJ\x1fCTRL", "unit separator"),
            ("TEST\x7fDEL", "delete character"),
            # Special characters not allowed in JIRA keys
            ("PROJ_123", "underscore"),
            ("TEST 123", "space"),
            ("PROJ.123", "dot"),
            ("TEST@123", "at symbol"),
            ("PROJ#123", "hash"),
            ("TEST%123", "percent"),
            ("PROJ$123", "dollar"),
            ("TEST*123", "asterisk"),
            ("PROJ+123", "plus"),
            ("TEST=123", "equals"),
            ("PROJ/123", "slash"),
            ("TEST\\123", "backslash"),
            ("PROJ[123]", "brackets"),
            ("TEST{123}", "braces"),
            ("PROJ(123)", "parentheses"),
            ("TEST:123", "colon"),
            ("PROJ;123", "semicolon"),
            ("TEST'123", "single quote"),
            ('PROJ"123', "double quote"),
            # Empty/whitespace
            ("", "empty string"),
            ("   ", "whitespace only"),
            # Length violations
            ("A" * 101, "too long"),
            ("PROJ-" + "1" * 96, "101 chars"),
            ("A" * 150, "way too long"),
            ("A" * 1000, "extremely long"),
            # Unicode attacks
            ("TEST\u0000", "unicode null"),
            ("PROJ\u000a", "unicode newline"),
            ("TEST\u0022", "unicode quote"),
            ("PROJ\u0027", "unicode apostrophe"),
            ("TEST\u003c\u003e", "unicode brackets"),
            # Format string attacks
            ("TEST%s%n%x", "format strings"),
            ("PROJ%08x", "format padding"),
            ("TEST%999999999999999999999999999999999999999999d", "format overflow"),
            # Path traversal
            ("../../../etc/passwd", "path traversal"),
            ("..\\..\\..\\windows\\system32", "windows path traversal"),
            ("/etc/passwd", "absolute path"),
            ("C:\\Windows\\System32", "windows absolute path"),
            # Protocol injection
            ("javascript:", "javascript protocol"),
            ("data:", "data protocol"),
            ("file:", "file protocol"),
            ("http://", "http protocol"),
            ("ftp://", "ftp protocol"),
            # LDAP injection
            ("PROJ)(uid=*", "LDAP injection"),
            ("TEST*)(uid=admin", "LDAP wildcard"),
            # NoSQL injection
            ("PROJ'; db.users.drop();", "MongoDB injection"),
            ('TEST"; this.sleep(5000);', "JavaScript injection"),
        ],
    )
    def test_ts_migrator_validate_jira_key_rejects_invalid_inputs(
        self,
        ts_migrator,
        invalid_key,
        description,
    ) -> None:
        """Test that invalid/malicious JIRA keys are rejected by TS migrator."""
        with pytest.raises(ValueError):
            ts_migrator._validate_jira_key(invalid_key)

    def test_validate_jira_key_none_input(self, ua_migrator) -> None:
        """Test that None input is handled properly."""
        with pytest.raises(ValueError):
            ua_migrator._validate_jira_key(None)

    def test_validate_jira_key_error_messages_specific(self, ua_migrator) -> None:
        """Test that error messages are specific and informative."""
        with pytest.raises(ValueError, match=r"empty"):
            ua_migrator._validate_jira_key("")

        with pytest.raises(ValueError, match=r"too long"):
            ua_migrator._validate_jira_key("A" * 101)

        with pytest.raises(ValueError, match=r"format"):
            ua_migrator._validate_jira_key("invalid@key")

    def test_validate_jira_key_boundary_conditions(self, ua_migrator) -> None:
        """Test boundary conditions for JIRA key validation."""
        # Test exactly 100 characters (should be valid)
        exactly_100 = "A" * 95 + "-" + "1" * 4  # 100 chars total
        ua_migrator._validate_jira_key(exactly_100)  # Should not raise

        # Test 101 characters (should be invalid)
        exactly_101 = "A" * 96 + "-" + "1" * 4  # 101 chars total
        with pytest.raises(ValueError, match=r"too long"):
            ua_migrator._validate_jira_key(exactly_101)

    def test_validate_jira_key_regex_pattern_comprehensive(self, ua_migrator) -> None:
        """Test that the regex pattern correctly identifies valid vs invalid formats."""
        # Test that single character project codes work
        ua_migrator._validate_jira_key("A-1")  # Should not raise

        # Test that numeric project codes work
        ua_migrator._validate_jira_key("123-456")  # Should not raise

        # Test that mixed alphanumeric works
        ua_migrator._validate_jira_key("AB12-CD34")  # Should not raise

        # Test that lowercase fails
        with pytest.raises(ValueError):
            ua_migrator._validate_jira_key("abc-123")
