"""Comprehensive tests for SecurityValidator-based input validation system.

This test suite covers:
1. Core SecurityValidator validation methods
2. Security injection attack prevention
3. Integration with performance components
4. Edge cases and boundary conditions
5. Resource allocation and timing validation
"""

import os
import pytest
import logging
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from typing import Any, Dict

from src.utils.config_validation import (
    SecurityValidator,
    ConfigurationValidationError,
    validate_configuration_dict
)
from src.performance.migration_performance_manager import PerformanceConfig
from src.utils.batch_processor import ThreadSafeBatchProcessor
from src.utils.enhanced_rate_limiter import RateLimitConfig as EnhancedRateLimitConfig
from src.utils.rate_limiter import RateLimitConfig, RateLimitStrategy


class TestSecurityValidatorNumericValidation:
    """Test SecurityValidator numeric parameter validation with comprehensive edge cases."""
    
    @pytest.mark.parametrize("param_name,valid_value,expected_type", [
        ("batch_size", 100, int),
        ("max_workers", 4, int),
        ("rate_limit_per_sec", 50, int),
        ("base_delay", 1.5, float),
        ("max_delay", 30.0, float),
        ("adaptive_factor", 0.8, float),
        ("cache_size", 1000, int),
        ("memory_limit_mb", 512, int),
    ])
    def test_validate_numeric_parameter_valid_values(self, param_name, valid_value, expected_type):
        """Test that valid numeric parameters are accepted and returned with correct type."""
        result = SecurityValidator.validate_numeric_parameter(param_name, valid_value)
        assert isinstance(result, expected_type)
        assert result == valid_value
    
    @pytest.mark.parametrize("param_name,invalid_value,expected_error", [
        ("batch_size", 0, "Value below security minimum"),
        ("batch_size", 1000, "Value exceeds security maximum"),
        ("max_workers", -1, "Value below security minimum"),
        ("rate_limit_per_sec", 0, "Value below security minimum"),
        ("rate_limit_per_sec", 5000, "Value exceeds security maximum"),
        ("base_delay", 0.0, "Value below security minimum"),
        ("max_delay", 500.0, "Value exceeds security maximum"),
        ("adaptive_factor", 0.05, "Value below security minimum"),
        ("adaptive_factor", 1.5, "Value exceeds security maximum"),
    ])
    def test_validate_numeric_parameter_boundary_violations(self, param_name, invalid_value, expected_error):
        """Test that values outside defined bounds are rejected with appropriate errors."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_numeric_parameter(param_name, invalid_value)
        assert expected_error in str(exc.value)
        assert param_name in str(exc.value)
    
    @pytest.mark.parametrize("param_name,string_value,expected_result", [
        ("batch_size", "100", 100),
        ("max_workers", "8", 8),
        ("base_delay", "1.5", 1.5),
        ("memory_limit_mb", "1024", 1024),
    ])
    def test_validate_numeric_parameter_string_coercion(self, param_name, string_value, expected_result):
        """Test that valid numeric strings are properly coerced to expected types."""
        result = SecurityValidator.validate_numeric_parameter(param_name, string_value)
        assert result == expected_result
        
        # Verify type matches expected bounds
        bounds = SecurityValidator.NUMERIC_BOUNDS[param_name]
        assert isinstance(result, bounds['type'])
    
    @pytest.mark.parametrize("param_name,malformed_string", [
        ("batch_size", "100a"),
        ("batch_size", "12.5.3"),
        ("batch_size", "1e5"),
        ("batch_size", "0x64"),
        ("batch_size", "100 "),  # trailing space should be stripped, but this tests malformed pattern
        ("max_workers", "abc"),
        ("base_delay", "1.2.3"),
        ("rate_limit_per_sec", "50%"),
    ])
    def test_validate_numeric_parameter_malformed_strings(self, param_name, malformed_string):
        """Test that malformed numeric strings are rejected."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_numeric_parameter(param_name, malformed_string)
        assert "String contains non-numeric characters" in str(exc.value)
    
    @pytest.mark.parametrize("param_name,invalid_type", [
        ("batch_size", [100]),
        ("batch_size", {"value": 100}),
        ("batch_size", True),
        ("max_workers", object()),
        ("base_delay", complex(1, 2)),
    ])
    def test_validate_numeric_parameter_type_errors(self, param_name, invalid_type):
        """Test that invalid types are rejected with clear error messages."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_numeric_parameter(param_name, invalid_type)
        assert "Type conversion failed" in str(exc.value) or f"got {type(invalid_type).__name__}" in str(exc.value)
    
    def test_validate_numeric_parameter_none_handling(self):
        """Test None value handling with allow_none parameter."""
        # None not allowed by default
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_numeric_parameter("batch_size", None)
        assert "non-null value" in str(exc.value)
        
        # None allowed when specified
        result = SecurityValidator.validate_numeric_parameter("batch_size", None, allow_none=True)
        assert result is None
    
    def test_validate_numeric_parameter_unknown_parameter(self):
        """Test handling of unknown parameters (not in NUMERIC_BOUNDS)."""
        # Valid numeric values should pass through
        result = SecurityValidator.validate_numeric_parameter("unknown_param", 42)
        assert result == 42
        
        # Invalid types should still be rejected
        with pytest.raises(ConfigurationValidationError):
            SecurityValidator.validate_numeric_parameter("unknown_param", "not_a_number")


class TestSecurityValidatorStringValidation:
    """Test SecurityValidator string validation and security sanitization."""
    
    def test_validate_string_parameter_basic_validation(self):
        """Test basic string validation with valid inputs."""
        result = SecurityValidator.validate_string_parameter("project_name", "my_project")
        assert result == "my_project"
        
        # Test empty string handling
        with pytest.raises(ConfigurationValidationError):
            SecurityValidator.validate_string_parameter("project_name", "")
        
        # Test empty string allowed
        result = SecurityValidator.validate_string_parameter("project_name", "", allow_empty=True)
        assert result == ""
    
    def test_validate_string_parameter_length_limits(self):
        """Test string length validation."""
        # Valid length
        result = SecurityValidator.validate_string_parameter("name", "short", max_length=10)
        assert result == "short"
        
        # Too long
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_string_parameter("name", "very_long_string", max_length=5)
        assert "String too long" in str(exc.value)
        assert "potential DoS" in str(exc.value)
    
    @pytest.mark.parametrize("sql_payload", [
        "'; DROP TABLE users--",
        "UNION SELECT * FROM accounts",
        "INSERT INTO foo VALUES('x')",
        "DELETE FROM users WHERE 1=1",
        "admin'--",
        "' OR '1'='1",
        "'; SHUTDOWN--",
        "SELECT * FROM information_schema",
    ])
    def test_validate_string_parameter_sql_injection_detection(self, sql_payload):
        """Test detection of SQL injection patterns."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_string_parameter("input_value", sql_payload)
        assert "SQL injection" in str(exc.value)
    
    @pytest.mark.parametrize("script_payload", [
        "<script>alert('xss')</script>",
        "javascript:void(0)",
        "javascript:alert(1)",
        "data:text/html,<script>alert('test')</script>",
        "vbscript:msgbox('test')",
        "<SCRIPT>document.location='malicious'</SCRIPT>",
    ])
    def test_validate_string_parameter_script_injection_detection(self, script_payload):
        """Test detection of script injection patterns."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_string_parameter("input_value", script_payload)
        assert "script injection" in str(exc.value)
    
    @pytest.mark.parametrize("path_payload", [
        "../../../etc/passwd",
        "..\\..\\windows\\system32",
        "config/../../../etc/shadow",
        "file|rm -rf /",
        "test<>file",
        "path|dangerous_command",
    ])
    def test_validate_string_parameter_path_traversal_detection(self, path_payload):
        """Test detection of path traversal patterns."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_string_parameter("file_path", path_payload)
        assert "path traversal" in str(exc.value)
    
    @pytest.mark.parametrize("control_char_input,expected_sanitized", [
        ("hello\x00world", "helloworld"),
        ("test\x01\x02\x03", "test"),
        ("normal\x7ftext", "normaltext"),
        ("string\x9fwith\x80chars", "stringwithchars"),
        ("\x1fprefix_text", "prefix_text"),
    ])
    def test_validate_string_parameter_control_char_sanitization(self, control_char_input, expected_sanitized):
        """Test that control characters are stripped from strings."""
        result = SecurityValidator.validate_string_parameter("test_param", control_char_input)
        assert result == expected_sanitized
    
    def test_validate_string_parameter_pattern_validation(self):
        """Test custom pattern validation."""
        # Valid pattern match
        result = SecurityValidator.validate_string_parameter(
            "identifier", "valid_name", pattern=r'^[a-zA-Z][a-zA-Z0-9_]*$'
        )
        assert result == "valid_name"
        
        # Invalid pattern match
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_string_parameter(
                "identifier", "123invalid", pattern=r'^[a-zA-Z][a-zA-Z0-9_]*$'
            )
        assert "string matching pattern" in str(exc.value)
    
    def test_validate_string_parameter_type_validation(self):
        """Test that non-string types are rejected."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_string_parameter("param", 123)
        assert f"string (got {type(123).__name__})" in str(exc.value)
        
        with pytest.raises(ConfigurationValidationError):
            SecurityValidator.validate_string_parameter("param", None)


class TestSecurityValidatorFilePathValidation:
    """Test SecurityValidator file path validation and security checks."""
    
    def test_validate_file_path_none_handling(self):
        """Test that None values are handled appropriately."""
        result = SecurityValidator.validate_file_path("config_path", None)
        assert result is None
    
    @patch('pathlib.Path.resolve')
    def test_validate_file_path_valid_paths(self, mock_resolve):
        """Test validation of valid file paths."""
        mock_path = Mock()
        mock_path.is_absolute.return_value = True
        mock_path.exists.return_value = True
        mock_resolve.return_value = mock_path
        
        # Valid string path
        result = SecurityValidator.validate_file_path("config_file", "/valid/path/config.json")
        assert result == mock_path
        
        # Valid Path object
        path_obj = Path("/valid/path")
        result = SecurityValidator.validate_file_path("config_file", path_obj)
        assert result == mock_path
    
    @patch('pathlib.Path.resolve')
    def test_validate_file_path_security_checks(self, mock_resolve):
        """Test security checks for dangerous file paths."""
        mock_path = Mock()
        mock_resolve.return_value = mock_path
        
        # Test sensitive system paths
        dangerous_paths = [
            "/etc/passwd",
            "/proc/version", 
            "/etc/shadow",
            "../../../etc/passwd"
        ]
        
        for dangerous_path in dangerous_paths:
            mock_resolve.return_value.__str__ = lambda: dangerous_path
            with pytest.raises(ConfigurationValidationError) as exc:
                SecurityValidator.validate_file_path("config_file", dangerous_path)
            assert "sensitive system areas" in str(exc.value)
    
    @patch('pathlib.Path.resolve')
    def test_validate_file_path_path_traversal_prevention(self, mock_resolve):
        """Test prevention of path traversal attacks."""
        mock_path = Mock()
        mock_resolve.return_value = mock_path
        mock_resolve.return_value.__str__ = lambda: "/some/path/../../../etc/passwd"
        
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_file_path("config_file", "/some/path/../../../etc/passwd")
        assert "sensitive system areas" in str(exc.value)
    
    @patch('pathlib.Path.resolve')
    def test_validate_file_path_existence_checks(self, mock_resolve):
        """Test file existence validation."""
        mock_path = Mock()
        mock_path.__str__ = lambda: "/safe/path"
        mock_path.is_absolute.return_value = True
        mock_path.exists.return_value = False
        mock_resolve.return_value = mock_path
        
        # Should pass when existence not required
        result = SecurityValidator.validate_file_path("config_file", "/safe/path", must_exist=False)
        assert result == mock_path
        
        # Should fail when existence required
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_file_path("config_file", "/safe/path", must_exist=True)
        assert "existing path" in str(exc.value)
    
    @patch('pathlib.Path.resolve')
    def test_validate_file_path_absolute_requirement(self, mock_resolve):
        """Test absolute path requirement."""
        mock_path = Mock()
        mock_path.__str__ = lambda: "/safe/path"
        mock_path.is_absolute.return_value = False
        mock_resolve.return_value = mock_path
        
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_file_path("config_file", "relative/path", must_be_absolute=True)
        assert "absolute path" in str(exc.value)
    
    def test_validate_file_path_invalid_types(self):
        """Test rejection of invalid path types."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_file_path("config_file", 123)
        assert f"string or Path (got {type(123).__name__})" in str(exc.value)
    
    @patch('pathlib.Path.resolve')
    def test_validate_file_path_resolution_errors(self, mock_resolve):
        """Test handling of path resolution errors."""
        mock_resolve.side_effect = OSError("Permission denied")
        
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_file_path("config_file", "/inaccessible/path")
        assert "Path resolution failed" in str(exc.value)


