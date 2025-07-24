#!/usr/bin/env python3
"""Tests for the Advanced Configuration Management System."""
import asyncio
import json
import pytest
import tempfile
import yaml
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.utils.advanced_config_manager import (
    ConfigurationManager, EnvironmentType, ConfigVersion, ConfigTemplate,
    ConfigOverride, ConfigValidationResult, ConfigBackup,
    generate_config_from_template, apply_config_overrides, validate_config,
    backup_config, restore_config, encrypt_config, decrypt_config
)


class TestEnvironmentType:
    def test_environment_type_values(self):
        assert EnvironmentType.DEVELOPMENT.value == "development"
        assert EnvironmentType.STAGING.value == "staging"
        assert EnvironmentType.PRODUCTION.value == "production"
        assert EnvironmentType.TESTING.value == "testing"


class TestConfigVersion:
    def test_config_version_creation(self):
        version = ConfigVersion(major=2, minor=0, patch=1)
        assert version.major == 2
        assert version.minor == 0
        assert version.patch == 1
        assert str(version) == "2.0.1"

    def test_config_version_from_string(self):
        version = ConfigVersion.from_string("1.2.3")
        assert version.major == 1
        assert version.minor == 2
        assert version.patch == 3

    def test_config_version_comparison(self):
        v1 = ConfigVersion(1, 0, 0)
        v2 = ConfigVersion(2, 0, 0)
        assert v1 < v2
        assert v2 > v1


class TestConfigTemplate:
    def test_config_template_creation(self):
        template = ConfigTemplate(
            name="test_template",
            description="Test template",
            template_path=Path("/tmp/test.j2"),
            variables=["var1", "var2"],
            dependencies=["base_config"]
        )
        assert template.name == "test_template"
        assert template.description == "Test template"
        assert len(template.variables) == 2


class TestConfigOverride:
    def test_config_override_creation(self):
        override = ConfigOverride(
            path="jira.url",
            value="https://new-jira.local/",
            environment=EnvironmentType.DEVELOPMENT,
            description="Override Jira URL for development"
        )
        assert override.path == "jira.url"
        assert override.value == "https://new-jira.local/"


class TestConfigValidationResult:
    def test_config_validation_result_creation(self):
        result = ConfigValidationResult(
            is_valid=True,
            errors=[],
            warnings=["Warning 1"],
            schema_version="2.0"
        )
        assert result.is_valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 1


class TestConfigBackup:
    def test_config_backup_creation(self):
        backup = ConfigBackup(
            backup_id="backup_123",
            original_path=Path("/tmp/config.yaml"),
            backup_path=Path("/tmp/backup/config.yaml"),
            timestamp=datetime.now(UTC),
            description="Test backup"
        )
        assert backup.backup_id == "backup_123"
        assert backup.original_path == Path("/tmp/config.yaml")


