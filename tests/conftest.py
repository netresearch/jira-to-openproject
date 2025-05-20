"""Shared pytest fixtures and configuration for all tests."""

import os
import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from _pytest.config import Config
from dotenv import load_dotenv

from src.clients.docker_client import DockerClient
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.rails_console_client import RailsConsoleClient
from src.clients.ssh_client import SSHClient


def pytest_configure(config: Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: mark a test as a unit test")
    config.addinivalue_line("markers", "functional: mark a test as a functional test")
    config.addinivalue_line("markers", "integration: mark a test as an integration test")
    config.addinivalue_line("markers", "end_to_end: mark a test as an end-to-end test")
    config.addinivalue_line("markers", "slow: mark a test as slow-running")
    config.addinivalue_line("markers", "requires_docker: test requires Docker to be available")
    config.addinivalue_line("markers", "requires_ssh: test requires SSH connection to be available")
    config.addinivalue_line("markers", "requires_rails: test requires Rails console to be available")


# Automatically detect when running in test mode
def _is_test_environment() -> bool:
    """Detect if code is running in a pytest environment."""
    return "PYTEST_CURRENT_TEST" in os.environ


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment() -> Generator[None, None, None]:
    """Set up the test environment at the start of the test session.

    This fixture automatically loads the appropriate .env files for testing:
    - Always loads .env (base config)
    - Always loads .env.test (default test config)
    - Optionally loads .env.local if exists (local dev overrides)
    - Optionally loads .env.test.local if exists (local test overrides)

    Files are loaded in order of increasing specificity, with later files
    overriding earlier ones.
    """
    # Store original environment to restore later
    original_env = os.environ.copy()

    # Set flag to indicate we're in test mode
    os.environ["J2O_TEST_MODE"] = "true"

    # Load configuration files in correct precedence order
    load_dotenv(".env", override=True)  # Base configuration

    if Path(".env.local").exists():
        load_dotenv(".env.local", override=True)  # Local development overrides

    load_dotenv(".env.test", override=True)  # Default test configuration

    if Path(".env.test.local").exists():
        load_dotenv(".env.test.local", override=True)  # Local test overrides

    # Yield control back to tests
    yield

    # Clean up: restore original environment
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def test_env() -> Generator[dict[str, str], None, None]:
    """Fixture to control environment variables during a test.

    This fixture allows tests to temporarily override environment variables
    for their duration, restoring the original values afterward.

    Yields:
        dict[str, str]: A dictionary of current environment variables that
                       the test can modify to set temporary overrides.

    """
    # Store original environment to restore later
    original_env = os.environ.copy()

    # Create a dictionary connected to the actual environment
    # Changes to this dict will affect os.environ
    env_dict = os.environ

    try:
        # Let the test modify the environment as needed
        yield cast("dict[str, str]", env_dict)
    finally:
        # Clean up: restore original environment
        os.environ.clear()
        os.environ.update(original_env)


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test files.

    The directory is automatically cleaned up after the test completes.

    Returns:
        Path: Path to the temporary directory

    """
    dir_path = Path(tempfile.mkdtemp())
    try:
        yield dir_path
    finally:
        if dir_path.exists():
            shutil.rmtree(dir_path)


@pytest.fixture
def mock_jira_client() -> JiraClient:
    """Create a mock JiraClient for testing.

    Returns:
        JiraClient: A mocked JiraClient instance

    """
    return cast("JiraClient", MagicMock(spec=JiraClient))


@pytest.fixture
def mock_ssh_client() -> SSHClient:
    """Create a mock SSHClient for testing.

    Returns:
        SSHClient: A mocked SSHClient instance

    """
    return cast("SSHClient", MagicMock(spec=SSHClient))


@pytest.fixture
def mock_docker_client() -> DockerClient:
    """Create a mock DockerClient for testing.

    Returns:
        DockerClient: A mocked DockerClient instance

    """
    return cast("DockerClient", MagicMock(spec=DockerClient))


@pytest.fixture
def mock_rails_client() -> RailsConsoleClient:
    """Create a mock RailsConsoleClient for testing.

    Returns:
        RailsConsoleClient: A mocked RailsConsoleClient instance

    """
    return cast("RailsConsoleClient", MagicMock(spec=RailsConsoleClient))


@pytest.fixture
def mock_op_client() -> OpenProjectClient:
    """Create a mock OpenProjectClient for testing.

    Returns:
        OpenProjectClient: A mocked OpenProjectClient instance

    """
    return cast("OpenProjectClient", MagicMock(spec=OpenProjectClient))


# Fixtures for integration tests directly using the clients
@pytest.fixture
def ssh_client() -> Generator[SSHClient, None, None]:
    """Create an SSH client instance."""
    # Only create the client if the required environment variables are set
    if not os.environ.get("J2O_OPENPROJECT_SERVER") or not os.environ.get("J2O_OPENPROJECT_USER"):
        pytest.skip("Required SSH environment variables not set")

    try:
        client = SSHClient(
            host=os.environ["J2O_OPENPROJECT_SERVER"],
            user=os.environ["J2O_OPENPROJECT_USER"],
            connect_timeout=5,
            operation_timeout=15,
        )
        yield client
    except Exception as e:
        pytest.skip(f"Failed to create SSH client: {e}")
    finally:
        # Cleanup will happen in the client's __del__ method
        pass
