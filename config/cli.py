"""CLI tools for configuration management and validation.

This module provides command-line tools for managing and validating
the Jira to OpenProject migration configuration.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import requests
import yaml

from .loader import load_settings
from .schemas.settings import Settings

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def validate_configuration(settings: Settings) -> list[str]:
    """Validate configuration and return list of errors.

    Args:
        settings: Settings object to validate

    Returns:
        List of validation error messages (empty if valid)

    """
    errors = []

    try:
        # Validate that required directories exist or can be created
        for path_name, path in [
            ("data_dir", settings.data_dir),
            ("backup_dir", settings.backup_dir),
            ("results_dir", settings.results_dir),
        ]:
            try:
                path.mkdir(parents=True, exist_ok=True)
                logger.debug(f"✓ {path_name} directory: {path}")
            except Exception as e:
                error_msg = f"Cannot create {path_name} directory {path}: {e}"
                errors.append(error_msg)
                logger.error(error_msg)

        # Validate that mapping file directory exists
        try:
            settings.mapping_file.parent.mkdir(parents=True, exist_ok=True)
            logger.debug(f"✓ mapping file directory: {settings.mapping_file.parent}")
        except Exception as e:
            error_msg = f"Cannot create mapping file directory: {e}"
            errors.append(error_msg)
            logger.error(error_msg)

        # Validate that attachment path directory exists
        try:
            settings.attachment_path.mkdir(parents=True, exist_ok=True)
            logger.debug(f"✓ attachment directory: {settings.attachment_path}")
        except Exception as e:
            error_msg = f"Cannot create attachment directory: {e}"
            errors.append(error_msg)
            logger.error(error_msg)

    except Exception as e:
        error_msg = f"Validation failed: {e}"
        errors.append(error_msg)
        logger.error(error_msg)

    return errors


def test_jira_connection(settings: Settings) -> bool:
    """Test connection to Jira.

    Args:
        settings: Settings object with Jira configuration

    Returns:
        True if connection successful, False otherwise

    """
    try:
        base = settings.jira_url.rstrip("/")
        # 1) Server availability check (no auth)
        si = requests.get(
            f"{base}/rest/api/2/serverInfo",
            timeout=10,
            verify=settings.ssl_verify,
        )
        server_ok = si.status_code == 200
        if server_ok:
            info = si.json()
            logger.info(
                f"✓ Jira server reachable: {info.get('baseUrl','')} ({info.get('version','')})",
            )
        else:
            logger.error(f"✗ Jira server not reachable - Status: {si.status_code}")

        # 2) Auth check using PAT (Bearer)
        headers = {"Authorization": f"Bearer {settings.jira_api_token}"}
        me = requests.get(
            f"{base}/rest/api/2/myself",
            headers=headers,
            timeout=10,
            verify=settings.ssl_verify,
        )
        auth_ok = me.status_code == 200
        if auth_ok:
            user_info = me.json()
            logger.info(
                f"✓ Jira auth successful - User: {user_info.get('displayName', 'Unknown')}",
            )
        else:
            hdr = {k: v for k, v in me.headers.items() if k.lower() in ("www-authenticate", "x-ausername")}
            logger.error(
                f"✗ Jira auth failed - Status: {me.status_code} headers={hdr}",
            )

        return server_ok and auth_ok

    except requests.exceptions.RequestException as e:
        logger.error(f"✗ Jira connection failed - {e}")
        return False


def test_openproject_connection(settings: Settings) -> bool:
    """Test connection to OpenProject.

    Args:
        settings: Settings object with OpenProject configuration

    Returns:
        True if connection successful, False otherwise

    """
    try:
        # Test basic connectivity
        headers = {"Authorization": f"Bearer {settings.openproject_api_token}"}
        response = requests.get(
            f"{settings.openproject_url}/api/v3/users/me",
            headers=headers,
            timeout=10,
            verify=settings.ssl_verify,
        )

        if response.status_code == 200:
            user_info = response.json()
            logger.info(
                f"✓ OpenProject connection successful - User: {user_info.get('name', 'Unknown')}",
            )
            return True
        logger.error(
            f"✗ OpenProject connection failed - Status: {response.status_code}",
        )
        return False

    except requests.exceptions.RequestException as e:
        logger.error(f"✗ OpenProject connection failed - {e}")
        return False


def print_settings_summary(settings: Settings, show_secrets: bool = False) -> None:
    """Print a summary of the current settings.

    Args:
        settings: Settings object to display
        show_secrets: Whether to show secret values

    """
    print("=== Configuration Summary ===")
    print(f"Jira URL: {settings.jira_url}")
    print(f"Jira Username: {settings.jira_username}")
    print(f"Jira API Token: {'***' if not show_secrets else settings.jira_api_token}")
    print(f"OpenProject URL: {settings.openproject_url}")
    print(
        f"OpenProject API Token: {'***' if not show_secrets else settings.openproject_api_token}",
    )
    print(f"Migration Batch Size: {settings.batch_size}")
    print(f"SSL Verify: {settings.ssl_verify}")
    print(f"Log Level: {settings.log_level}")
    print(f"Test Mode: {settings.test_mode}")
    print(f"Test Mock Mode: {settings.test_mock_mode}")
    print(f"Use Mock APIs: {settings.use_mock_apis}")
    print(f"Data Directory: {settings.data_dir}")
    print(f"Backup Directory: {settings.backup_dir}")
    print(f"Results Directory: {settings.results_dir}")
    print(f"Component Order: {settings.component_order}")


def export_config(
    settings: Settings, output_file: Path | None = None, format: str = "json",
) -> None:
    """Export configuration to file.

    Args:
        settings: Settings object to export
        output_file: Output file path (if None, prints to stdout)
        format: Output format (json or yaml)

    """
    config_data = {
        "jira": settings.get_jira_config(),
        "openproject": settings.get_openproject_config(),
        "migration": settings.get_migration_config(),
        "database": settings.get_database_config(),
        "test_mode": settings.is_test_mode(),
    }

    # Remove secrets from export
    if "jira" in config_data:
        config_data["jira"]["api_token"] = "***"
    if "openproject" in config_data:
        config_data["openproject"]["api_token"] = "***"
        config_data["openproject"]["api_key"] = "***"
    if "database" in config_data:
        config_data["database"]["postgres_password"] = "***"

    if format.lower() == "json":
        output = json.dumps(config_data, indent=2)
    elif format.lower() == "yaml":
        output = yaml.dump(config_data, default_flow_style=False, indent=2)
    else:
        raise ValueError(f"Unsupported format: {format}")

    if output_file:
        output_file.write_text(output)
        logger.info(f"Configuration exported to {output_file}")
    else:
        print(output)


def create_envrc_template(
    settings: Settings, output_file: Path = Path(".envrc"),
) -> None:
    """Create a .envrc template for direnv.

    Args:
        settings: Settings object to use as template
        output_file: Output file path

    """
    template = f"""# Jira to OpenProject Migration - direnv configuration
