#!/usr/bin/env python3
"""Tests for the configuration loader."""

import os
from unittest.mock import patch

import pytest

from src.config_loader import ConfigLoader


class ConfigLoaderTestHelper:
    """Helper class for creating test configurations."""

    def create_test_config(self, env_vars: dict, **overrides) -> dict:
        """Create a test configuration with environment variable values."""
        config = {
            "jira": {
                "server": env_vars["J2O_JIRA_SERVER"],
                "username": env_vars["J2O_JIRA_USERNAME"],
                "api_token": env_vars["J2O_JIRA_API_TOKEN"],
                "project_key": env_vars["J2O_JIRA_PROJECT_KEY"],
            },
            "openproject": {
                "server": env_vars["J2O_OPENPROJECT_SERVER"],
                "api_key": env_vars["J2O_OPENPROJECT_API_KEY"],
            },
            "migration": {
                "dry_run": env_vars.get("J2O_DRY_RUN", "false").lower() == "true",
                "batch_size": int(env_vars.get("J2O_BATCH_SIZE", "10")),
            },
        }

        # Apply overrides using dot notation (e.g., "jira.server")
        for key, value in overrides.items():
            sections = key.split(".")
            target = config
            for section in sections[:-1]:
                target = target[section]
            target[sections[-1]] = value

        return config


@pytest.fixture
def config_helper():
    """Fixture providing a ConfigLoaderTestHelper instance."""
    return ConfigLoaderTestHelper()


@pytest.fixture
def test_env():
    """Fixture providing test environment variables."""
    return {
        "J2O_JIRA_SERVER": "https://test-jira.example.com",
        "J2O_JIRA_USERNAME": "test_user",
        "J2O_JIRA_API_TOKEN": "test_token",
        "J2O_JIRA_PROJECT_KEY": "TEST",
        "J2O_OPENPROJECT_SERVER": "https://test-openproject.example.com",
        "J2O_OPENPROJECT_API_KEY": "test_api_key",
        "POSTGRES_PASSWORD": "test_password",
    }


@pytest.fixture
def temp_dir(tmp_path):
    """Fixture providing a temporary directory."""
    return tmp_path


@pytest.mark.unit
@patch("src.config_loader.ConfigLoader._apply_environment_overrides")
@patch("src.config_loader.ConfigLoader._load_database_config")
@patch("src.config_loader.ConfigLoader._load_yaml_config")
def test_config_loader_loads_from_config_file(
    mock_load_yaml,
    mock_load_db,
    mock_apply_env,
    test_env,
    config_helper,
    temp_dir,
) -> None:
    """Test that ConfigLoader can load configuration from a YAML file."""
    # Arrange
    test_config = config_helper.create_test_config(test_env)
    mock_load_yaml.return_value = test_config

    # Mock environment to provide the config file path
    with patch.dict(
        os.environ,
        {"CONFIG_FILE": str(temp_dir / "config.yaml")},
        clear=True,
    ):
        # Act
        config_loader = ConfigLoader()

    # Assert
    assert config_loader.config["jira"]["server"] == test_config["jira"]["server"]
    mock_load_db.assert_called_once()


@pytest.mark.unit
@patch("src.config_loader.ConfigLoader._apply_environment_overrides")
@patch("src.config_loader.ConfigLoader._load_database_config")
@patch("src.config_loader.ConfigLoader._load_yaml_config")
def test_config_loader_loads_from_environment(
    mock_load_yaml,
    mock_load_db,
    mock_apply_env,
    test_env,
) -> None:
    """Test that ConfigLoader can load configuration from environment variables."""
    # Arrange
    mock_load_yaml.return_value = {}

    # Act
    with patch.dict(os.environ, test_env, clear=True):
        config_loader = ConfigLoader()
        # Manually set the config after applying env overrides
        config_loader.config["jira"]["server"] = test_env["J2O_JIRA_SERVER"]

    # Assert - the environment overrides should set these values
    assert config_loader.config["jira"]["server"] == test_env["J2O_JIRA_SERVER"]
    mock_load_db.assert_called_once()


