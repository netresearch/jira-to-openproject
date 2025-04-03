# Source Code Organization

This directory contains the source code for the Jira to OpenProject migration tool.

## Directory Structure

- **clients/**: API client implementations for Jira and OpenProject
- **migrations/**: Core migration modules for each component
- **models/**: Data models and mapping definitions
- **config.py**: Configuration loader and logging setup

## Module Organization

The migration is organized into component modules in the `migrations/` directory:

- **user_migration.py**: Handles user account migration
- **project_migration.py**: Handles project structure migration
- **company_migration.py**: Handles company data migration
- **tempo_account_migration.py**: Handles Tempo Timesheet accounts
- **custom_field_migration.py**: Handles custom field mapping and Ruby script generation
- **workflow_migration.py**: Handles workflow states and transitions
- **link_type_migration.py**: Handles issue link types

Each module follows a similar pattern:
1. A class that implements the migration logic
2. Functions to run the migration as standalone scripts
3. Command-line argument handling for direct invocation

## Special-Purpose Utilities

The custom field migration has a special capability due to OpenProject API limitations:

```bash
python -m src.migrations.custom_field_migration --generate-ruby
```

This generates a Ruby script for importing custom fields via Rails console, as OpenProject's API does not support custom field creation.

## Development

When developing new migration components:

1. Create a new module in the `migrations/` directory
2. Implement the core functionality in a class
3. Add a runner function (e.g., `run_component_migration()`)
4. Update `run_migration.py` to include the new component

The master script (`run_migration.py` at the project root) coordinates the execution of all migration components.
