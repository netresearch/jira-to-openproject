"""Example of how to use the test_env fixture in tests."""

import os
import random

import pytest


@pytest.mark.unit
def test_example_with_environment_override(test_env: dict[str, str]) -> None:
    """Example test showing how to override environment variables.

    This test demonstrates how to use the test_env fixture to:
    1. Override specific environment variables for this test only
    2. Ensure those changes are automatically reverted after the test

    Args:
        test_env: Dictionary fixture that provides access to modify environment variables

    """
    # Create a special variable that won't interfere with existing variables
    test_url = f"https://example-{random.randint(10000, 99999)}.com"
    test_var = f"J2O_TEST_UNIQUE_{random.randint(10000, 99999)}"

    # Set the variables
    test_env[test_var] = test_url

    # Create a simple config loader that directly reads environment vars
    class TestConfigLoader:
        def get_config_value(self, var_name: str) -> str:
            return os.environ.get(var_name)

    # Create an instance of our test config loader
    config_loader = TestConfigLoader()

    # Verify our overrides took effect
    assert config_loader.get_config_value(test_var) == test_url

    # Also demonstrate that the variable will be automatically reset
    # after the test completes

    # Clean up
    del test_env[test_var]


@pytest.mark.unit
def test_example_demonstrating_isolation(test_env: dict[str, str]) -> None:
    """Example showing environment variables are isolated between tests.

    This test demonstrates that changes made in the previous test
    don't affect this test.

    Args:
        test_env: Dictionary fixture that provides access to modify environment variables

    """
    # This should no longer have the overridden values from the previous test
    assert test_env.get("J2O_JIRA_URL") != "https://example-test-jira.com"
    assert test_env.get("J2O_BATCH_SIZE") != "123"
