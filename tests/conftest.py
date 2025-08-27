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

from tests.utils.mock_factory import (
    create_mock_docker_client,
    create_mock_jira_client,
    create_mock_openproject_client,
    create_mock_rails_client,
    create_mock_ssh_client,
)

# Import real client classes when available; fall back to simple stubs to keep
# unit tests runnable without optional third-party deps (e.g., jira library)
try:  # pragma: no cover - exercised via import path, not logic
    from src.clients.docker_client import DockerClient
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient
    from src.clients.rails_console_client import RailsConsoleClient
    from src.clients.ssh_client import SSHClient
except ModuleNotFoundError:  # lightweight stubs for unit-only runs
    class DockerClient:  # type: ignore[no-redef]
        pass

    class JiraClient:  # type: ignore[no-redef]
        pass

    class OpenProjectClient:  # type: ignore[no-redef]
        pass

    class RailsConsoleClient:  # type: ignore[no-redef]
        pass

    class SSHClient:  # type: ignore[no-redef]
        pass


def pytest_configure(config: Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: mark a test as a unit test")
    config.addinivalue_line("markers", "functional: mark a test as a functional test")
    config.addinivalue_line(
        "markers",
        "integration: mark a test as an integration test",
    )
    config.addinivalue_line("markers", "end_to_end: mark a test as an end-to-end test")
    config.addinivalue_line("markers", "slow: mark a test as slow-running")
    config.addinivalue_line(
        "markers",
        "requires_docker: test requires Docker to be available",
    )
    config.addinivalue_line(
        "markers",
        "requires_ssh: test requires SSH connection to be available",
    )
    config.addinivalue_line(
        "markers",
        "requires_rails: test requires Rails console to be available",
    )


def _env_flag(name: str, default: bool = False) -> bool:
    """Read boolean environment flag (true/false)."""
    val = os.environ.get(name, "true" if default else "false").strip().lower()
    return val in {"1", "true", "yes", "on"}


def pytest_collection_modifyitems(config: Config, items: list[pytest.Item]) -> None:  # type: ignore[name-defined]
    """Apply default skipping for non-unit and external-resource tests.

    - Integration/functional/end_to_end tests are skipped by default unless
      J2O_RUN_INTEGRATION / J2O_RUN_FUNCTIONAL / J2O_RUN_E2E are set to true.
    - Tests marked requires_docker / requires_ssh / requires_rails are skipped
      unless J2O_ENABLE_DOCKER / J2O_ENABLE_SSH / J2O_ENABLE_RAILS are true.
    - Unmarked tests (no {unit,integration,functional,end_to_end}) are skipped by
      default to keep CI stable; mark them appropriately or set J2O_RUN_ALL_TESTS=true.
    """
    run_all = _env_flag("J2O_RUN_ALL_TESTS", False)
    # Global live/mocked mode
    live_services = _env_flag("J2O_LIVE_SERVICES", False) or config.getoption(
        "--live-services",
        default=False,
    )
    run_integration = _env_flag("J2O_RUN_INTEGRATION", False) or run_all
    run_functional = _env_flag("J2O_RUN_FUNCTIONAL", False) or run_all
    run_e2e = _env_flag("J2O_RUN_E2E", False) or run_all

    enable_docker = (
        _env_flag("J2O_ENABLE_DOCKER", False)
        or _env_flag("J2O_LIVE_DOCKER", False)
        or config.getoption("--live-docker", default=False)
        or live_services
        or run_all
    )
    enable_ssh = (
        _env_flag("J2O_ENABLE_SSH", False)
        or _env_flag("J2O_LIVE_SSH", False)
        or config.getoption("--live-ssh", default=False)
        or live_services
        or run_all
    )
    enable_rails = (
        _env_flag("J2O_ENABLE_RAILS", False)
        or _env_flag("J2O_LIVE_RAILS", False)
        or config.getoption("--live-rails", default=False)
        or live_services
        or run_all
    )

    skip_integration = pytest.mark.skip(
        reason="Integration tests disabled by default. Set J2O_RUN_INTEGRATION=true to enable.",
    )
    skip_functional = pytest.mark.skip(
        reason="Functional tests disabled by default. Set J2O_RUN_FUNCTIONAL=true to enable.",
    )
    skip_e2e = pytest.mark.skip(
        reason="End-to-end tests disabled by default. Set J2O_RUN_E2E=true to enable.",
    )
    skip_docker = pytest.mark.skip(
        reason="Docker-dependent test disabled. Set J2O_ENABLE_DOCKER=true to enable.",
    )
    skip_ssh = pytest.mark.skip(
        reason="SSH-dependent test disabled. Set J2O_ENABLE_SSH=true to enable.",
    )
    skip_rails = pytest.mark.skip(
        reason="Rails-dependent test disabled. Set J2O_ENABLE_RAILS=true to enable.",
    )
    skip_unmarked = pytest.mark.skip(
        reason=(
            "Unmarked test skipped by default. Mark with unit/integration/functional/end_to_end "
            "or set J2O_RUN_ALL_TESTS=true."
        ),
    )

    for item in items:
        kws = item.keywords

        # Category-level gating
        if "integration" in kws and not run_integration:
            item.add_marker(skip_integration)
            continue
        if "functional" in kws and not run_functional:
            item.add_marker(skip_functional)
            continue
        if "end_to_end" in kws and not run_e2e:
            item.add_marker(skip_e2e)
            continue

        # Resource gating
        if "requires_docker" in kws and not enable_docker:
            item.add_marker(skip_docker)
            continue
        if "requires_ssh" in kws and not enable_ssh:
            item.add_marker(skip_ssh)
            continue
        if "requires_rails" in kws and not enable_rails:
            item.add_marker(skip_rails)
            continue

        # Default: only run explicitly marked unit tests unless run_all is set
        if not run_all and not any(m in kws for m in ("unit", "integration", "functional", "end_to_end")):
            item.add_marker(skip_unmarked)


def pytest_addoption(parser) -> None:
    """Add custom command line options."""
    parser.addoption(
        "--live-ssh",
        action="store_true",
        default=False,
        help="Enable real SSH connections for integration tests (default: mock SSH for speed)",
    )
    parser.addoption(
        "--live-services",
        action="store_true",
        default=False,
        help="Run with live external services (Jira, OpenProject, Rails, Docker, SSH). Defaults to fully mocked.",
    )
    parser.addoption(
        "--live-jira",
        action="store_true",
        default=False,
        help="Use live Jira service instead of mocks.",
    )
    parser.addoption(
        "--live-openproject",
        action="store_true",
        default=False,
        help="Use live OpenProject service instead of mocks.",
    )
    parser.addoption(
        "--live-rails",
        action="store_true",
        default=False,
        help="Use live Rails console instead of mocks.",
    )
    parser.addoption(
        "--live-docker",
        action="store_true",
        default=False,
        help="Use live Docker instead of mocks.",
    )


# Automatically detect when running in test mode
def _is_test_environment() -> bool:
    """Detect if code is running in a pytest environment."""
    return "PYTEST_CURRENT_TEST" in os.environ


def pytest_ignore_collect(path, config):  # type: ignore[override]
    """Optionally ignore most tests to allow minimal, dependency-light runs.

    Set J2O_TEST_MINIMAL=1 to collect only a small allowlist of fast tests,
    avoiding imports that require optional external deps (jira, structlog).
    """
    if os.environ.get("J2O_TEST_MINIMAL", "0") != "1":
        return False
    try:
        p = Path(str(path))
        allow = {
            Path("tests/integration/test_rails_console_client.py").name,
            Path("tests/unit/clients/test_openproject_client_enhanced.py").name,
        }
        return p.name not in allow
    except Exception:
        return False


# Cache for environment file loading to avoid repeated disk I/O
_env_cache = {}


def _load_env_file_cached(file_path: str) -> dict:
    """Load environment file with caching to avoid repeated disk I/O."""
    if file_path not in _env_cache:
        if Path(file_path).exists():
            # Use a temporary dict to capture the loaded variables
            temp_env = {}
            load_dotenv(file_path, override=False)
            # Capture the loaded values, excluding sensitive keys
            sensitive_keys = {
                "*_PASSWORD",
                "*_SECRET",
                "*_KEY",
                "*_TOKEN",
                "SSH_PRIVATE_KEY",
            }
            _env_cache[file_path] = {
                k: v
                for k, v in os.environ.items()
                if k not in temp_env
                and not any(sensitive in k.upper() for sensitive in sensitive_keys)
            }
        else:
            _env_cache[file_path] = {}
    return _env_cache[file_path]


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment() -> Generator[None]:
    """Set up the test environment at the start of the test session.

    This fixture automatically loads the appropriate .env files for testing with caching:
    - Always loads .env (base config)
    - Always loads .env.test (default test config)
    - Optionally loads .env.local if exists (local dev overrides)
    - Optionally loads .env.test.local if exists (local test overrides)

    Files are loaded in order of increasing specificity, with later files
    overriding earlier ones. Uses caching to avoid repeated disk I/O.
    """
    # Store original environment to restore later
    original_env = os.environ.copy()

    # Set flag to indicate we're in test mode
    os.environ["J2O_TEST_MODE"] = "true"

    # Load configuration files in correct precedence order with caching
    # Base configuration
    base_env = _load_env_file_cached(".env")
    os.environ.update(base_env)

    # Local development overrides (if exists)
    local_env = _load_env_file_cached(".env.local")
    os.environ.update(local_env)

    # Default test configuration
    test_env = _load_env_file_cached(".env.test")
    os.environ.update(test_env)

    # Local test overrides (if exists)
    test_local_env = _load_env_file_cached(".env.test.local")
    os.environ.update(test_local_env)

    # Yield control back to tests
    yield

    # Clean up: restore original environment
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def test_env() -> Generator[dict[str, str]]:
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
def temp_dir() -> Generator[Path]:
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
    # Use factory to ensure commonly used methods like .get exist for tests
    return create_mock_jira_client()


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
    mock_client = MagicMock(spec=DockerClient)
    mock_client.container_name = "test-container"
    return cast("DockerClient", mock_client)


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


@pytest.fixture
def mock_clients(mock_jira_client: JiraClient, mock_op_client: OpenProjectClient):
    """Provide (jira_client, op_client) tuple for tests expecting combined fixture."""
    return mock_jira_client, mock_op_client


# Fixtures for integration tests directly using the clients
@pytest.fixture
def ssh_client(request) -> Generator[SSHClient]:
    """Create an SSH client instance.

    By default, returns a mock SSH client to eliminate network overhead in unit tests.
    Use --live-ssh flag to enable real SSH connections for integration tests.
    """
    # Check if --live-ssh flag is set
    live_ssh = request.config.getoption("--live-ssh", default=False)

    if not live_ssh:
        # Return mock SSH client for unit tests (fast execution)
        yield cast("SSHClient", MagicMock(spec=SSHClient))
        return

    # Only create real client if the required environment variables are set
    if not os.environ.get("J2O_OPENPROJECT_SERVER") or not os.environ.get(
        "J2O_OPENPROJECT_USER",
    ):
        pytest.skip("Required SSH environment variables not set for live SSH")

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


# Session autouse: default to fully mocked external clients, enable live via flags/env
@pytest.fixture(scope="session", autouse=True)
def configure_external_clients_mocking(pytestconfig: Config) -> Generator[None]:
    live_all = _env_flag("J2O_LIVE_SERVICES", False) or pytestconfig.getoption(
        "--live-services",
        default=False,
    )

    live_jira = live_all or _env_flag("J2O_LIVE_JIRA", False) or pytestconfig.getoption(
        "--live-jira",
        default=False,
    )
    live_op = live_all or _env_flag("J2O_LIVE_OPENPROJECT", False) or pytestconfig.getoption(
        "--live-openproject",
        default=False,
    )
    live_rails = live_all or _env_flag("J2O_LIVE_RAILS", False) or pytestconfig.getoption(
        "--live-rails",
        default=False,
    )
    live_docker = live_all or _env_flag("J2O_LIVE_DOCKER", False) or pytestconfig.getoption(
        "--live-docker",
        default=False,
    )
    live_ssh = live_all or _env_flag("J2O_LIVE_SSH", False) or pytestconfig.getoption(
        "--live-ssh",
        default=False,
    )

    mp = pytest.MonkeyPatch()
    try:
        if not live_jira:
            mp.setattr(
                "src.clients.jira_client.JiraClient",
                lambda *args, **kwargs: create_mock_jira_client(),
            )
        if not live_op:
            mp.setattr(
                "src.clients.openproject_client.OpenProjectClient",
                lambda *args, **kwargs: create_mock_openproject_client(),
            )
        if not live_rails:
            mp.setattr(
                "src.clients.rails_console_client.RailsConsoleClient",
                lambda *args, **kwargs: create_mock_rails_client(),
            )
        if not live_docker:
            mp.setattr(
                "src.clients.docker_client.DockerClient",
                lambda *args, **kwargs: create_mock_docker_client(),
            )
        if not live_ssh:
            mp.setattr(
                "src.clients.ssh_client.SSHClient",
                lambda *args, **kwargs: create_mock_ssh_client(),
            )

        yield
    finally:
        mp.undo()


# Monkeypatch helper functions for standardized mocking patterns
class MonkeypatchHelpers:
    """Helper class for standardized monkeypatch patterns."""

    @staticmethod
    def mock_method_return_value(
        monkeypatch: pytest.MonkeyPatch,
        obj: object,
        method_name: str,
        return_value,
    ) -> None:
        """Set a method's return value using monkeypatch.

        Args:
            monkeypatch: pytest monkeypatch fixture
            obj: Object containing the method to patch
            method_name: Name of the method to patch
            return_value: Value to return when method is called

        Example:
            helpers.mock_method_return_value(monkeypatch, mock_client, "get_users", [{"id": 1}])

        """
        mock_method = MagicMock(return_value=return_value)
        monkeypatch.setattr(obj, method_name, mock_method)

    @staticmethod
    def mock_method_side_effect(
        monkeypatch: pytest.MonkeyPatch,
        obj: object,
        method_name: str,
        side_effect,
    ) -> None:
        """Set a method's side effect using monkeypatch.

        Args:
            monkeypatch: pytest monkeypatch fixture
            obj: Object containing the method to patch
            method_name: Name of the method to patch
            side_effect: Side effect function or exception to apply

        Example:
            helpers.mock_method_side_effect(monkeypatch, mock_client, "create_user", Exception("Failed"))

        """
        mock_method = MagicMock(side_effect=side_effect)
        monkeypatch.setattr(obj, method_name, mock_method)

    @staticmethod
    def mock_class_return_value(
        monkeypatch: pytest.MonkeyPatch,
        module_path: str,
        class_name: str,
        return_value,
    ) -> None:
        """Set a class constructor's return value using monkeypatch.

        Args:
            monkeypatch: pytest monkeypatch fixture
            module_path: Full module path where class is imported
            class_name: Name of the class to patch
            return_value: Instance to return when class is instantiated

        Example:
            helpers.mock_class_return_value(monkeypatch, "src.clients.jira_client", "JiraClient", mock_jira_instance)

        """
        mock_class = MagicMock(return_value=return_value)
        monkeypatch.setattr(f"{module_path}.{class_name}", mock_class)

    @staticmethod
    def mock_path_exists(
        monkeypatch: pytest.MonkeyPatch,
        return_value: bool = True,
    ) -> None:
        """Mock os.path.exists using monkeypatch.

        Args:
            monkeypatch: pytest monkeypatch fixture
            return_value: Value to return for path existence checks

        """
        monkeypatch.setattr("os.path.exists", MagicMock(return_value=return_value))

    @staticmethod
    def mock_path_open(
        monkeypatch: pytest.MonkeyPatch,
        read_data: str = "",
    ) -> MagicMock:
        """Mock file opening using monkeypatch.

        Args:
            monkeypatch: pytest monkeypatch fixture
            read_data: Data to return when file is read

        Returns:
            MagicMock: The mock file object for additional configuration

        """
        from unittest.mock import mock_open

        mock_file = mock_open(read_data=read_data)
        monkeypatch.setattr("builtins.open", mock_file)
        return mock_file

    @staticmethod
    def mock_config_get(monkeypatch: pytest.MonkeyPatch, config_values: dict) -> None:
        """Mock configuration get method using monkeypatch.

        Args:
            monkeypatch: pytest monkeypatch fixture
            config_values: Dictionary mapping config keys to their values

        Example:
            helpers.mock_config_get(monkeypatch, {"dry_run": True, "force": False})

        """

        def config_side_effect(key: str, default=None):
            return config_values.get(key, default)

        # Mock various config patterns found in tests
        config_patterns = [
            "src.migrations.base_migration.config.migration_config.get",
            "src.migrations.user_migration.config.migration_config.get",
            "src.migrations.project_migration.config.migration_config.get",
            "src.migrations.issue_type_migration.config.migration_config.get",
            "src.migrations.workflow_migration.config.migration_config.get",
        ]

        for pattern in config_patterns:
            try:
                monkeypatch.setattr(pattern, MagicMock(side_effect=config_side_effect))
            except AttributeError:
                # Pattern doesn't exist in current test context, skip
                pass

    @staticmethod
    def mock_json_operations(
        monkeypatch: pytest.MonkeyPatch,
        load_data: dict | None = None,
        dump_data: dict | None = None,
    ) -> None:
        """Mock JSON load and dump operations using monkeypatch.

        Args:
            monkeypatch: pytest monkeypatch fixture
            load_data: Data to return when json.load is called
            dump_data: Expected data when json.dump is called (for verification)

        """
        if load_data is not None:
            monkeypatch.setattr("json.load", MagicMock(return_value=load_data))
        if dump_data is not None:
            monkeypatch.setattr("json.dump", MagicMock())


@pytest.fixture
def monkeypatch_helpers() -> MonkeypatchHelpers:
    """Provide access to monkeypatch helper methods.

    Returns:
        MonkeypatchHelpers: Helper class instance with standardized monkeypatch patterns

    """
    return MonkeypatchHelpers()


@pytest.fixture
def project_migration(mock_jira_client, mock_op_client):
    """Create a ProjectMigration instance for testing.

    Returns:
        ProjectMigration: A ProjectMigration instance with mocked clients

    """
    from src.migrations.project_migration import ProjectMigration

    return ProjectMigration(mock_jira_client, mock_op_client)


@pytest.fixture
def mock_jira_projects():
    """Provide mock Jira project data for testing.

    Returns:
        list: List of mock Jira project dictionaries

    """
    return [
        {"key": "TEST1", "name": "Test Project 1", "description": "First test project"},
        {
            "key": "TEST2",
            "name": "Test Project 2",
            "description": "Second test project",
        },
    ]
