# Configuration Guide

This document explains the configuration system for the Jira to OpenProject migration tool.

**Key Documentation:**

*   **Project Overview:** [README.md](../README.md)
*   **Development Guide:** [development.md](development.md)
*   **Tasks & Status:** [TASKS.md](../TASKS.md)

## Overview

The migration tool uses a consolidated configuration approach combining:

1.  **YAML Configuration File:** `config/config.yaml` for structured settings, defaults, and non-sensitive parameters.
2.  **Environment Variables:** For secrets, environment-specific overrides, and compatibility with containerized deployments.
    *   `.env`: Contains version-controlled default environment variables.
    *   `.env.local`: Contains **non-version-controlled** custom settings and secrets (e.g., API keys, passwords). This file overrides `.env`.
    *   Shell Environment Variables: System-level environment variables have the highest priority.
3.  **Configuration Loader:** The `src.config_loader.ConfigLoader` class reads and merges these sources, making them accessible via the `src.config` module.

## Configuration Priority

Configuration values are loaded in the following order, with later sources overriding earlier ones:

1.  Values defined in `config/config.yaml`.
2.  Variables defined in the `.env` file.
3.  Variables defined in the `.env.local` file.
4.  Shell environment variables set in the system.

## Accessing Configuration

All application code should access configuration settings through the central `src.config` object or its helper functions.

```python
from src import config

# Get specific configuration sections (as dictionaries)
jira_config = config.jira_config
openproject_config = config.openproject_config
migration_config = config.migration_config
rails_config = config.rails_config # Configuration for Rails console access

# Access specific values using .get() for safety
jira_url = jira_config.get("url")
api_key = openproject_config.get("api_key") # Likely overridden by .env.local
batch_size = migration_config.get("batch_size", 100) # Default value if not set
ssh_user = rails_config.get("ssh_user") # Likely overridden by .env.local

# Get a value from a specific section with a default
ssl_verify = config.get_value("migration", "ssl_verify", True)

# Get the fully merged configuration dictionary
all_settings = config.get_config()
```

## Environment Variables (`.env`, `.env.local`, Shell)

Environment variables provide a secure way to handle secrets and allow for easy overrides in different environments (development, testing, production).

*   **Prefix:** All environment variables used by this application **must** start with the `J2O_` prefix to avoid conflicts.
*   **.env:** Contains default, non-sensitive values. Commit this file to Git.
*   **.env.local:** Contains sensitive data (API keys, usernames, passwords, SSH keys) and specific overrides for your local setup. **Do NOT commit this file to Git.** Ensure it's listed in `.gitignore`.

### Key Environment Variables:

| Variable                      | Example Value                         | Description                                                     |
| :---------------------------- | :------------------------------------ | :-------------------------------------------------------------- |
| `J2O_JIRA_URL`                | `https://jira.example.com`            | URL of the source Jira instance.                                |
| `J2O_JIRA_USERNAME`           | `migration_user`                      | Username for Jira API authentication.                           |
| `J2O_JIRA_API_TOKEN`          | `your_jira_api_token`                 | API token or password for Jira API authentication.              |
| `J2O_OPENPROJECT_URL`         | `https://openproject.example.com`     | URL of the target OpenProject instance.                         |
| `J2O_OPENPROJECT_API_KEY`     | `your_openproject_api_key`            | API key for OpenProject API authentication.                     |
| `J2O_LOG_LEVEL`               | `INFO`                                | Logging level (DEBUG, INFO, NOTICE, SUCCESS, WARNING, ERROR, CRITICAL) |
| `J2O_BATCH_SIZE`              | `50`                                  | Default batch size for processing items (can be overridden in YAML). |
| `J2O_RATE_LIMIT_REQUESTS`     | `100`                                 | Max API requests per period (can be overridden in YAML).         |
| `J2O_RATE_LIMIT_PERIOD`       | `60`                                  | Rate limit period in seconds (can be overridden in YAML).       |
| `J2O_SSL_VERIFY`              | `false`                               | Set to `false` to disable SSL verification (use with caution). |
| `J2O_OPENPROJECT_SERVER`      | `op.server.example.com`               | Hostname/IP for SSH access to OpenProject server (for Rails). |
| `J2O_OPENPROJECT_SSH_USER`    | `deployer`                            | SSH username for OpenProject server (for Rails).                |
| `J2O_OPENPROJECT_SSH_KEY_PATH`| `/home/user/.ssh/id_rsa_op`           | Path to SSH private key for OpenProject server (for Rails).     |
| `J2O_OPENPROJECT_CONTAINER`   | `openproject-web-1`                   | Name of the OpenProject Docker container (for Rails).           |
| `J2O_OPENPROJECT_RAILS_PATH`  | `/app`                                | Path to OpenProject installation within the container (for Rails). |