class TestConfigurationManager:
    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield Path(tmp_dir)

    @pytest.fixture
    def config_manager(self, temp_dir):
        return ConfigurationManager(
            config_dir=temp_dir,
            templates_dir=temp_dir / "templates",
            schemas_dir=temp_dir / "schemas",
            backups_dir=temp_dir / "backups"
        )

    def test_config_manager_initialization(self, config_manager, temp_dir):
        assert config_manager.config_dir == temp_dir
        assert config_manager.templates_dir == temp_dir / "templates"
        assert config_manager.schemas_dir == temp_dir / "schemas"
        assert config_manager.backups_dir == temp_dir / "backups"

    def test_create_directories(self, config_manager):
        config_manager.create_directories()
        assert config_manager.config_dir.exists()
        assert config_manager.templates_dir.exists()
        assert config_manager.schemas_dir.exists()
        assert config_manager.backups_dir.exists()

    def test_generate_config_from_template(self, config_manager, temp_dir):
        # Create a simple template
        template_content = """
        jira:
          url: "{{ jira_url }}"
          username: "{{ jira_username }}"
        """
        template_path = temp_dir / "test_template.yaml.j2"
        template_path.write_text(template_content)

        template = ConfigTemplate(
            name="test_template",
            description="Test template",
            template_path=template_path,
            variables=["jira_url", "jira_username"],
            dependencies=[]
        )

        variables = {
            "jira_url": "https://test-jira.local/",
            "jira_username": "testuser"
        }

        result = config_manager.generate_config_from_template(template, variables)
        assert result.is_valid is True
        assert "jira" in result.config
        assert result.config["jira"]["url"] == "https://test-jira.local/"

    def test_apply_config_overrides(self, config_manager):
        base_config = {
            "jira": {
                "url": "https://jira.local/",
                "username": "user"
            }
        }

        overrides = [
            ConfigOverride(
                path="jira.url",
                value="https://new-jira.local/",
                environment=EnvironmentType.DEVELOPMENT
            )
        ]

        result = config_manager.apply_config_overrides(base_config, overrides)
        assert result.config["jira"]["url"] == "https://new-jira.local/"

    def test_validate_config(self, config_manager, temp_dir):
        # Create a simple schema
        schema = {
            "type": "object",
            "properties": {
                "jira": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "username": {"type": "string"}
                    },
                    "required": ["url", "username"]
                }
            },
            "required": ["jira"]
        }

        schema_path = temp_dir / "test_schema.json"
        schema_path.write_text(json.dumps(schema))

        config = {
            "jira": {
                "url": "https://jira.local/",
                "username": "user"
            }
        }

        result = config_manager.validate_config(config, schema_path)
        assert result.is_valid is True

    def test_backup_config(self, config_manager, temp_dir):
        config_path = temp_dir / "config.yaml"
        config_content = {"test": "data"}
        config_path.write_text(yaml.dump(config_content))

        backup = config_manager.backup_config(
            config_path,
            description="Test backup"
        )

        assert backup.backup_id is not None
        assert backup.original_path == config_path
        assert backup.backup_path.exists()
        assert backup.description == "Test backup"

    def test_restore_config(self, config_manager, temp_dir):
        # Create original config
        original_config = {"original": "data"}
        config_path = temp_dir / "config.yaml"
        config_path.write_text(yaml.dump(original_config))

        # Create backup
        backup = config_manager.backup_config(config_path, "Test backup")

        # Modify config
        modified_config = {"modified": "data"}
        config_path.write_text(yaml.dump(modified_config))

        # Restore from backup
        restored = config_manager.restore_config(backup.backup_id)
        assert restored is True

        # Verify restoration
        with open(config_path) as f:
            restored_config = yaml.safe_load(f)
        assert restored_config == original_config

    def test_encrypt_decrypt_config(self, config_manager):
        config_data = {"sensitive": "data"}
        encryption_key = config_manager.generate_encryption_key()

        encrypted = config_manager.encrypt_config(config_data, encryption_key)
        assert encrypted != config_data

        decrypted = config_manager.decrypt_config(encrypted, encryption_key)
        assert decrypted == config_data

    def test_list_templates(self, config_manager, temp_dir):
        # Create some templates
        template1 = temp_dir / "template1.yaml.j2"
        template1.write_text("template1")
        template2 = temp_dir / "template2.yaml.j2"
        template2.write_text("template2")

        templates = config_manager.list_templates()
        assert len(templates) == 2
        template_names = [t.name for t in templates]
        assert "template1" in template_names
        assert "template2" in template_names

    def test_list_backups(self, config_manager, temp_dir):
        # Create some backups
        backup1 = temp_dir / "backup1.yaml"
        backup1.write_text("backup1")
        backup2 = temp_dir / "backup2.yaml"
        backup2.write_text("backup2")

        backups = config_manager.list_backups()
        assert len(backups) == 2

    def test_export_config(self, config_manager, temp_dir):
        config = {"test": "data"}
        export_path = temp_dir / "exported_config.yaml"

        result = config_manager.export_config(config, export_path)
        assert result is True
        assert export_path.exists()

        with open(export_path) as f:
            exported = yaml.safe_load(f)
        assert exported == config

    def test_import_config(self, config_manager, temp_dir):
        config_data = {"imported": "data"}
        import_path = temp_dir / "import_config.yaml"
        import_path.write_text(yaml.dump(config_data))

        imported = config_manager.import_config(import_path)
        assert imported == config_data


class TestConvenienceFunctions:
    def test_generate_config_from_template_function(self, temp_dir):
        template_content = """
        jira:
          url: "{{ jira_url }}"
        """
        template_path = temp_dir / "template.yaml.j2"
        template_path.write_text(template_content)

        template = ConfigTemplate(
            name="test",
            description="Test",
            template_path=template_path,
            variables=["jira_url"],
            dependencies=[]
        )

        variables = {"jira_url": "https://test.local/"}
        result = generate_config_from_template(template, variables)
        assert result.is_valid is True

    def test_apply_config_overrides_function(self):
        config = {"jira": {"url": "old"}}
        overrides = [
            ConfigOverride(
                path="jira.url",
                value="new",
                environment=EnvironmentType.DEVELOPMENT
            )
        ]

        result = apply_config_overrides(config, overrides)
        assert result.config["jira"]["url"] == "new"

    def test_validate_config_function(self, temp_dir):
        schema = {"type": "object", "properties": {"test": {"type": "string"}}}
        schema_path = temp_dir / "schema.json"
        schema_path.write_text(json.dumps(schema))

        config = {"test": "value"}
        result = validate_config(config, schema_path)
        assert result.is_valid is True

    def test_backup_config_function(self, temp_dir):
        config_path = temp_dir / "config.yaml"
        config_path.write_text("test: data")

        backup = backup_config(config_path, "Test backup")
        assert backup.backup_id is not None
        assert backup.backup_path.exists()

    def test_restore_config_function(self, temp_dir):
        # Create backup
        config_path = temp_dir / "config.yaml"
        config_path.write_text("original: data")
        backup = backup_config(config_path, "Test backup")

        # Modify and restore
        config_path.write_text("modified: data")
        result = restore_config(backup.backup_id)
        assert result is True

    def test_encrypt_decrypt_config_functions(self):
        config_data = {"secret": "data"}
        key = b"test_key_32_bytes_long_for_fernet_"
        
        encrypted = encrypt_config(config_data, key)
        decrypted = decrypt_config(encrypted, key)
        assert decrypted == config_data


if __name__ == "__main__":
    pytest.main([__file__]) 