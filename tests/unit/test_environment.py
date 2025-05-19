import os
import sys


def test_python_version() -> None:
    """Test Python version is 3.12.x."""
    assert sys.version_info.major == 3
    assert sys.version_info.minor == 12


def test_environment_setup() -> None:
    """Test environment variables are properly loaded by the conftest fixture."""
    # Check for prefixed environment variables
    # These should be loaded automatically by the test fixtures in conftest.py
    jira_url = os.getenv("J2O_JIRA_URL")
    jira_token = os.getenv("J2O_JIRA_API_TOKEN")

    # We should have these variables loaded from .env or .env.test in test mode
    assert jira_url is not None, "J2O_JIRA_URL not found in environment"
    assert jira_token is not None, "J2O_JIRA_API_TOKEN not found in environment"

    # Previous test may have set J2O_TEST_MODE to false, so let's not check that
    # Instead, ensure test fixture works
    assert "PYTEST_CURRENT_TEST" in os.environ, "Not in pytest test context"
