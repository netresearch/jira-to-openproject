#!/usr/bin/env python3
"""Advanced Configuration Management System.

This module provides a comprehensive configuration management solution:
- Template-based configuration generation
- Environment-specific overrides
- Configuration validation and schema enforcement
- Version control and migration
- Import/export capabilities
- Configuration encryption and security
- Dynamic configuration updates
- Configuration backup and restore
"""

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import jsonschema
import yaml
from cryptography.fernet import Fernet
from jinja2 import Environment, FileSystemLoader


class EnvironmentType(Enum):
    """Supported environment types."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"


class ConfigVersion(Enum):
    """Configuration version types."""

    V1_0 = "1.0"
    V1_1 = "1.1"
    V2_0 = "2.0"


@dataclass
class ConfigTemplate:
    """Configuration template definition."""

    name: str
    description: str
    version: str
    environment: EnvironmentType
    template_path: Path
    schema_path: Path | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)


@dataclass
class ConfigOverride:
    """Configuration override definition."""

    environment: EnvironmentType
    section: str
    key: str
    value: Any
    description: str = ""
    priority: int = 100


@dataclass
class ConfigValidationResult:
    """Configuration validation result."""

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema_version: str | None = None


@dataclass
class ConfigBackup:
    """Configuration backup metadata."""

    backup_id: str
    timestamp: datetime
    description: str
    config_version: str
    environment: EnvironmentType
    file_path: Path
    checksum: str
    size_bytes: int


class ConfigurationManager:
    """Advanced configuration management system."""

    def __init__(
        self,
        config_dir: Path = Path("config"),
        templates_dir: Path = Path("config/templates"),
        backups_dir: Path = Path("config/backups"),
        encryption_key: str | None = None,
    ) -> None:
        """Initialize the configuration manager.

        Args:
            config_dir: Directory containing configuration files
            templates_dir: Directory containing configuration templates
            backups_dir: Directory for configuration backups
            encryption_key: Optional encryption key for sensitive configs

        """
        self.config_dir = config_dir
        self.templates_dir = templates_dir
        self.backups_dir = backups_dir
        self.encryption_key = encryption_key

        # Create directories if they don't exist
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

        # Initialize encryption if key provided
        self.fernet = None
        if encryption_key:
            self.fernet = Fernet(encryption_key.encode())

        # Load configuration schemas
        self.schemas = self._load_schemas()

        # Initialize Jinja2 environment for templates
        self.jinja_env = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _load_schemas(self) -> dict[str, dict[str, Any]]:
        """Load configuration schemas."""
        schemas = {}
        schema_dir = self.config_dir / "schemas"

        if schema_dir.exists():
            for schema_file in schema_dir.glob("*.json"):
                try:
                    with open(schema_file) as f:
                        schemas[schema_file.stem] = json.load(f)
                except Exception:
                    pass

        return schemas

    def create_config_from_template(
        self,
        template_name: str,
        environment: EnvironmentType,
        variables: dict[str, Any],
        output_path: Path | None = None,
        validate: bool = True,
    ) -> Path:
        """Create configuration from template.

        Args:
            template_name: Name of the template to use
            environment: Target environment
            variables: Template variables
            output_path: Output path for generated config
            validate: Whether to validate the generated config

        Returns:
            Path to the generated configuration file

        """
        # Load template
        template = self.jinja_env.get_template(f"{template_name}.yaml.j2")

        # Add environment and timestamp variables
        template_vars = {
            "environment": environment.value,
            "timestamp": datetime.now(UTC).isoformat(),
            "config_version": ConfigVersion.V2_0.value,
            **variables,
        }

        # Render template
        config_content = template.render(**template_vars)

        # Determine output path
        if output_path is None:
            output_path = self.config_dir / f"config_{environment.value}.yaml"

        # Write configuration
        with open(output_path, "w") as f:
            f.write(config_content)

        # Validate if requested
        if validate:
            validation_result = self.validate_config(output_path)
            if not validation_result.is_valid:
                msg = f"Generated configuration is invalid: {validation_result.errors}"
                raise ValueError(
                    msg,
                )

        return output_path

    def apply_overrides(
        self,
        config_path: Path,
        overrides: list[ConfigOverride],
        output_path: Path | None = None,
    ) -> Path:
        """Apply configuration overrides.

        Args:
            config_path: Path to base configuration
            overrides: List of overrides to apply
            output_path: Output path for modified config

        Returns:
            Path to the modified configuration file

        """
        # Load base configuration
        with open(config_path) as f:
            config = yaml.safe_load(f)

        # Sort overrides by priority (lower numbers = higher priority)
        sorted_overrides = sorted(overrides, key=lambda x: x.priority)

        # Apply overrides
        for override in sorted_overrides:
            if override.section not in config:
                config[override.section] = {}

            config[override.section][override.key] = override.value

        # Determine output path
        if output_path is None:
            output_path = config_path.with_suffix(".override.yaml")

        # Write modified configuration
        with open(output_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, indent=2)

        return output_path

    def validate_config(self, config_path: Path) -> ConfigValidationResult:
        """Validate configuration against schema.

        Args:
            config_path: Path to configuration file

        Returns:
            Validation result

        """
        # Load configuration
        with open(config_path) as f:
            config = yaml.safe_load(f)

        # Get schema version from config
        schema_version = config.get("schema_version", "1.0")
        schema_key = f"config_v{schema_version.replace('.', '_')}"

        # Check if schema exists
        if schema_key not in self.schemas:
            return ConfigValidationResult(
                is_valid=False,
                errors=[f"Schema version {schema_version} not found"],
                schema_version=schema_version,
            )

        schema = self.schemas[schema_key]

        # Validate against schema
        try:
            jsonschema.validate(instance=config, schema=schema)
            return ConfigValidationResult(is_valid=True, schema_version=schema_version)
        except jsonschema.ValidationError as e:
            return ConfigValidationResult(
                is_valid=False,
                errors=[str(e)],
                schema_version=schema_version,
            )

    def create_backup(self, config_path: Path, description: str = "") -> ConfigBackup:
        """Create configuration backup.

        Args:
            config_path: Path to configuration file
            description: Backup description

        Returns:
            Backup metadata

        """
        backup_id = str(uuid4())
        timestamp = datetime.now(UTC)

        # Load configuration to get metadata
        with open(config_path) as f:
            config = yaml.safe_load(f)

        config_version = config.get("schema_version", "1.0")
        environment = EnvironmentType(config.get("environment", "development"))

        # Create backup file
        backup_filename = (
            f"backup_{backup_id}_{timestamp.strftime('%Y%m%d_%H%M%S')}.yaml"
        )
        backup_path = self.backups_dir / backup_filename

        # Copy configuration file
        shutil.copy2(config_path, backup_path)

        # Calculate checksum
        import hashlib

        with open(backup_path, "rb") as f:
            checksum = hashlib.sha256(f.read()).hexdigest()

        # Create backup metadata
        backup = ConfigBackup(
            backup_id=backup_id,
            timestamp=timestamp,
            description=description,
            config_version=config_version,
            environment=environment,
            file_path=backup_path,
            checksum=checksum,
            size_bytes=backup_path.stat().st_size,
        )

        # Save backup metadata
        metadata_path = backup_path.with_suffix(".meta.json")
        with open(metadata_path, "w") as f:
            json.dump(backup.__dict__, f, default=str, indent=2)

        return backup

    def restore_backup(
        self,
        backup_id: str,
        target_path: Path | None = None,
    ) -> Path:
        """Restore configuration from backup.

        Args:
            backup_id: Backup ID to restore
            target_path: Target path for restored config

        Returns:
            Path to restored configuration file

        """
        # Find backup file
        backup_files = list(self.backups_dir.glob(f"backup_{backup_id}_*.yaml"))
        if not backup_files:
            msg = f"Backup {backup_id} not found"
            raise FileNotFoundError(msg)

        backup_path = backup_files[0]

        # Load backup metadata
        metadata_path = backup_path.with_suffix(".meta.json")
        if metadata_path.exists():
            with open(metadata_path) as f:
                json.load(f)

        # Determine target path
        if target_path is None:
            target_path = self.config_dir / f"config_restored_{backup_id}.yaml"

        # Restore configuration
        shutil.copy2(backup_path, target_path)

        return target_path

    def export_config(self, config_path: Path, format: str = "yaml") -> str:
        """Export configuration in specified format.

        Args:
            config_path: Path to configuration file
            format: Export format (yaml, json, env)

        Returns:
            Exported configuration as string

        """
        with open(config_path) as f:
            config = yaml.safe_load(f)

        if format.lower() == "json":
            return json.dumps(config, indent=2)
        if format.lower() == "env":
            return self._config_to_env(config)
        # yaml
        return yaml.dump(config, default_flow_style=False, indent=2)

    def import_config(
        self,
        config_content: str,
        format: str = "yaml",
    ) -> dict[str, Any]:
        """Import configuration from string.

        Args:
            config_content: Configuration content as string
            format: Import format (yaml, json, env)

        Returns:
            Parsed configuration dictionary

        """
        if format.lower() == "json":
            return json.loads(config_content)
        if format.lower() == "env":
            return self._env_to_config(config_content)
        # yaml
        return yaml.safe_load(config_content)

    def _config_to_env(self, config: dict[str, Any], prefix: str = "J2O_") -> str:
        """Convert configuration to environment variables format."""
        env_lines = []

        def flatten_dict(d: dict[str, Any], parent_key: str = "") -> None:
            for key, value in d.items():
                new_key = f"{parent_key}_{key}" if parent_key else key
                if isinstance(value, dict):
                    flatten_dict(value, new_key)
                else:
                    env_lines.append(f"{prefix}{new_key.upper()}={value}")

        flatten_dict(config)
        return "\n".join(env_lines)

    def _env_to_config(self, env_content: str) -> dict[str, Any]:
        """Convert environment variables to configuration format."""
        config = {}

        for line in env_content.strip().split("\n"):
            if line.startswith("J2O_") and "=" in line:
                key, value = line.split("=", 1)
                key = key[4:].lower()  # Remove J2O_ prefix

                # Convert key path to nested dict
                keys = key.split("_")
                current = config
                for k in keys[:-1]:
                    if k not in current:
                        current[k] = {}
                    current = current[k]
                current[keys[-1]] = value

        return config

    def encrypt_config(self, config_path: Path) -> Path:
        """Encrypt sensitive configuration file.

        Args:
            config_path: Path to configuration file

        Returns:
            Path to encrypted configuration file

        """
        if not self.fernet:
            msg = "Encryption key not provided"
            raise ValueError(msg)

        # Read configuration
        with open(config_path, "rb") as f:
            config_data = f.read()

        # Encrypt configuration
        encrypted_data = self.fernet.encrypt(config_data)

        # Write encrypted configuration
        encrypted_path = config_path.with_suffix(".encrypted")
        with open(encrypted_path, "wb") as f:
            f.write(encrypted_data)

        return encrypted_path

    def decrypt_config(self, encrypted_path: Path) -> Path:
        """Decrypt configuration file.

        Args:
            encrypted_path: Path to encrypted configuration file

        Returns:
            Path to decrypted configuration file

        """
        if not self.fernet:
            msg = "Encryption key not provided"
            raise ValueError(msg)

        # Read encrypted configuration
        with open(encrypted_path, "rb") as f:
            encrypted_data = f.read()

        # Decrypt configuration
        config_data = self.fernet.decrypt(encrypted_data)

        # Write decrypted configuration
        decrypted_path = encrypted_path.with_suffix(".decrypted")
        with open(decrypted_path, "wb") as f:
            f.write(config_data)

        return decrypted_path

    def list_templates(self) -> list[ConfigTemplate]:
        """List available configuration templates.

        Returns:
            List of available templates

        """
        templates = []

        for template_file in self.templates_dir.glob("*.yaml.j2"):
            # Try to extract metadata from template
            try:
                with open(template_file) as f:
                    content = f.read()

                # Extract metadata from comments
                metadata = self._extract_template_metadata(content)

                template = ConfigTemplate(
                    name=template_file.stem,
                    description=metadata.get("description", ""),
                    version=metadata.get("version", "1.0"),
                    environment=EnvironmentType(
                        metadata.get("environment", "development"),
                    ),
                    template_path=template_file,
                    variables=metadata.get("variables", {}),
                    dependencies=metadata.get("dependencies", []),
                )
                templates.append(template)
            except Exception:
                pass

        return templates

    def _extract_template_metadata(self, content: str) -> dict[str, Any]:
        """Extract metadata from template comments."""
        metadata = {}

        for line in content.split("\n"):
            if line.strip().startswith("# @"):
                # Parse metadata comment
                comment = line.strip()[3:]  # Remove '# @'
                if ":" in comment:
                    key, value = comment.split(":", 1)
                    metadata[key.strip()] = value.strip()

        return metadata

    def list_backups(self) -> list[ConfigBackup]:
        """List available configuration backups.

        Returns:
            List of available backups

        """
        backups = []

        for metadata_file in self.backups_dir.glob("*.meta.json"):
            try:
                with open(metadata_file) as f:
                    metadata = json.load(f)

                # Convert timestamp back to datetime
                metadata["timestamp"] = datetime.fromisoformat(metadata["timestamp"])
                metadata["environment"] = EnvironmentType(metadata["environment"])

                backup = ConfigBackup(**metadata)
                backups.append(backup)
            except Exception:
                pass

        return sorted(backups, key=lambda x: x.timestamp, reverse=True)


# Convenience functions
def create_config_manager(
    config_dir: Path = Path("config"),
    encryption_key: str | None = None,
) -> ConfigurationManager:
    """Create a configuration manager instance.

    Args:
        config_dir: Configuration directory
        encryption_key: Optional encryption key

    Returns:
        Configuration manager instance

    """
    return ConfigurationManager(config_dir=config_dir, encryption_key=encryption_key)


def generate_config_from_template(
    template_name: str,
    environment: EnvironmentType,
    variables: dict[str, Any],
    output_path: Path | None = None,
) -> Path:
    """Generate configuration from template.

    Args:
        template_name: Template name
        environment: Target environment
        variables: Template variables
        output_path: Output path

    Returns:
        Path to generated configuration

    """
    manager = create_config_manager()
    return manager.create_config_from_template(
        template_name=template_name,
        environment=environment,
        variables=variables,
        output_path=output_path,
    )


def validate_configuration(config_path: Path) -> ConfigValidationResult:
    """Validate configuration file.

    Args:
        config_path: Path to configuration file

    Returns:
        Validation result

    """
    manager = create_config_manager()
    return manager.validate_config(config_path)
