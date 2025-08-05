# 🚀 New Configuration System Documentation

## Overview

The Jira to OpenProject migration tool now uses a **cutting-edge, type-safe configuration system** built with Pydantic v2 and pydantic-settings. This system provides:

- ✅ **Type safety** with automatic validation
- ✅ **IDE support** with autocomplete and type hints
- ✅ **Environment-specific** configurations
- ✅ **direnv integration** for project-specific variables
- ✅ **CLI tools** for validation and testing
- ✅ **Comprehensive error messages** with actionable feedback

## Configuration Sources (Precedence Order)

```
1. Command-line arguments (highest priority)
2. Environment variables (J2O_* prefix)
3. .envrc (direnv - project-specific environment)
4. .env (base configuration)
5. config/config.yaml (structured YAML settings)
6. config/environments/{environment}.yaml (environment-specific overrides)
```

## File Structure

```
config/
├── __init__.py              # Package initialization
├── loader.py                # Main configuration loader
├── cli.py                   # CLI tools for configuration management
├── schemas/
│   ├── __init__.py          # Schemas package initialization
│   └── settings.py          # Pydantic settings models
├── config.yaml              # Base configuration
└── environments/
    ├── development.yaml     # Development overrides
    ├── staging.yaml         # Staging overrides
    └── production.yaml      # Production overrides

.env                         # Base environment variables
.envrc                       # direnv project-specific variables
.env.example                 # Template with documentation
```

## Quick Start

### 1. Install Dependencies

```bash
pip install pydantic-settings
```

### 2. Setup direnv (Recommended)

```bash
# Install direnv
brew install direnv  # macOS
sudo apt install direnv  # Ubuntu

# Add to shell
echo 'eval "$(direnv hook bash)"' >> ~/.bashrc
echo 'eval "$(direnv hook zsh)"' >> ~/.zshrc

# Allow .envrc in project
direnv allow
```

### 3. Configure Your Environment

Edit `.envrc` with your actual credentials:

```bash
# Jira Configuration
export J2O_JIRA_URL="https://your-company.atlassian.net"
export J2O_JIRA_USERNAME="your-email@company.com"
export J2O_JIRA_API_TOKEN="your_actual_jira_token"

# OpenProject Configuration
export J2O_OPENPROJECT_URL="https://your-openproject.company.com"
export J2O_OPENPROJECT_API_TOKEN="your_actual_openproject_token"
```

### 4. Validate Configuration

```bash
python -m config.cli validate
```

## CLI Tools

The new configuration system includes powerful CLI tools for management and validation:

### Validate Configuration

```bash
python -m config.cli validate
```

Validates all configuration settings and ensures:
- Required directories exist or can be created
- Configuration values are valid
- No missing required settings

### Test Service Connections

```bash
python -m config.cli test-connections
```

Tests connectivity to:
- Jira API
- OpenProject API

### Show Configuration Summary

```bash
# Show configuration (secrets hidden)
python -m config.cli show

# Show configuration with secrets (use with caution)
python -m config.cli show --secrets
```

### Export Configuration

```bash
# Export as JSON
python -m config.cli export --format json

# Export as YAML
python -m config.cli export --format yaml

# Export to file
python -m config.cli export --format json --output config-export.json
```

### Create direnv Template

```bash
python -m config.cli create-envrc
```

Creates a `.envrc` template with current configuration values.

## Configuration Settings

### Jira Configuration

| Setting | Environment Variable | Description | Default |
|---------|---------------------|-------------|---------|
| `jira_url` | `J2O_JIRA_URL` | Jira instance URL | Required |
| `jira_username` | `J2O_JIRA_USERNAME` | Jira username/email | Required |
| `jira_api_token` | `J2O_JIRA_API_TOKEN` | Jira API token | Required |
| `jira_projects` | - | Projects to migrate | From YAML |
| `jira_batch_size` | `J2O_JIRA_BATCH_SIZE` | API batch size | 100 |
| `jira_fields` | `J2O_JIRA_FIELDS` | Fields to retrieve | Default fields |
| `jira_scriptrunner_enabled` | `J2O_JIRA_SCRIPTRUNNER_ENABLED` | Enable ScriptRunner | false |
| `jira_scriptrunner_endpoint` | `J2O_JIRA_SCRIPTRUNNER_ENDPOINT` | ScriptRunner endpoint | - |

### OpenProject Configuration

| Setting | Environment Variable | Description | Default |
|---------|---------------------|-------------|---------|
| `openproject_url` | `J2O_OPENPROJECT_URL` | OpenProject instance URL | Required |
| `openproject_api_token` | `J2O_OPENPROJECT_API_TOKEN` | OpenProject API token | Required |
| `openproject_api_key` | `J2O_OPENPROJECT_API_KEY` | Alternative API key | - |
| `openproject_server` | `J2O_OPENPROJECT_SERVER` | SSH server hostname | sobol.nr |
| `openproject_user` | `J2O_OPENPROJECT_USER` | SSH username | sebastian.mendel |
| `openproject_container` | `J2O_OPENPROJECT_CONTAINER` | Docker container name | openproject-web-1 |
| `openproject_tmux_session_name` | `J2O_OPENPROJECT_TMUX_SESSION_NAME` | tmux session name | rails_console |
| `openproject_batch_size` | `J2O_OPENPROJECT_BATCH_SIZE` | API batch size | 50 |

