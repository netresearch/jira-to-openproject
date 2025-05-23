# Configuration Guide

This document explains the configuration system for the Jira to OpenProject migration tool. It is up to date with the current codebase and environment variable usage.

**Key Documentation:**

* **Project Overview:** [README.md](../README.md)
* **Development Guide:** [development.md](development.md)
* **Tasks & Status:** [TASKS.md](../TASKS.md)

## Overview

The migration tool uses a consolidated configuration approach combining:

1. **YAML Configuration File:** `config/config.yaml` for structured settings, defaults, and non-sensitive parameters.
2. **Environment Variables:** For secrets, environment-specific overrides, and compatibility with containerized deployments.
    * `.env`: Contains version-controlled default environment variables.
    * `.env.local`: Contains **non-version-controlled** custom settings and secrets (e.g., API keys, passwords). This file overrides `.env`.
    * Shell Environment Variables: System-level environment variables have the highest priority.
3. **Command-Line Arguments:** CLI arguments like `--force` and `--no-backup` are integrated into the configuration system.
4. **Configuration Loader:** The `src.config_loader.ConfigLoader` class reads and merges these sources, making them accessible via the `src.config` module.

## Configuration Priority

Configuration values are loaded in the following order, with later sources overriding earlier ones:

1. Values defined in `config/config.yaml`.
2. Variables defined in the `.env` file.
3. Variables defined in the `.env.local` file.
4. Shell environment variables set in the system.
5. Command-line arguments specified when running the tool.

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

# Access CLI arguments through the migration_config
force_mode = migration_config.get("force", False)
no_backup = migration_config.get("no_backup", False)
dry_run = migration_config.get("dry_run", False)

# Get a value from a specific section with a default
ssl_verify = config.get_value("migration", "ssl_verify", True)

# Get the fully merged configuration dictionary
all_settings = config.get_config()
```

## Command-Line Arguments

Command-line arguments are automatically integrated into the configuration system. This means that components can access them consistently via the configuration system rather than having to pass them around explicitly. The following CLI arguments are supported:

| Argument       | Config Key             | Description                                      |
|----------------|------------------------|--------------------------------------------------|
| `--dry-run`    | `migration_config['dry_run']` | Run without making changes to OpenProject      |
| `--no-backup`  | `migration_config['no_backup']` | Skip creating a backup before migration      |
| `--force`      | `migration_config['force']` | Force extraction of data even if it already exists |

To access these from any component:

```python
from src import config

# This will be True if --force was specified on the command line
if config.migration_config.get("force", False):
    # Force mode is enabled
    # ...
```

## Environment Variables (`.env`, `.env.local`, Shell)

Environment variables provide a secure way to handle secrets and allow for easy overrides in different environments (development, testing, production).

* **Prefix:** All environment variables used by this application **must** start with the `J2O_` prefix to avoid conflicts.
* **.env:** Contains default, non-sensitive values. Commit this file to Git.
* **.env.local:** Contains sensitive data (API keys, usernames, passwords, SSH keys) and specific overrides for your local setup. **Do NOT commit this file to Git.** Ensure it's listed in `.gitignore`.

### Key Environment Variables:

| Variable                      | Example Value                         | Description                                                     |
| :---------------------------- | :------------------------------------ | :-------------------------------------------------------------- |
| `J2O_JIRA_URL`                | `https://jira.local`                  | URL of the source Jira instance.                                |
| `J2O_JIRA_USERNAME`           | `migration_user`                      | Username for Jira API authentication.                           |
| `J2O_JIRA_API_TOKEN`          | `your_jira_api_token`                 | API token or password for Jira API authentication.              |
| `J2O_OPENPROJECT_URL`         | `https://openproject.local`           | URL of the target OpenProject instance.                         |
| `J2O_OPENPROJECT_API_KEY`     | `your_openproject_api_key`            | API key for OpenProject API authentication.                     |
| `J2O_LOG_LEVEL`               | `INFO`                                | Logging level (DEBUG, INFO, NOTICE, SUCCESS, WARNING, ERROR, CRITICAL) |
| `J2O_BATCH_SIZE`              | `50`                                  | Default batch size for processing items (can be overridden in YAML). |
| `J2O_RATE_LIMIT_REQUESTS`     | `100`                                 | Max API requests per period (can be overridden in YAML).         |
| `J2O_RATE_LIMIT_PERIOD`       | `60`                                  | Rate limit period in seconds (can be overridden in YAML).       |
| `J2O_SSL_VERIFY`              | `false`                               | Set to `false` to disable SSL verification (use with caution). |
| `J2O_OPENPROJECT_SERVER`      | `openproject.local`                   | Hostname/IP for SSH access to OpenProject server (for Rails). |
| `J2O_OPENPROJECT_SSH_USER`    | `deployer`                            | SSH username for OpenProject server (for Rails).                |
| `J2O_OPENPROJECT_SSH_KEY_PATH`| `/home/user/.ssh/id_rsa_op`           | Path to SSH private key for OpenProject server (for Rails).     |
| `J2O_OPENPROJECT_CONTAINER`   | `openproject-web-1`                   | Name of the OpenProject Docker container (for Rails).           |
| `J2O_OPENPROJECT_RAILS_PATH`  | `/app`                                | Path to OpenProject installation within the container (for Rails). |

