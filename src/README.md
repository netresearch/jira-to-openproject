# Source Code (`src/`)

This directory contains the core Python source code for the Jira to OpenProject migration tool.

## Directory Structure

```
src/
├── __init__.py
├── clients/                 # API clients for interacting with Jira and OpenProject
│   ├── __init__.py
│   ├── jira_client.py         # Handles communication with the Jira REST API
│   ├── openproject_client.py  # Handles communication with the OpenProject REST API
│   └── openproject_rails_client.py # Handles interaction with OpenProject Rails console via SSH/Docker
├── config.py                # Provides centralized access to configuration settings
├── config_loader.py         # Loads and merges configuration from YAML and environment variables
├── display.py               # Utilities for displaying progress and information (using Rich library)
├── mappings/                # Logic related to mapping entities between systems
│   └── mappings.py          # (Potential location for mapping helper functions/classes)
├── migrations/              # Core migration logic for each data type (component)
│   ├── __init__.py
│   ├── base_migration.py    # Abstract base class for all migration components
│   ├── account_migration.py # Migrates Tempo Accounts (needs review/refactor?)
│   ├── company_migration.py # Migrates Companies (based on defined strategy)
│   ├── custom_field_migration.py # Migrates Custom Fields (uses Rails Client)
│   ├── issue_type_migration.py   # Migrates Issue Types / WP Types (uses Rails Client)
│   ├── link_type_migration.py    # Migrates Issue Link Types / Relations
│   ├── project_migration.py    # Migrates Projects
│   ├── status_migration.py     # Migrates Statuses
│   ├── user_migration.py       # Migrates Users
│   ├── work_package_migration.py # Migrates Issues / Work Packages (including details)
│   └── workflow_migration.py   # Analyzes Workflows for manual configuration mapping
├── models/                  # Data models or dataclasses (if needed, currently minimal)
│   └── __init__.py
│   └── mapping.py           # Defines mapping data structures
├── utils.py                 # General utility functions used across the application
└── cleanup_openproject.py   # Script/module to help clean up migrated data in OpenProject (for testing)
```

## Key Modules & Classes

*   **`src/main.py`:** The new unified entry point for the application. Provides a single command-line interface with subcommands for different operations (migrate, export, import).
*   **`run_migration.py` (in root):** The legacy main entry point for migration functionality. Still usable directly but now integrated into the `src/main.py` interface.
*   **`export_work_packages.py` (in root):** The legacy entry point for work package export functionality. Still usable directly but now integrated into the `src/main.py` interface.
*   **`src/config_loader.py:ConfigLoader`:** Responsible for loading configuration from `config.yaml`, `.env`, `.env.local`, and environment variables.
*   **`src/config.py`:** Provides a global access point (`config` object) to the loaded configuration.
*   **`src/clients/jira_client.py:JiraClient`:** Interacts with the Jira API to fetch data.
*   **`src/clients/openproject_client.py:OpenProjectClient`:** Interacts with the OpenProject API v3 to create/update data.
*   **`src/clients/openproject_rails_client.py:OpenProjectRailsClient`:** Connects via SSH to the OpenProject server, enters the Docker container, and executes commands/scripts within the Rails console. Used for operations not supported by the API.
*   **`src/migrations/base_migration.py:BaseMigration`:** Abstract base class defining the interface for all migration components (`run`, `_extract`, `_map`, `_load`, `_test`).
*   **`src/migrations/*_migration.py`:** Concrete implementations of `BaseMigration` for each specific data type (e.g., `UserMigration`, `ProjectMigration`). Each handles the Extract-Map-Load process for its component.
*   **`src/display.py`:** Contains functions for user-friendly console output using the `rich` library (e.g., progress bars, tables, formatted logs).
*   **`src/mappings/mappings.py`:** (Currently minimal) Intended for helper functions related to loading, saving, and applying mapping files (`var/data/*_mapping.json`).
*   **`src/models/mapping.py`:** Defines dataclasses or structures used for storing mapping information.
*   **`src/utils.py`:** Holds common helper functions (e.g., file handling, date parsing, sanitization) used by multiple modules.
*   **`src/cleanup_openproject.py`:** Provides functionality to remove data created by the migration tool in an OpenProject instance, primarily useful during testing and development.

## Development Notes

*   Code should follow standards outlined in [docs/development.md](../docs/development.md).
*   New migration components should inherit from `BaseMigration`.
*   API interactions should be encapsulated within the `clients/` modules.
*   Configuration should always be accessed via `src.config`.