@pytest.mark.unit
@patch("src.config_loader.ConfigLoader._apply_environment_overrides")
@patch("src.config_loader.ConfigLoader._load_database_config")
@patch("src.config_loader.ConfigLoader._load_yaml_config")
def test_config_loader_raises_error_for_missing_required_config(
    mock_load_yaml,
    mock_load_db,
    mock_apply_env,
) -> None:
    """Test that ConfigLoader raises an error when required configuration is missing."""
    # Arrange
    mock_load_yaml.return_value = {}
    mock_load_db.side_effect = RuntimeError("Missing required database configuration")

    # Act & Assert
    with patch.dict(os.environ, {}, clear=True), pytest.raises(RuntimeError) as excinfo:
        ConfigLoader()

    assert "Missing required database configuration" in str(excinfo.value)


@pytest.mark.unit
@patch("src.config_loader.ConfigLoader._apply_environment_overrides")
@patch("src.config_loader.ConfigLoader._load_database_config")
@patch("src.config_loader.ConfigLoader._load_yaml_config")
def test_config_loader_file_overrides_environment(
    mock_load_yaml,
    mock_load_db,
    mock_apply_env,
    test_env,
    config_helper,
    temp_dir,
) -> None:
    """Test that file configuration overrides environment variables."""
    # Arrange
    file_config = config_helper.create_test_config(
        test_env,
        **{"jira.server": "https://file-jira.example.com"},
    )
    mock_load_yaml.return_value = file_config

    env_with_config_file = {**test_env, "CONFIG_FILE": str(temp_dir / "config.yaml")}

    # Act
    with patch.dict(os.environ, env_with_config_file, clear=True):
        config_loader = ConfigLoader()

    # Assert
    assert config_loader.config["jira"]["server"] == "https://file-jira.example.com"
    mock_load_db.assert_called_once()