## YAML Configuration (`config/config.yaml`)

This file defines the structure and default values for less sensitive or more complex configuration parameters. See the file for the most up-to-date options and comments.

## Best Practices

1. **Prioritize `.env.local` for Secrets:** Never commit API keys, passwords, or specific server details to Git. Use `.env.local`.
2. **Use `src.config`:** Always access configuration through the `src.config` module for consistency.
3. **Use `.get()`:** Access dictionary keys using `.get("key", default_value)` to avoid `KeyError` exceptions if a setting is missing.
4. **Keep YAML for Defaults:** Use `config.yaml` for non-sensitive defaults and structural organization.
5. **Document New Settings:** When adding new configuration options, document them in this guide, `config.yaml` (with comments), and the `.env` template.
6. **Validate Configuration:** Consider adding validation logic within `ConfigLoader` or on first use to ensure required settings are present and correctly formatted.

## Python 3.13 Configuration Features

* Type hints for configuration values
* Pattern matching for processing configuration sources
* Dataclasses with slots for configuration schema
* f-strings with `=` operator for debug output

## Impact on Migration

These configuration settings directly affect migration behavior:

* `J2O_BATCH_SIZE`: Controls how many items are processed in each batch, affecting memory usage and performance
* `J2O_RATE_LIMIT_REQUESTS` and `J2O_RATE_LIMIT_PERIOD`: Prevent API rate limit issues
* `J2O_SSL_VERIFY`: May need to be disabled in test environments

For details on running migrations, see [TASKS.md](../TASKS.md).

## Mapping Files

The following mapping files are generated and used during migration:

* `var/data/user_mapping.json` - Maps Jira users to OpenProject users
* `var/data/project_mapping.json` - Maps Jira projects to OpenProject projects
* `var/data/issue_type_mapping.json` - Maps Jira issue types to OpenProject work package types
* `var/data/status_mapping.json` - Maps Jira statuses to OpenProject statuses
* `var/data/link_type_mapping.json` - Maps Jira link types to OpenProject relation types
* `var/data/custom_field_mapping.json` - Maps Jira custom fields to OpenProject custom fields
* `var/data/workflow_mapping.json` - Maps Jira workflows to OpenProject workflows
* `var/data/company_mapping.json` - Maps Tempo companies to OpenProject top-level projects
* `var/data/account_mapping.json` - Maps Tempo accounts to OpenProject custom field values

### User-Configurable Mappings

Some mappings can be manually configured to improve matching:

#### Link Type Mapping

Due to OpenProject API limitations (no ability to retrieve, add, or edit relation types), you need to map Jira link types to OpenProject's default relation types:

1. Generate a template with all Jira link types:
   ```bash
   python src/main.py --components link_types --generate-mapping-template
   ```

2. Edit the generated template file at `var/data/link_type_user_mapping_template.json` and save it as `var/data/link_type_user_mapping.json`.

3. For each Jira link type, either:
   - Specify an `openproject_id` from the available defaults: "relates", "duplicates", "blocks", "precedes", "includes"
   - Set `create_custom_field` to `true` to create a custom field for unmapped types

4. Run the link type migration again to apply your mappings:
   ```bash
   python src/main.py --components link_types
   ```

The migration will automatically use your custom mappings and create custom fields for the unmapped types.

**Example `link_type_user_mapping.json`:**
```json
{
  "10000": {
    "jira_id": "10000",
    "jira_name": "Blocks",
    "jira_outward": "blocks",
    "jira_inward": "is blocked by",
    "openproject_id": "blocks",
    "create_custom_field": false
  },
  "10001": {
    "jira_id": "10001",
    "jira_name": "Clones",
    "jira_outward": "clones",
    "jira_inward": "is cloned by",
    "openproject_id": "",
    "create_custom_field": true
  }
}
```
