# Configuration Guide

Configuration system for the Jira to OpenProject migration tool using YAML files and environment variables.

## Configuration Sources

The migration tool uses these configuration sources in order of priority:

1. Command-line arguments (highest priority)
2. Environment variables
3. `.env.local` file (local overrides, not version controlled)
4. `.env` file (defaults, version controlled)
5. `config/config.yaml` (structured settings)

## Configuration Access

```python
from src import config

# Get configuration sections
jira_config = config.jira_config
openproject_config = config.openproject_config
migration_config = config.migration_config
rails_config = config.rails_config

# Access specific values safely
jira_url = jira_config.get("url")
api_key = openproject_config.get("api_key")
batch_size = migration_config.get("batch_size", 100)
```

## Environment Variables

All environment variables use the `J2O_` prefix. Key variables:

| Variable | Description |
|----------|-------------|
| `J2O_JIRA_URL` | Jira server URL |
| `J2O_JIRA_USERNAME` | Jira authentication username |
| `J2O_JIRA_API_TOKEN` | Jira API token or password |
| `J2O_OPENPROJECT_URL` | OpenProject server URL |
| `J2O_OPENPROJECT_API_KEY` | OpenProject API key |
| `J2O_OPENPROJECT_SSH_HOST` | SSH hostname for Rails console |
| `J2O_OPENPROJECT_SSH_USER` | SSH username |
| `J2O_OPENPROJECT_SSH_KEY_PATH` | SSH private key file path |
| `J2O_POSTGRES_HOST` | PostgreSQL host |
| `J2O_POSTGRES_PORT` | PostgreSQL port |
| `J2O_POSTGRES_DB` | PostgreSQL database name |
| `J2O_POSTGRES_USER` | PostgreSQL username |
| `J2O_POSTGRES_PASSWORD` | PostgreSQL password |

## Command-Line Arguments

| Argument | Config Key | Description |
|----------|------------|-------------|
| `--dry-run` | `migration_config['dry_run']` | Run without making changes |
| `--no-backup` | `migration_config['no_backup']` | Skip backup creation |
| `--force` | `migration_config['force']` | Force data extraction |

## Configuration Structure

**YAML Configuration** (`config/config.yaml`):
```yaml
jira:
  batch_size: 100
  max_retries: 3

openproject:
  batch_size: 50
  timeout: 30

migration:
  components:
    - users
    - projects
    - work_packages
```

**Environment Files**:
- `.env` - Default values (version controlled)
- `.env.local` - Local overrides and secrets (not version controlled)

## Security

- Store sensitive data (passwords, API keys) in `.env.local`
- Never commit `.env.local` to version control
- Use strong, unique passwords for database connections
- Restrict SSH key permissions (`chmod 600`)
