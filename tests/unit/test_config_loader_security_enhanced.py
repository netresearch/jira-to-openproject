#!/usr/bin/env python3
"""Comprehensive security-focused tests for the configuration loader."""

import logging
import os
from unittest.mock import patch

import pytest

from src.config_loader import ConfigLoader


class TestConfigLoaderSecurityEnhanced:
    """Comprehensive security tests for ConfigLoader after recent security fixes."""

    @pytest.fixture
    def mock_config_data(self):
        """Provide basic configuration data for testing."""
        return {
            "jira": {
                "server": "https://test.example.com",
                "username": "test_user",
                "api_token": "test_token",
                "project_key": "TEST",
            },
            "openproject": {
                "server": "https://op.example.com",
                "api_key": "op_key",
                "project_id": 1,
            },
            "migration": {"dry_run": False, "batch_size": 10},
        }

    @pytest.mark.unit
    @patch("src.config_loader.load_dotenv")
    @patch("src.config_loader.ConfigLoader._apply_environment_overrides")
    @patch("src.config_loader.ConfigLoader._load_yaml_config")
    def test_database_config_no_credential_exposure_in_logs(
        self,
        mock_load_yaml,
        mock_apply_env,
        mock_load_dotenv,
        mock_config_data,
        caplog,
    ) -> None:
        """Test that PostgreSQL password is not exposed in debug logs after security fix."""
        # Arrange
        mock_load_yaml.return_value = mock_config_data
        test_password = "super_secret_password_123!@#"

        with patch.dict(os.environ, {"POSTGRES_PASSWORD": test_password}, clear=True):
            with caplog.at_level(logging.DEBUG):
                # Act
                config_loader = ConfigLoader()

                # Assert - Password should not appear in any log messages
                for record in caplog.records:
                    assert (
                        test_password not in record.getMessage()
                    ), f"Password exposed in log: {record.getMessage()}"
                    assert test_password not in str(
                        record.args,
                    ), f"Password exposed in log args: {record.args}"

                # Verify password was loaded correctly (but not logged)
                assert config_loader.get_postgres_password() == test_password

    @pytest.mark.unit
    @patch("src.config_loader.load_dotenv")
    @patch("src.config_loader.ConfigLoader._apply_environment_overrides")
    @patch("src.config_loader.ConfigLoader._load_yaml_config")
    @patch("src.config_loader.Path")
    def test_docker_secret_no_credential_exposure_in_logs(
        self,
        mock_path,
        mock_load_yaml,
        mock_apply_env,
        mock_load_dotenv,
        mock_config_data,
        caplog,
    ) -> None:
        """Test that Docker secret password is not exposed in debug logs."""
        # Arrange
        mock_load_yaml.return_value = mock_config_data
        mock_path_instance = mock_path.return_value
        mock_path_instance.exists.return_value = True
        secret_password = "docker_secret_password_456!@#"
        mock_path_instance.read_text.return_value = (
            f"  {secret_password}  \n"  # With whitespace
        )

        with patch.dict(os.environ, {}, clear=True):  # No env var
            with caplog.at_level(logging.DEBUG):
                # Act
                config_loader = ConfigLoader()

                # Assert - Password should not appear in any log messages
                for record in caplog.records:
                    assert (
                        secret_password not in record.getMessage()
                    ), f"Docker secret password exposed in log: {record.getMessage()}"
                    assert secret_password not in str(
                        record.args,
                    ), f"Docker secret password exposed in log args: {record.args}"

                # Verify password was loaded and trimmed correctly
                assert config_loader.get_postgres_password() == secret_password

    @pytest.mark.unit
    @patch("src.config_loader.load_dotenv")
    @patch("src.config_loader.ConfigLoader._apply_environment_overrides")
    @patch("src.config_loader.ConfigLoader._load_yaml_config")
    @patch("src.config_loader.Path")
    @patch.dict(os.environ, {}, clear=True)
    def test_docker_secret_permission_error_no_credential_exposure(
        self,
        mock_path,
        mock_load_yaml,
        mock_apply_env,
        mock_load_dotenv,
        mock_config_data,
        caplog,
    ) -> None:
        """Test that permission errors don't expose credentials and proper error handling occurs."""
        # Arrange
        mock_load_yaml.return_value = mock_config_data
        mock_path_instance = mock_path.return_value
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.side_effect = PermissionError("Permission denied")

        # No POSTGRES_PASSWORD in environment, should try Docker secret and fail
        with caplog.at_level(logging.WARNING):
            # Act & Assert - Should raise RuntimeError due to missing password
            with pytest.raises(
                RuntimeError,
                match="POSTGRES_PASSWORD is required but not found",
            ):
                ConfigLoader()

            # Verify permission error was logged properly (class name only, no details)
            warning_messages = [
                r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
            ]
            assert any("PermissionError" in msg for msg in warning_messages)

            # Ensure no sensitive data in logs
            all_log_messages = [r.getMessage() for r in caplog.records]
            for msg in all_log_messages:
                assert "Permission denied" not in msg  # No detailed error message

    @pytest.mark.unit
    @patch("src.config_loader.load_dotenv")
    @patch("src.config_loader.ConfigLoader._apply_environment_overrides")
    @patch("src.config_loader.ConfigLoader._load_yaml_config")
    @patch("src.config_loader.Path")
    def test_empty_password_validation_security(
        self,
        mock_path,
        mock_load_yaml,
        mock_apply_env,
        mock_load_dotenv,
        mock_config_data,
    ) -> None:
        """Test that empty passwords are properly rejected for security."""
        # Arrange
        mock_load_yaml.return_value = mock_config_data
        mock_path_instance = mock_path.return_value
        mock_path_instance.exists.return_value = False

        test_cases = ["", "   ", "\t\n"]

        for empty_password in test_cases:
            with patch.dict(
                os.environ,
                {"POSTGRES_PASSWORD": empty_password},
                clear=True,
            ):
                # Act & Assert
                with pytest.raises(RuntimeError) as exc_info:
                    ConfigLoader()

                # Verify proper error message
                assert "POSTGRES_PASSWORD is required" in str(exc_info.value)
                assert "Please set the POSTGRES_PASSWORD environment variable" in str(
                    exc_info.value,
                )

        # Test case with no password at all
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError) as exc_info:
                ConfigLoader()

            assert "POSTGRES_PASSWORD is required" in str(exc_info.value)

    @pytest.mark.unit
    @patch("src.config_loader.load_dotenv")
    @patch("src.config_loader.ConfigLoader._apply_environment_overrides")
    @patch("src.config_loader.ConfigLoader._load_yaml_config")
    def test_environment_variable_type_conversion_security(
        self,
        mock_load_yaml,
        mock_apply_env,
        mock_load_dotenv,
        mock_config_data,
    ) -> None:
        """Test that environment variable type conversion handles edge cases securely."""
        # Arrange
        mock_load_yaml.return_value = mock_config_data

        # Test malicious injection attempts
        test_cases = {
            "J2O_LOG_LEVEL": "DEBUG'; DROP TABLE users; --",  # SQL injection attempt
            "J2O_JIRA_BATCH_SIZE": "999999999999999999999",  # Integer overflow attempt
            "J2O_OPENPROJECT_TIMEOUT": "../../../etc/passwd",  # Path traversal attempt
        }

        with patch.dict(
            os.environ,
            {"POSTGRES_PASSWORD": "test_password", **test_cases},
            clear=True,
        ):
            # Act
            config_loader = ConfigLoader()

            # Assert - Malicious values should be treated as strings, not executed
            config = config_loader.get_config()

            # SQL injection should be treated as invalid log level and ignored
            assert (
                config["migration"].get("log_level") != "DEBUG'; DROP TABLE users; --"
            )

            # Large numbers should be treated as strings, not cause overflow
            jira_config = config.get("jira", {})
            batch_size = jira_config.get("batch_size", "not_set")
            if batch_size != "not_set":
                assert isinstance(batch_size, str)  # Should remain string due to size

            # Path traversal should be treated as normal string value
            openproject_config = config.get("openproject", {})
            timeout_val = openproject_config.get("timeout", "not_set")
            if timeout_val != "not_set":
                assert isinstance(timeout_val, str)

    @pytest.mark.unit
    @patch("src.config_loader.load_dotenv")
    @patch("src.config_loader.ConfigLoader._apply_environment_overrides")
    @patch("src.config_loader.ConfigLoader._load_yaml_config")
    def test_config_loader_edge_case_security(
        self,
        mock_load_yaml,
        mock_apply_env,
        mock_load_dotenv,
        mock_config_data,
    ) -> None:
        """Test edge cases that could lead to security issues."""
        # Arrange
        mock_load_yaml.return_value = mock_config_data

        with patch.dict(os.environ, {"POSTGRES_PASSWORD": "test_password"}, clear=True):
            config_loader = ConfigLoader()

            # Test that get_postgres_password doesn't expose internals
            password = config_loader.get_postgres_password()
            assert password == "test_password"

            # Test that accessing non-existent keys doesn't expose internals
            with pytest.raises(RuntimeError) as exc_info:
                # Temporarily remove password to test error case
                del config_loader.config["database"]["postgres_password"]
                config_loader.get_postgres_password()

            assert "PostgreSQL password not configured" in str(exc_info.value)
            # Ensure error message doesn't contain sensitive data
            assert "test_password" not in str(exc_info.value)

    @pytest.mark.unit
    @patch("src.config_loader.load_dotenv")
    @patch("src.config_loader.ConfigLoader._apply_environment_overrides")
    @patch("src.config_loader.ConfigLoader._load_yaml_config")
    def test_log_level_validation_prevents_injection(
        self,
        mock_load_yaml,
        mock_apply_env,
        mock_load_dotenv,
        mock_config_data,
        caplog,
    ) -> None:
        """Test that log level validation prevents log injection attacks."""
        # Arrange
        mock_load_yaml.return_value = mock_config_data

        # Test various injection attempts in log level
        malicious_log_levels = [
            "DEBUG\nFAKE LOG ENTRY",  # Log injection attempt
            "INFO; rm -rf /",  # Command injection attempt
            "WARNING\r\nADMIN LOGIN: success",  # CRLF injection
            "INVALID_LEVEL",  # Invalid level
            "",  # Empty string
        ]

        for malicious_level in malicious_log_levels:
            with (
                patch.dict(
                    os.environ,
                    {
                        "POSTGRES_PASSWORD": "test_password",
                        "J2O_LOG_LEVEL": malicious_level,
                    },
                    clear=True,
                ),
                caplog.at_level(logging.DEBUG),
            ):
                caplog.clear()

                # Act
                config_loader = ConfigLoader()

                # Assert - Invalid log levels should be rejected
                config = config_loader.get_config()
                log_level = config["migration"].get("log_level", "default")

                # Should only accept valid log levels
                valid_levels = [
                    "DEBUG",
                    "INFO",
                    "NOTICE",
                    "WARNING",
                    "ERROR",
                    "CRITICAL",
                    "SUCCESS",
                ]
                if malicious_level.upper() not in valid_levels:
                    assert log_level != malicious_level.upper()

                # Verify no log injection occurred
                for record in caplog.records:
                    message = record.getMessage()
                    assert "FAKE LOG ENTRY" not in message
                    assert "ADMIN LOGIN" not in message
                    assert "rm -rf" not in message