### Migration Configuration

| Setting | Environment Variable | Description | Default |
|---------|---------------------|-------------|---------|
| `batch_size` | `J2O_BATCH_SIZE` | Migration batch size | 100 |
| `ssl_verify` | `J2O_SSL_VERIFY` | Enable SSL verification | true |
| `log_level` | `J2O_LOG_LEVEL` | Logging level | INFO |
| `data_dir` | `J2O_DATA_DIR` | Data directory | ./data |
| `backup_dir` | `J2O_BACKUP_DIR` | Backup directory | ./backups |
| `results_dir` | `J2O_RESULTS_DIR` | Results directory | ./results |
| `component_order` | - | Migration component order | From YAML |
| `mapping_file` | - | ID mapping file | data/id_mapping.json |
| `attachment_path` | - | Attachment storage path | data/attachments |
| `skip_existing` | - | Skip existing items | true |

### Testing Configuration

| Setting | Environment Variable | Description | Default |
|---------|---------------------|-------------|---------|
| `test_mode` | `J2O_TEST_MODE` | Test mode flag | false |
| `test_mock_mode` | `J2O_TEST_MOCK_MODE` | Mock mode flag | false |
| `use_mock_apis` | `J2O_USE_MOCK_APIS` | Use mock APIs flag | false |

### Database Configuration

| Setting | Environment Variable | Description | Default |
|---------|---------------------|-------------|---------|
| `postgres_password` | `POSTGRES_PASSWORD` | PostgreSQL password | Required |
| `postgres_db` | `POSTGRES_DB` | Database name | jira_migration |
| `postgres_user` | `POSTGRES_USER` | Database username | postgres |

## Environment-Specific Configuration

The system supports environment-specific configurations through YAML files:

### Development Environment

```yaml
# config/environments/development.yaml
migration:
  log_level: "DEBUG"
  batch_size: 50

database:
  postgres_db: "jira_migration_dev"
```

### Staging Environment

```yaml
# config/environments/staging.yaml
migration:
  log_level: "INFO"
  batch_size: 100

database:
  postgres_db: "jira_migration_staging"
```

### Production Environment

```yaml
# config/environments/production.yaml
migration:
  log_level: "INFO"
  batch_size: 200

database:
  postgres_db: "jira_migration_prod"
```

## Programmatic Usage

### Basic Usage

```python
from config import load_settings, get_config_loader

# Load settings
settings = load_settings()

# Access configuration
print(f"Jira URL: {settings.jira_url}")
print(f"Batch Size: {settings.batch_size}")
```

### Using ConfigLoader

```python
from config import get_config_loader

# Get configuration loader
loader = get_config_loader()

# Access configuration sections
jira_config = loader.get_jira_config()
openproject_config = loader.get_openproject_config()
migration_config = loader.get_migration_config()
database_config = loader.get_database_config()

# Check test mode
if loader.is_test_mode():
    print("Running in test mode")
```

### Validation

```python
from config import load_settings

try:
    settings = load_settings()
    print("Configuration is valid")
except ValueError as e:
    print(f"Configuration error: {e}")
```

## Migration from Old System

The new system is **backward compatible** with the old configuration system. All existing environment variables and YAML configurations are preserved.

### What Changed

1. **Type Safety**: All configuration values are now validated with Pydantic
2. **Better Error Messages**: Clear, actionable error messages for configuration issues
3. **CLI Tools**: New command-line tools for configuration management
4. **direnv Integration**: Project-specific environment variables with `.envrc`
5. **Environment-Specific Configs**: YAML files for different environments

### What Stayed the Same

1. **Environment Variables**: All `J2O_*` environment variables work the same
2. **YAML Configuration**: `config/config.yaml` still works
3. **File Precedence**: Same precedence order for configuration sources
4. **Database Configuration**: PostgreSQL configuration unchanged

## Troubleshooting

### Common Issues

#### 1. Configuration Validation Errors

```bash
# Check what's wrong
python -m config.cli validate --log-level DEBUG
```

#### 2. Connection Test Failures

```bash
# Test connections
python -m config.cli test-connections
```

#### 3. Missing Environment Variables

```bash
# Show current configuration
python -m config.cli show
```

### Debug Mode

```bash
# Enable debug logging
python -m config.cli validate --log-level DEBUG
```

## Best Practices

### 1. Use direnv for Development

```bash
# Create .envrc with your development settings
python -m config.cli create-envrc
direnv allow
```

### 2. Keep Secrets Out of Version Control

```bash
# Add to .gitignore
echo ".env*" >> .gitignore
echo "!.env.example" >> .gitignore
```

### 3. Validate Configuration Regularly

```bash
# Add to your CI/CD pipeline
python -m config.cli validate
python -m config.cli test-connections
```

### 4. Use Environment-Specific Configs

```bash
# Create environment-specific configurations
cp config/environments/development.yaml config/environments/my-env.yaml
```

## Security Considerations

1. **Secrets Management**: Never commit `.env` files with real secrets
2. **Environment Variables**: Use environment variables for sensitive data
3. **direnv**: Use `.envrc` for project-specific secrets
4. **Validation**: Always validate configuration before use
5. **Export**: Use export tools to share configuration safely (secrets are masked)

---

**This new configuration system provides a rock-solid, type-safe, and developer-friendly way to manage your Jira to OpenProject migration configuration!** 🚀 