## YAML Configuration (`config/config.yaml`)

This file defines the structure and default values for less sensitive or more complex configuration parameters.

```yaml
migration:
  # General migration settings
  batch_size: 100                # Default batch size (overridden by J2O_BATCH_SIZE env var)
  ssl_verify: true               # Default SSL verification (overridden by J2O_SSL_VERIFY env var)
  parallel_downloads: 5          # Number of parallel attachment downloads
  data_dir: "var/data"           # Directory for mapping files, extracted data
  log_dir: "var/logs"            # Directory for log files
  script_dir: "var/scripts"      # Directory for generated Ruby scripts

performance:
  # Rate limiting for API calls
  rate_limit:
    jira:
      requests: 100            # Max requests (overridden by J2O_RATE_LIMIT_REQUESTS)
      period: 60               # Period in seconds (overridden by J2O_RATE_LIMIT_PERIOD)
    openproject:
      requests: 100
      period: 60

jira:
  # Jira specific settings (URL/Credentials set via Env Vars)
  api_version: "2"
  search_max_results: 50         # Page size for JQL searches
  attachment_download_timeout: 120 # Timeout in seconds for downloading attachments
  scriptrunner:
    enabled: false                 # Set to true if using ScriptRunner endpoint
    custom_field_options_endpoint: "" # Full URL to ScriptRunner endpoint (if enabled)

openproject:
  # OpenProject specific settings (URL/Credentials set via Env Vars)
  api_version: "v3"
  attachment_upload_timeout: 180 # Timeout in seconds for uploading attachments
  # User to attribute actions to during migration (if not mapping)
  default_migration_user: "admin"

rails_console:
  # Settings for interacting with OP Rails console via SSH/Docker
  # Server, User, Key Path, Container Name, App Path are set via Env Vars
  connection_timeout: 30         # SSH connection timeout
  command_timeout: 300           # Timeout for executing commands in Rails console
  shell_prompt_regex: '.*# '      # Regex to detect the shell prompt
  rails_prompt_regex: '.*irb.*> '  # Regex to detect the Rails console prompt

mappings:
  # Configuration for how different entities are mapped
  users:
    # Strategy: 'email', 'username', 'ldap', 'manual'
    strategy: "email"
    # If strategy is 'manual', specify the mapping file
    # mapping_file: "var/data/manual_user_map.json"
  projects:
    # If true, attempts to map Jira project keys to OP identifiers
    use_jira_key_as_identifier: true
  # Add other mapping configurations as needed (e.g., status, type)
```

## Best Practices

1.  **Prioritize `.env.local` for Secrets:** Never commit API keys, passwords, or specific server details to Git. Use `.env.local`.
2.  **Use `src.config`:** Always access configuration through the `src.config` module for consistency.
3.  **Use `.get()`:** Access dictionary keys using `.get("key", default_value)` to avoid `KeyError` exceptions if a setting is missing.
4.  **Keep YAML for Defaults:** Use `config.yaml` for non-sensitive defaults and structural organization.
5.  **Document New Settings:** When adding new configuration options, document them in this guide, `config.yaml` (with comments), and the `.env` template.
6.  **Validate Configuration:** Consider adding validation logic within `ConfigLoader` or on first use to ensure required settings are present and correctly formatted.

## Python 3.13 Configuration Features

The configuration system takes advantage of Python 3.13 features:

- Type hints for configuration values
- Pattern matching for processing configuration sources
- Dataclasses with slots for configuration schema
- f-strings with `=` operator for debug output

## Impact on Migration

These configuration settings directly affect migration behavior:

- `J2O_BATCH_SIZE`: Controls how many items are processed in each batch, affecting memory usage and performance
- `J2O_RATE_LIMIT_REQUESTS` and `J2O_RATE_LIMIT_PERIOD`: Prevent API rate limit issues
- `J2O_SSL_VERIFY`: May need to be disabled in test environments

For details on running migrations, see [Migration Components](../PROGRESS.md#migration-components) in the Progress Tracker.
