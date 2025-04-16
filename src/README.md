# Source Code (`src/`)

This directory contains the core Python source code for the Jira to OpenProject migration tool. The codebase is modular, with each migration component implemented as a separate module.

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
│   └── mappings.py          # Helper functions/classes for loading and applying mapping files
├── migrations/              # Core migration logic for each data type (component)
│   ├── __init__.py
│   ├── base_migration.py    # Abstract base class for all migration components
│   ├── account_migration.py # Migrates Tempo Accounts (Jira plugin data to OP custom field)
│   ├── company_migration.py # Migrates Companies (Jira field to OP project structure)
│   ├── custom_field_migration.py # Migrates Custom Fields (uses Rails Client for OP)
│   ├── issue_type_migration.py   # Migrates Issue Types / Work Package Types (uses Rails Client)
│   ├── link_type_migration.py    # Migrates Issue Link Types / Relations
│   ├── project_migration.py      # Migrates Projects (including hierarchy)
│   ├── status_migration.py       # Migrates Statuses (Jira to OP, with mapping and creation)
│   ├── user_migration.py         # Migrates Users (with mapping strategies)
│   ├── work_package_migration.py # Migrates Issues / Work Packages (including details, attachments, comments)
│   └── workflow_migration.py     # Analyzes and migrates Workflows (status transitions per type)
├── models/                  # Data models or dataclasses
│   ├── __init__.py
│   └── mapping.py           # Defines mapping data structures
├── utils.py                 # General utility functions used across the application
└── cleanup_openproject.py   # Script/module to help clean up migrated data in OpenProject (for testing)
```

## Key Modules & Classes

*   **`src/main.py`:** The main entry point for all application functionality.
*   **`src/config_loader.py:ConfigLoader`:** Loads configuration from YAML, `.env`, `.env.local`, and environment variables.
*   **`src/config.py`:** Provides a global access point (`config` object) to the loaded configuration.
*   **`src/clients/jira_client.py:JiraClient`:** Interacts with the Jira API to fetch data.
*   **`src/clients/openproject_client.py:OpenProjectClient`:** Interacts with the OpenProject API v3 to create/update data.
*   **`src/clients/openproject_rails_client.py:OpenProjectRailsClient`:** Connects via SSH to the OpenProject server, enters the Docker container, and executes commands/scripts within the Rails console. Used for operations not supported by the API.
*   **`src/migrations/base_migration.py:BaseMigration`:** Abstract base class defining the interface for all migration components (`run`, `_extract`, `_map`, `_load`, `_test`).
*   **`src/migrations/*_migration.py`:** Concrete implementations of `BaseMigration` for each specific data type (e.g., `UserMigration`, `ProjectMigration`). Each handles the Extract-Map-Load process for its component.
*   **`src/display.py`:** Contains functions for user-friendly console output using the `rich` library (e.g., progress bars, tables, formatted logs).
*   **`src/mappings/mappings.py`:** Helper functions for loading, saving, and applying mapping files (`var/data/*_mapping.json`).
*   **`src/models/mapping.py`:** Dataclasses or structures used for storing mapping information.
*   **`src/utils.py`:** Common helper functions (e.g., file handling, date parsing, sanitization) used by multiple modules.
*   **`src/cleanup_openproject.py`:** Removes data created by the migration tool in an OpenProject instance, primarily for testing and development.
*   **`migration.py`:** Core migration logic and coordination of the migration process.

## Development Notes

*   Code should follow standards outlined in [../docs/development.md](../docs/development.md).
*   New migration components should inherit from `BaseMigration`.
*   API interactions should be encapsulated within the `clients/` modules.
*   Configuration should always be accessed via `src.config`.

## Error Handling and Resilience

The codebase implements comprehensive error handling strategies to ensure robustness during migration:

### Key Error Handling Features

1. **Retry Logic**: API calls and critical operations implement retry mechanisms with exponential backoff to handle transient failures:
   ```python
   # Example from work_package_migration.py
   max_retries = 3
   retry_count = 0
   while retry_count < max_retries:
       try:
           result = operation()
           break
       except Exception as e:
           retry_count += 1
           # Handle error, backoff and retry
   ```

2. **State Preservation**: Long-running operations save their state regularly, allowing resume capability:
   ```python
   # Migration state is saved at the beginning of each project processing
   with open(migration_state_file, 'w') as f:
       json.dump({
           'processed_projects': list(processed_projects),
           'last_processed_project': project_key,
           'timestamp': datetime.now().isoformat()
       }, f, indent=2)
   ```

3. **Safe File Operations**: File operations include error handling and backup mechanisms:
   ```python
   try:
       with open(filepath, 'w') as f:
           json.dump(data, f, indent=2)
   except Exception as e:
       # Attempt backup save to alternate location
       try:
           backup_path = f"{filepath}.backup"
           with open(backup_path, 'w') as f:
               json.dump(data, f, indent=2)
       except Exception as backup_e:
           logger.critical(f"Failed to save backup: {str(backup_e)}")
   ```

4. **Structured Error Reporting**: Errors are logged with context and saved to dedicated files for analysis:
   ```python
   # Example from work_package_migration.py
   issues_data["work_package_migration"].append({
       "timestamp": timestamp,
       "type": "error",
       "message": error_message,
       "error": str(e),
       "traceback": str(getattr(e, "__traceback__", "No traceback available"))
   })
   ```

5. **Graceful Degradation**: The migration continues despite partial failures, with clear reporting of what succeeded and what failed:
   ```python
   # Continue to next batch/project instead of failing completely
   if not issues:
       logger.warning(f"No issues retrieved for batch starting at {start_at}")
       continue  # Move to next batch instead of breaking
   ```

6. **Rails Client Resilience**: For operations using the Rails console, we implement safeguards against connection failures and command execution errors.

### Error Files and Logs

The migration process generates several error-related files:

* `var/data/migration_issues.json`: Records warnings and errors encountered during migration
* `var/data/migration_error.json`: Records critical errors that caused migration to abort
* `var/logs/j2o.log`: Full application log with timestamps and log levels

### Implementing New Error Handling

When adding new code, follow the error handling guidelines in [../docs/development.md](../docs/development.md), particularly:

1. Use try/except blocks around all external operations (API calls, file I/O)
2. Implement retry logic for operations that might experience transient failures
3. Log all errors with appropriate context
4. Save partial results whenever possible
5. Provide clear error messages and error codes where appropriate