class TestSecurityValidatorResourceAllocation:
    """Test SecurityValidator resource allocation validation."""
    
    def test_validate_resource_allocation_valid_combinations(self):
        """Test valid resource allocation combinations."""
        # Should pass with reasonable values
        SecurityValidator.validate_resource_allocation(
            batch_size=100, max_workers=4, memory_limit_mb=1024
        )
        
        # Should pass with minimal values
        SecurityValidator.validate_resource_allocation(
            batch_size=1, max_workers=1, memory_limit_mb=64
        )
    
    def test_validate_resource_allocation_memory_exceeded(self):
        """Test detection of memory limit violations."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_resource_allocation(
                batch_size=500,  # 100 MB per worker
                max_workers=20,  # 2 GB total estimated
                memory_limit_mb=512  # Only 512 MB available
            )
        assert "exceeds limit" in str(exc.value)
        assert "resource_allocation" in str(exc.value)
    
    @patch('os.cpu_count')
    def test_validate_resource_allocation_cpu_oversubscription(self, mock_cpu_count):
        """Test detection of CPU oversubscription."""
        mock_cpu_count.return_value = 4
        
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_resource_allocation(
                batch_size=10, max_workers=10, memory_limit_mb=1024  # 10 > 4*2
            )
        assert "system instability" in str(exc.value)
        assert "max_workers" in str(exc.value)
    
    @patch('os.cpu_count')
    def test_validate_resource_allocation_cpu_count_none(self, mock_cpu_count):
        """Test handling when os.cpu_count() returns None."""
        mock_cpu_count.return_value = None
        
        # Should use default of 4 CPUs, so max_workers > 8 should fail
        with pytest.raises(ConfigurationValidationError):
            SecurityValidator.validate_resource_allocation(
                batch_size=10, max_workers=10, memory_limit_mb=1024
            )


class TestSecurityValidatorTimingRelationships:
    """Test SecurityValidator timing relationship validation."""
    
    def test_validate_timing_relationships_valid(self):
        """Test valid timing relationships."""
        # Basic valid relationship
        SecurityValidator.validate_timing_relationships(
            base_delay=1.0, max_delay=10.0
        )
        
        # With min_delay
        SecurityValidator.validate_timing_relationships(
            base_delay=2.0, max_delay=20.0, min_delay=1.0
        )
    
    def test_validate_timing_relationships_base_exceeds_max(self):
        """Test rejection when base_delay exceeds max_delay."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_timing_relationships(
                base_delay=10.0, max_delay=5.0
            )
        assert "max_delay" in str(exc.value)
        assert ">= base_delay" in str(exc.value)
    
    def test_validate_timing_relationships_base_below_min(self):
        """Test rejection when base_delay is below min_delay."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_timing_relationships(
                base_delay=1.0, max_delay=10.0, min_delay=2.0
            )
        assert "base_delay" in str(exc.value)
        assert ">= min_delay" in str(exc.value)
    
    def test_validate_timing_relationships_excessive_ratio(self):
        """Test rejection of excessive delay ratios."""
        with pytest.raises(ConfigurationValidationError) as exc:
            SecurityValidator.validate_timing_relationships(
                base_delay=0.001, max_delay=10.0  # 10000x ratio
            )
        assert "indefinite blocking" in str(exc.value)
        assert "1000x base_delay" in str(exc.value)


class TestSecurityValidatorIntegration:
    """Test SecurityValidator integration with performance components."""
    
    def test_performance_config_validation_success(self):
        """Test successful PerformanceConfig validation."""
        config = PerformanceConfig(
            batch_size=50,
            max_concurrent_batches=3,
            batch_timeout=60.0,
            max_requests_per_minute=200,
            burst_size=20,
            max_retries=5,
            base_delay=0.5,
            max_delay=30.0,
            progress_update_interval=2.0,
            memory_limit_mb=1024
        )
        # Should not raise any exceptions
        assert config.batch_size == 50
        assert config.memory_limit_mb == 1024
    
    def test_performance_config_validation_failure(self):
        """Test PerformanceConfig validation failures."""
        with pytest.raises(ConfigurationValidationError):
            PerformanceConfig(batch_size=0)  # Below minimum
        
        with pytest.raises(ConfigurationValidationError):
            PerformanceConfig(batch_size=1000)  # Above maximum
        
        with pytest.raises(ConfigurationValidationError):
            PerformanceConfig(base_delay=10.0, max_delay=5.0)  # Invalid timing relationship
    
    def test_batch_processor_validation_success(self):
        """Test successful ThreadSafeBatchProcessor validation."""
        processor = ThreadSafeBatchProcessor(
            batch_size=100,
            max_workers=4,
            retry_attempts=3
        )
        assert processor.batch_size == 100
        assert processor.max_workers == 4
        assert processor.retry_attempts == 3
    
    def test_batch_processor_validation_failure(self):
        """Test ThreadSafeBatchProcessor validation failures."""
        with pytest.raises(ConfigurationValidationError):
            ThreadSafeBatchProcessor(batch_size=0)
        
        with pytest.raises(ConfigurationValidationError):
            ThreadSafeBatchProcessor(max_workers=100)  # Exceeds CPU limit
        
        with pytest.raises(ConfigurationValidationError):
            ThreadSafeBatchProcessor(retry_attempts=-1)
    
    def test_rate_limit_config_validation_success(self):
        """Test successful RateLimitConfig validation."""
        config = RateLimitConfig(
            strategy=RateLimitStrategy.ADAPTIVE,
            base_delay=0.1,
            max_delay=30.0,
            min_delay=0.01,
            burst_capacity=15,
            exponential_base=2.0,
            adaptive_threshold=0.5
        )
        assert config.base_delay == 0.1
        assert config.burst_capacity == 15
    
    def test_rate_limit_config_validation_failure(self):
        """Test RateLimitConfig validation failures."""
        with pytest.raises(ConfigurationValidationError):
            RateLimitConfig(base_delay=0.0)  # Below minimum
        
        with pytest.raises(ConfigurationValidationError):
            RateLimitConfig(burst_capacity=0)  # Below minimum
        
        with pytest.raises(ConfigurationValidationError):
            RateLimitConfig(base_delay=10.0, max_delay=5.0)  # Invalid timing
    
    @patch('src.utils.config_validation.logger')
    def test_configuration_validation_error_logging(self, mock_logger):
        """Test that ConfigurationValidationError properly logs warnings."""
        with pytest.raises(ConfigurationValidationError):
            SecurityValidator.validate_numeric_parameter("batch_size", -1)
        
        # Verify warning was logged
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args[0][0]
        assert "Configuration validation failed" in call_args


class TestValidateConfigurationDict:
    """Test the validate_configuration_dict utility function."""
    
    def test_validate_configuration_dict_success(self):
        """Test successful configuration dictionary validation."""
        config_dict = {
            "batch_size": 100,
            "max_workers": 4,
            "base_delay": 1.0,
            "project_name": "test_project",
            "enable_feature": True,
            "optional_value": None
        }
        
        result = validate_configuration_dict(config_dict)
        
        assert result["batch_size"] == 100
        assert result["max_workers"] == 4
        assert result["base_delay"] == 1.0
        assert result["project_name"] == "test_project"
        assert result["enable_feature"] is True
        assert result["optional_value"] is None
    
    def test_validate_configuration_dict_with_expected_keys(self):
        """Test configuration dictionary validation with expected keys."""
        config_dict = {
            "batch_size": 100,
            "max_workers": 4
        }
        
        # Should pass with all expected keys present
        result = validate_configuration_dict(
            config_dict, expected_keys=["batch_size", "max_workers"]
        )
        assert len(result) == 2
        
        # Should fail with missing expected keys
        with pytest.raises(ConfigurationValidationError) as exc:
            validate_configuration_dict(
                config_dict, expected_keys=["batch_size", "max_workers", "missing_key"]
            )
        assert "missing_key" in str(exc.value)
    
    def test_validate_configuration_dict_invalid_types(self):
        """Test configuration dictionary validation with invalid input types."""
        with pytest.raises(ConfigurationValidationError) as exc:
            validate_configuration_dict("not_a_dict")
        assert "dictionary" in str(exc.value)
        
        with pytest.raises(ConfigurationValidationError):
            validate_configuration_dict(None)
    
    def test_validate_configuration_dict_numeric_validation(self):
        """Test that numeric parameters in dictionaries are properly validated."""
        config_dict = {
            "batch_size": 1000,  # Above maximum
            "max_workers": 4
        }
        
        with pytest.raises(ConfigurationValidationError):
            validate_configuration_dict(config_dict)
    
    def test_validate_configuration_dict_string_sanitization(self):
        """Test that string parameters in dictionaries are sanitized."""
        config_dict = {
            "project_name": "valid_name",
            "malicious_input": "'; DROP TABLE--"
        }
        
        with pytest.raises(ConfigurationValidationError):
            validate_configuration_dict(config_dict)


class TestSecurityValidatorErrorHandling:
    """Test comprehensive error handling and edge cases."""
    
    def test_configuration_validation_error_attributes(self):
        """Test ConfigurationValidationError attribute preservation."""
        error = ConfigurationValidationError(
            parameter_name="test_param",
            invalid_value="invalid",
            expected_range="valid range",
            additional_context="extra context"
        )
        
        assert error.parameter_name == "test_param"
        assert error.invalid_value == "invalid"
        assert error.expected_range == "valid range"
        assert error.additional_context == "extra context"
        
        error_str = str(error)
        assert "test_param" in error_str
        assert "invalid" in error_str
        assert "valid range" in error_str
        assert "extra context" in error_str
    
    def test_security_validator_regex_patterns(self):
        """Test that security regex patterns are properly compiled and functional."""
        # Test each pattern individually
        assert SecurityValidator.CONTROL_CHARS_PATTERN.search("\x00test")
        assert SecurityValidator.SQL_INJECTION_PATTERN.search("SELECT * FROM")
        assert SecurityValidator.SCRIPT_INJECTION_PATTERN.search("<script>")
        assert SecurityValidator.PATH_TRAVERSAL_PATTERN.search("../test")
        
        # Test whitelist patterns
        assert SecurityValidator.SAFE_FILENAME_PATTERN.match("test_file.txt")
        assert SecurityValidator.SAFE_IDENTIFIER_PATTERN.match("valid_identifier")
        assert SecurityValidator.NUMERIC_STRING_PATTERN.match("12345")
    
    @pytest.mark.parametrize("cpu_count_value", [None, 1, 2, 4, 8, 16])
    @patch('os.cpu_count')
    def test_numeric_bounds_cpu_dependency(self, mock_cpu_count, cpu_count_value):
        """Test that NUMERIC_BOUNDS correctly handles different CPU counts."""
        mock_cpu_count.return_value = cpu_count_value
        
        # Re-import to get updated bounds with mocked CPU count
        from importlib import reload
        import src.utils.config_validation
        reload(src.utils.config_validation)
        
        expected_cpu = cpu_count_value or 4
        expected_max_workers = min(expected_cpu, 32)
        expected_max_batches = min(expected_cpu, 16)
        
        bounds = src.utils.config_validation.SecurityValidator.NUMERIC_BOUNDS
        assert bounds['max_workers']['max'] == expected_max_workers
        assert bounds['max_concurrent_batches']['max'] == expected_max_batches


if __name__ == "__main__":
    pytest.main([__file__, "-v"]) 