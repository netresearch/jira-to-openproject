#!/usr/bin/env python3
"""Tests for the main entry point script."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from src.main import validate_database_configuration


@pytest.mark.unit
@patch("src.config_loader.ConfigLoader")
def test_validate_db_config_success(mock_config_loader_cls, caplog):
    """
    Test validate_database_configuration succeeds when ConfigLoader initializes successfully.
    """
    # Arrange
    mock_loader_instance = MagicMock()
    mock_config_loader_cls.return_value = mock_loader_instance

    # Act
    with caplog.at_level(logging.DEBUG):
        validate_database_configuration()

    # Assert
    mock_config_loader_cls.assert_called_once()
    assert "Database configuration validated successfully" in caplog.text


@pytest.mark.unit
@patch("src.config_loader.ConfigLoader")
def test_validate_db_config_fails_on_runtime_error(mock_config_loader_cls, caplog):
    """
    Test validate_database_configuration exits if ConfigLoader raises RuntimeError.
    This simulates the case where no password is found at all.
    """
    # Arrange
    error_message = (
        "POSTGRES_PASSWORD is required but not found. "
        "Please set the POSTGRES_PASSWORD environment variable "
        "or create a Docker secret at /run/secrets/postgres_password"
    )
    mock_config_loader_cls.side_effect = RuntimeError(error_message)

    # Act & Assert
    with pytest.raises(SystemExit) as excinfo:
        validate_database_configuration()

    assert excinfo.value.code == 1
    assert "Please ensure POSTGRES_PASSWORD is set" in caplog.text


@pytest.mark.unit
@patch("src.config_loader.ConfigLoader")
def test_validate_db_config_fails_on_empty_password(mock_config_loader_cls, caplog):
    """
    Test validate_database_configuration exits if ConfigLoader raises RuntimeError for empty password.
    This is now handled entirely within ConfigLoader during initialization.
    """
    # Arrange - ConfigLoader will raise RuntimeError for empty passwords during __init__
    error_message = "POSTGRES_PASSWORD is required but not found."
    mock_config_loader_cls.side_effect = RuntimeError(error_message)

    # Act & Assert
    with pytest.raises(SystemExit) as excinfo:
        validate_database_configuration()

    assert excinfo.value.code == 1
    assert "Database configuration failed:" in caplog.text


@pytest.mark.unit
@patch("src.config_loader.ConfigLoader")
def test_validate_db_config_fails_on_unexpected_error(mock_config_loader_cls, caplog):
    """
    Test validate_database_configuration exits on an unexpected exception.
    """
    # Arrange
    error_message = "A wild error appears!"
    mock_config_loader_cls.side_effect = Exception(error_message)

    # Act & Assert
    with pytest.raises(SystemExit) as excinfo:
        validate_database_configuration()

    assert excinfo.value.code == 1
    assert "Unexpected error validating database configuration" in caplog.text
    assert error_message in caplog.text 