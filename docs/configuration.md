# Configuration Guide

This document explains the configuration system for the Jira to OpenProject migration tool.

> **Related Documents**:
> - [README.md](../README.md): Project setup and overview
> - [PROGRESS.md](../PROGRESS.md): Migration progress and implementation details

## Overview

The migration tool uses a consolidated configuration approach with:

1. YAML configuration file (`config/config.yaml`) for structured settings
2. Environment variables (`.env` and `.env.local`) for sensitive or environment-specific values
3. A unified `ConfigLoader` class that manages everything

## How Configuration Works

The configuration system follows this priority order (highest to lowest):

1. Shell environment variables
2. Variables in `.env.local` (not version-controlled)
3. Variables in `.env` (version-controlled defaults)
4. Values in `config/config.yaml`

## Accessing Configuration

All code should get configuration through the `config` module:

```python
from src import config

# Get configuration sections
jira_config = config.jira_config
openproject_config = config.openproject_config
migration_config = config.migration_config

# Access configuration values
jira_url = jira_config.get("url")
api_key = openproject_config.get("api_token")
batch_size = migration_config.get("batch_size")

# Get a specific value with a default
ssl_verify = config.get_value("migration", "ssl_verify", True)

# Get the full configuration
full_config = config.get_config()
```

## Environment Variables

All environment variables use the `J2O_` prefix to avoid conflicts with other applications:

| Variable | Default | Description |
|----------|---------|-------------|
| `J2O_JIRA_URL` | - | URL of the Jira instance |
| `J2O_JIRA_USERNAME` | - | Username for Jira |
| `J2O_JIRA_API_TOKEN` | - | API token for Jira |
| `J2O_OPENPROJECT_URL` | - | URL of the OpenProject instance |
| `J2O_OPENPROJECT_API_KEY` | - | API key for OpenProject |
| `J2O_LOG_LEVEL` | `DEBUG` | Logging level (DEBUG, INFO, NOTICE, SUCCESS, WARNING, ERROR, CRITICAL) |
| `J2O_BATCH_SIZE` | `1000` | Number of items to process in each batch |
| `J2O_RATE_LIMIT_REQUESTS` | `1000` | Maximum API requests in rate limit period |
| `J2O_RATE_LIMIT_PERIOD` | `60` | Rate limit period in seconds |
| `J2O_SSL_VERIFY` | `true` | Whether to verify SSL certificates |

## Configuration Files

### config/config.yaml

This file provides the base configuration structure and default values. It includes:

- Jira settings
- OpenProject settings
- Migration settings
- Performance tuning
- Security settings

### .env and .env.local

The `.env` file contains default values that can be version-controlled. The `.env.local` file contains your custom settings and overrides the `.env` file. It is not version-controlled and should be used for sensitive data.

## Best Practices

1. Always use the config module instead of directly accessing environment variables
2. Use standard dictionary methods like `get()` with defaults for safe access to config values
3. Keep sensitive information in `.env.local`, not in `.env` or `config.yaml`
4. Use the `J2O_` prefix for all environment variables
5. When adding new settings, update `config.yaml` with reasonable defaults and documentation

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