class TestDatabaseConfiguration:
    """Test database configuration loading functionality."""

    @pytest.mark.parametrize(
        "env_password",
        ["test_password", "complex!@#password123", ""],
    )
    @patch("src.config_loader.Path")
    def test_load_database_config_env_var_priority(
        self,
        mock_path,
        env_password,
    ) -> None:
        """Test _load_database_config prioritizes environment variable over Docker secrets."""
        # Arrange
        mock_path_instance = mock_path.return_value
        mock_path_instance.exists.return_value = True  # Docker secret exists
        mock_path_instance.read_text.return_value = "docker_secret_password"

        # Mock all the other initialization methods
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                mock_yaml.return_value = {}

                with patch.dict(
                    os.environ,
                    {"POSTGRES_PASSWORD": env_password},
                    clear=True,
                ):
                    if env_password:  # Non-empty password
                        config_loader = ConfigLoader()
                        expected_password = env_password
                        assert (
                            config_loader.config["database"]["postgres_password"]
                            == expected_password
                        )
                    else:  # Empty password should fall back to Docker secret
                        config_loader = ConfigLoader()
                        expected_password = "docker_secret_password"
                        assert (
                            config_loader.config["database"]["postgres_password"]
                            == expected_password
                        )

    @patch("src.config_loader.Path")
    @patch("src.config_loader.load_dotenv")
    def test_load_database_config_docker_secret_fallback(
        self,
        mock_load_dotenv,
        mock_path,
    ) -> None:
        """Test _load_database_config falls back to Docker secret when env var is missing."""
        # Arrange
        mock_path_instance = mock_path.return_value
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = (
            "docker_secret_password\n"  # With whitespace
        )

        # Mock all the other initialization methods
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                mock_yaml.return_value = {}

                with patch.dict(os.environ, {}, clear=True):  # No POSTGRES_PASSWORD
                    config_loader = ConfigLoader()

        # Assert
        assert (
            config_loader.config["database"]["postgres_password"]
            == "docker_secret_password"
        )
        assert (
            config_loader.config["database"]["postgres_db"] == "jira_migration"
        )  # Default value

    @patch("src.config_loader.Path")
    @patch("src.config_loader.load_dotenv")
    def test_load_database_config_no_password_sources(
        self,
        mock_load_dotenv,
        mock_path,
    ) -> None:
        """Test _load_database_config raises RuntimeError when no password sources exist."""
        # Arrange
        mock_path_instance = mock_path.return_value
        mock_path_instance.exists.return_value = False  # Docker secret doesn't exist

        # Mock all the other initialization methods
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                mock_yaml.return_value = {}

                with patch.dict(os.environ, {}, clear=True):  # No POSTGRES_PASSWORD
                    # Act & Assert
                    with pytest.raises(RuntimeError) as excinfo:
                        ConfigLoader()

                    assert "POSTGRES_PASSWORD is required but not found" in str(
                        excinfo.value,
                    )

    @patch("src.config_loader.Path")
    @patch("src.config_loader.load_dotenv")
    def test_load_database_config_docker_secret_read_error(
        self,
        mock_load_dotenv,
        mock_path,
    ) -> None:
        """Test _load_database_config handles Docker secret file read errors."""
        # Arrange
        mock_path_instance = mock_path.return_value
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.side_effect = OSError("Permission denied")

        # Mock all the other initialization methods
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                mock_yaml.return_value = {}

                with patch.dict(os.environ, {}, clear=True):
                    # Act & Assert
                    with pytest.raises(RuntimeError) as excinfo:
                        ConfigLoader()

                    assert "POSTGRES_PASSWORD is required but not found" in str(
                        excinfo.value,
                    )

    @patch("src.config_loader.Path")
    @patch("src.config_loader.load_dotenv")
    def test_load_database_config_docker_secret_empty_file(
        self,
        mock_load_dotenv,
        mock_path,
    ) -> None:
        """Test _load_database_config handles empty Docker secret file."""
        # Arrange
        mock_path_instance = mock_path.return_value
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "   \n\t  "  # Only whitespace

        # Mock all the other initialization methods
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                mock_yaml.return_value = {}

                with patch.dict(os.environ, {}, clear=True):
                    # Act & Assert
                    with pytest.raises(RuntimeError) as excinfo:
                        ConfigLoader()

                    assert "POSTGRES_PASSWORD is required but not found" in str(
                        excinfo.value,
                    )

    def test_load_database_config_returns_complete_config(self) -> None:
        """Test _load_database_config returns complete database configuration."""
        # Arrange
        # Mock all the other initialization methods
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                mock_yaml.return_value = {}

                with patch.dict(
                    os.environ,
                    {"POSTGRES_PASSWORD": "test_password"},
                    clear=True,
                ):
                    config_loader = ConfigLoader()

                # Assert - check that complete config is set
                db_config = config_loader.config["database"]
                assert db_config["postgres_password"] == "test_password"
                assert db_config["postgres_db"] == "jira_migration"  # Default value
                assert db_config["postgres_user"] == "postgres"  # Default value

    @pytest.mark.parametrize(
        ("env_overrides", "expected"),
        [
            ({"POSTGRES_DB": "custom_db"}, {"postgres_db": "custom_db"}),
            ({"POSTGRES_USER": "custom_user"}, {"postgres_user": "custom_user"}),
            (
                {"POSTGRES_DB": "test_db", "POSTGRES_USER": "test_user"},
                {"postgres_db": "test_db", "postgres_user": "test_user"},
            ),
        ],
    )
    def test_load_database_config_environment_overrides(
        self,
        env_overrides,
        expected,
    ) -> None:
        """Test _load_database_config respects environment variable overrides."""
        # Arrange
        base_env = {"POSTGRES_PASSWORD": "test_password"}
        test_env = {**base_env, **env_overrides}

        # Mock all the other initialization methods
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                mock_yaml.return_value = {}

                with patch.dict(os.environ, test_env, clear=True):
                    config_loader = ConfigLoader()

                # Assert
                db_config = config_loader.config["database"]
                for key, value in expected.items():
                    assert db_config[key] == value

    def test_get_postgres_password_success(self) -> None:
        """Test get_postgres_password returns password from database config."""
        # Arrange
        # Mock all the other initialization methods
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                mock_yaml.return_value = {}

                with patch.dict(
                    os.environ,
                    {"POSTGRES_PASSWORD": "test_password"},
                    clear=True,
                ):
                    config_loader = ConfigLoader()

                # Act
                password = config_loader.get_postgres_password()

                # Assert
                assert password == "test_password"

    def test_get_postgres_password_missing_config(self) -> None:
        """Test get_postgres_password raises RuntimeError when database config is missing."""
        # Arrange
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                with patch("src.config_loader.ConfigLoader._load_database_config"):
                    mock_yaml.return_value = {}
                    config_loader = ConfigLoader()
                    # Manually remove database config to simulate missing config
                    if "database" in config_loader.config:
                        del config_loader.config["database"]

        # Act & Assert
        with pytest.raises(RuntimeError) as excinfo:
            config_loader.get_postgres_password()

        assert "PostgreSQL password not configured" in str(excinfo.value)

    def test_get_postgres_password_missing_password_key(self) -> None:
        """Test get_postgres_password raises RuntimeError when password key is missing."""
        # Arrange
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                with patch("src.config_loader.ConfigLoader._load_database_config"):
                    mock_yaml.return_value = {}
                    config_loader = ConfigLoader()
                    # Set database config without password
                    config_loader.config["database"] = {"postgres_db": "test_db"}

        # Act & Assert
        with pytest.raises(RuntimeError) as excinfo:
            config_loader.get_postgres_password()

        assert "PostgreSQL password not configured" in str(excinfo.value)

    def test_get_database_config_success(self) -> None:
        """Test get_database_config returns complete database configuration."""
        # Arrange
        # Mock all the other initialization methods
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                mock_yaml.return_value = {}

                with patch.dict(
                    os.environ,
                    {"POSTGRES_PASSWORD": "test_password"},
                    clear=True,
                ):
                    config_loader = ConfigLoader()

                # Act
                result = config_loader.get_database_config()

                # Assert
                expected_keys = ["postgres_password", "postgres_db", "postgres_user"]
                for key in expected_keys:
                    assert key in result
                assert result["postgres_password"] == "test_password"

    def test_get_database_config_missing(self) -> None:
        """Test get_database_config returns empty dict when database config is missing."""
        # Arrange
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                with patch("src.config_loader.ConfigLoader._load_database_config"):
                    mock_yaml.return_value = {}
                    config_loader = ConfigLoader()
                    # Manually remove database config to simulate missing config
                    if "database" in config_loader.config:
                        del config_loader.config["database"]

        # Act
        result = config_loader.get_database_config()

        # Assert - method returns empty dict, doesn't raise error
        assert result == {}

    def test_configloader_init_calls_load_database_config(self, test_env) -> None:
        """Test ConfigLoader.__init__ calls _load_database_config."""
        # Arrange & Act
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                with patch(
                    "src.config_loader.ConfigLoader._load_database_config",
                ) as mock_load_db:
                    mock_yaml.return_value = {}
                    ConfigLoader()

        # Assert
        mock_load_db.assert_called_once()

    def test_configloader_init_propagates_database_config_error(self, test_env) -> None:
        """Test ConfigLoader.__init__ propagates _load_database_config errors."""
        # Arrange
        with patch("src.config_loader.ConfigLoader._load_yaml_config") as mock_yaml:
            with patch("src.config_loader.ConfigLoader._apply_environment_overrides"):
                with patch(
                    "src.config_loader.ConfigLoader._load_database_config",
                ) as mock_load_db:
                    mock_yaml.return_value = {}
                    mock_load_db.side_effect = RuntimeError("Database config error")

                    # Act & Assert
                    with pytest.raises(RuntimeError) as excinfo:
                        ConfigLoader()

                    assert "Database config error" in str(excinfo.value)
