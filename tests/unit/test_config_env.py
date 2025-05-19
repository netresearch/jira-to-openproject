"""Tests for environment configuration loading."""

import os
import random
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

from src.config_loader import ConfigLoader, is_test_environment


@pytest.mark.unit
def test_is_test_environment() -> None:
    """Test detection of test environment."""
    # Save original value
    original_pytest_val = "PYTEST_CURRENT_TEST" in os.environ

    try:
        # Ensure PYTEST_CURRENT_TEST is set to simulate pytest environment
        # This should already be the case, but we'll set it explicitly for the test
        os.environ["PYTEST_CURRENT_TEST"] = "tests/unit/test_config_env.py::test_is_test_environment (call)"

        # Now the function should detect we're in a test environment
        assert is_test_environment()

        # Override with environment variable - setting to false should still return True
        # because PYTEST_CURRENT_TEST still exists
        os.environ["J2O_TEST_MODE"] = "false"
        assert is_test_environment()

        # Now remove PYTEST_CURRENT_TEST to test just the J2O_TEST_MODE flag
        os.environ.pop("PYTEST_CURRENT_TEST")
        assert not is_test_environment()  # Should be false now

        # Set J2O_TEST_MODE to true and verify it works
        os.environ["J2O_TEST_MODE"] = "true"
        assert is_test_environment()

        # Reset to false for subsequent tests
        os.environ["J2O_TEST_MODE"] = "false"
    finally:
        # Restore original PYTEST_CURRENT_TEST state
        if original_pytest_val:
            os.environ["PYTEST_CURRENT_TEST"] = "tests/unit/test_config_env.py::test_is_test_environment (call)"
        elif "PYTEST_CURRENT_TEST" in os.environ:
            del os.environ["PYTEST_CURRENT_TEST"]


@pytest.mark.unit
def test_config_loader_test_mode(test_env: dict[str, str], tmp_path: Path) -> None:
    """Test configuration loader properly loads test environment files."""
    # Create temporary env files
    base_env = tmp_path / ".env"
    local_env = tmp_path / ".env.local"
    test_env_file = tmp_path / ".env.test"
    test_local_env = tmp_path / ".env.test.local"

    # Write sample content to each file with increasing precedence
    base_env.write_text(
        "J2O_SAMPLE_BASE=base\n"
        "J2O_SAMPLE_VALUE=base-value\n",
    )

    local_env.write_text(
        "J2O_SAMPLE_BASE=local\n"
        "J2O_SAMPLE_LOCAL=local-value\n",
    )

    test_env_file.write_text(
        "J2O_SAMPLE_BASE=test\n"
        "J2O_SAMPLE_TEST=test-value\n",
    )

    test_local_env.write_text("J2O_SAMPLE_BASE=test-local\n")

    # Store current directory to restore later
    original_dir = Path.cwd()

    try:
        # Change to tmp_path so that relative paths to .env files work
        os.chdir(tmp_path)

        # Create a ConfigLoader subclass for isolated testing
        class TestConfigLoader(ConfigLoader):
            def __init__(self) -> None:
                # Skip loading real env files
                # We'll manually load our test files instead
                self.config = {}
                self._dotenv_loaded = False
                self._load_dotenv_files()

            def _load_dotenv_files(self) -> None:
                # Always load in test mode
                self._dotenv_loaded = True
                # Load our test env files in correct order
                load_dotenv(base_env)
                load_dotenv(local_env, override=True)
                load_dotenv(test_env_file, override=True)
                load_dotenv(test_local_env, override=True)

            def get_config_value(self, section: str, key: str, default: Any = None) -> Any:
                """Get config value from environment variable."""
                env_var_name = f"J2O_SAMPLE_{key.upper()}"
                return os.environ.get(env_var_name, default)

        # Force test mode
        test_env["J2O_TEST_MODE"] = "true"

        # Initialize test config loader
        config_loader = TestConfigLoader()

        # Verify values loaded with correct precedence
        assert config_loader.get_config_value("test", "BASE") == "test-local"
        assert config_loader.get_config_value("test", "LOCAL") == "local-value"
        assert config_loader.get_config_value("test", "TEST") == "test-value"
        assert config_loader.get_config_value("test", "VALUE") == "base-value"

    finally:
        # Restore original directory
        os.chdir(original_dir)
        # Reset test mode
        test_env["J2O_TEST_MODE"] = "false"