# This file is automatically loaded when entering the project directory
# Run 'direnv allow' to enable this configuration

# Environment
export J2O_ENVIRONMENT="development"

# Jira Configuration
export J2O_JIRA_URL="{settings.jira_url}"
export J2O_JIRA_USERNAME="{settings.jira_username}"
export J2O_JIRA_API_TOKEN="your_jira_api_token_here"

# OpenProject Configuration
export J2O_OPENPROJECT_URL="{settings.openproject_url}"
export J2O_OPENPROJECT_API_TOKEN="your_openproject_api_token_here"
export J2O_OPENPROJECT_API_KEY="your_openproject_api_key_here"

# SSH/Docker Configuration
export J2O_OPENPROJECT_SERVER="{settings.openproject_server}"
export J2O_OPENPROJECT_USER="{settings.openproject_user}"
export J2O_OPENPROJECT_CONTAINER="{settings.openproject_container}"
export J2O_OPENPROJECT_TMUX_SESSION_NAME="{settings.openproject_tmux_session_name}"

# Migration Settings
export J2O_BATCH_SIZE="{settings.batch_size}"
export J2O_SSL_VERIFY="{str(settings.ssl_verify).lower()}"
export J2O_LOG_LEVEL="{settings.log_level}"

# Testing Configuration
export J2O_TEST_MODE="{str(settings.test_mode).lower()}"
export J2O_TEST_MOCK_MODE="{str(settings.test_mock_mode).lower()}"
export J2O_USE_MOCK_APIS="{str(settings.use_mock_apis).lower()}"

# Database Configuration
export POSTGRES_PASSWORD="your_postgres_password_here"
export POSTGRES_DB="{settings.postgres_db}"
export POSTGRES_USER="{settings.postgres_user}"
"""

    output_file.write_text(template)
    logger.info(f"direnv template created at {output_file}")
    logger.info("Run 'direnv allow' to enable this configuration")


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Jira to OpenProject Migration Configuration CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m config.cli validate                    # Validate configuration
  python -m config.cli test-connections            # Test service connections
  python -m config.cli show                        # Show configuration summary
  python -m config.cli show --secrets              # Show configuration with secrets
  python -m config.cli export --format json        # Export configuration as JSON
  python -m config.cli export --format yaml        # Export configuration as YAML
  python -m config.cli create-envrc                # Create .envrc template for direnv
        """,
    )

    parser.add_argument(
        "command",
        choices=["validate", "test-connections", "show", "export", "create-envrc"],
        help="Command to execute",
    )

    parser.add_argument(
        "--config-file",
        type=Path,
        default=Path("config/config.yaml"),
        help="Configuration file path (default: config/config.yaml)",
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )

    parser.add_argument(
        "--secrets",
        action="store_true",
        help="Show secrets in output (use with caution)",
    )

    parser.add_argument(
        "--format",
        choices=["json", "yaml"],
        default="json",
        help="Export format (default: json)",
    )

    parser.add_argument(
        "--output", type=Path, help="Output file path (default: stdout)",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)

    try:
        # Load settings
        settings = load_settings(args.config_file)
        logger.info("Configuration loaded successfully")

        if args.command == "validate":
            errors = validate_configuration(settings)
            if errors:
                logger.error("Configuration validation failed:")
                for error in errors:
                    logger.error(f"  - {error}")
                sys.exit(1)
            else:
                logger.info("✓ Configuration validation passed")

        elif args.command == "test-connections":
            logger.info("Testing service connections...")

            jira_ok = test_jira_connection(settings)
            openproject_ok = test_openproject_connection(settings)

            if jira_ok and openproject_ok:
                logger.info("✓ All service connections successful")
            else:
                logger.error("✗ Some service connections failed")
                sys.exit(1)

        elif args.command == "show":
            print_settings_summary(settings, show_secrets=args.secrets)

        elif args.command == "export":
            export_config(settings, args.output, args.format)

        elif args.command == "create-envrc":
            create_envrc_template(settings, args.output or Path(".envrc"))

    except Exception as e:
        logger.error(f"Command failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
