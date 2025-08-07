"""Tests for the Python environment and test fixtures."""

import os
import sys

import pytest


def test_python_version() -> None:
    """Test Python version is 3.12.x or 3.13.x."""
    assert sys.version_info.major == 3
    assert sys.version_info.minor in [12, 13]


@pytest.mark.unit
def test_pytest_environment() -> None:
    """Test that we're running in a pytest environment."""
    assert "PYTEST_CURRENT_TEST" in os.environ, "Not in pytest test context"


@pytest.mark.unit
def test_environment_fixture(test_env: dict[str, str]) -> None:
    """Test that the environment fixture works correctly."""
    # Create a unique test variable
    var_name = "J2O_TEST_ENV_FIXTURE_VAR"
    var_value = "test_value_123"

    # Set the variable using the fixture
    test_env[var_name] = var_value

    # Verify it's set in the environment
    assert os.getenv(var_name) == var_value

    # Clean up is handled by the fixture automatically