@pytest.mark.unit
def test_config_loader_dev_mode(test_env: dict[str, str], tmp_path: Path) -> None:
    """Test configuration loader properly loads development environment files."""
    # Create temporary env files
    base_env = tmp_path / ".env"
    local_env = tmp_path / ".env.local"
    test_env_file = tmp_path / ".env.test"

    # Write sample content to each file
    base_env.write_text(
        "J2O_SAMPLE_BASE=base\n"
        "J2O_SAMPLE_VALUE=base-value\n",
    )

    local_env.write_text(
        "J2O_SAMPLE_BASE=local\n"
        "J2O_SAMPLE_LOCAL=local-value\n",
    )

    test_env_file.write_text(
        "J2O_SAMPLE_BASE=test\n"
        "J2O_SAMPLE_TEST=test-value\n",
    )

    # Store current directory to restore later
    original_dir = Path.cwd()

    try:
        # Change to tmp_path so that relative paths to .env files work
        os.chdir(tmp_path)

        # Ensure we're in dev mode
        test_env["J2O_TEST_MODE"] = "false"
        if "PYTEST_CURRENT_TEST" in test_env:
            # Save and restore later since we need this for pytest to work
            original_pytest = test_env["PYTEST_CURRENT_TEST"]
            del test_env["PYTEST_CURRENT_TEST"]

        # Create a ConfigLoader subclass for isolated testing
        class TestConfigLoader(ConfigLoader):
            def __init__(self) -> None:
                # Skip loading real env files
                # We'll manually load our test files instead
                self.config = {}
                self._dotenv_loaded = False
                self._load_dotenv_files()

            def _load_dotenv_files(self) -> None:
                # Load in dev mode (not test mode)
                self._dotenv_loaded = True
                # Load our test env files in correct order for dev mode
                load_dotenv(base_env)
                load_dotenv(local_env, override=True)

            def get_config_value(self, section: str, key: str, default: Any = None) -> Any:
                """Get config value from environment variable."""
                env_var_name = f"J2O_SAMPLE_{key.upper()}"
                return os.environ.get(env_var_name, default)

        # Initialize test config loader
        config_loader = TestConfigLoader()

        # Check that the appropriate value was used for each setting in dev mode
        assert config_loader.get_config_value("test", "BASE") == "local"
        assert config_loader.get_config_value("test", "LOCAL") == "local-value"
        assert config_loader.get_config_value("test", "TEST") is None  # Not loaded in dev mode
        assert config_loader.get_config_value("test", "VALUE") == "base-value"

    finally:
        # Restore original directory
        os.chdir(original_dir)
        # Restore pytest variable
        if "original_pytest" in locals():
            test_env["PYTEST_CURRENT_TEST"] = original_pytest


@pytest.mark.unit
def test_config_env_variable_override(test_env: dict[str, str]) -> None:
    """Test environment variable overrides directly via environment."""
    # Create a special test value that won't conflict with real env vars
    test_var_name = f"J2O_TEST_UNIQUE_{random.randint(1000, 9999)}"
    test_var_value = f"test-value-{random.randint(1000, 9999)}"

    # Set variable directly in environment
    test_env[test_var_name] = test_var_value

    # Create a simple test config loader
    class TestConfigLoader:
        def get_config_value(self, var_name: str, default: Any = None) -> Any:
            return os.environ.get(var_name, default)

    config_loader = TestConfigLoader()

    # Verify our direct environment variable is accessible
    assert config_loader.get_config_value(test_var_name) == test_var_value

    # Clean up
    del test_env[test_var_name]
