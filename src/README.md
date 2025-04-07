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
- **account_migration.py**: Handles Tempo Timesheet accounts (as custom fields)
- **custom_field_migration.py**: Handles custom field mapping and potential Ruby script generation
- **workflow_migration.py**: Handles workflow states and transitions
- **link_type_migration.py**: Handles issue link types
- **work_package_migration.py**: Handles issue/work package data (managed via export_work_packages.py)

Each module typically contains a class implementing the core logic for that component, invoked by the main `run_migration.py` script.

## Special-Purpose Utilities

Due to OpenProject API limitations, some components might require manual steps or alternative approaches:

*   **Custom Field Creation:** The API doesn't support this. The migration attempts direct creation via the Rails console (`--direct-migration`). If this fails, a Ruby script can be generated (`--generate-ruby` option, although direct migration is preferred) for manual execution in the Rails console.
*   **Work Package Type Creation:** Similar to custom fields, this often requires direct Rails console interaction, handled by the `--direct-migration` flag.

## Development

When developing new migration components:

1. Create a new module in the `migrations/` directory.
2. Implement the core functionality in a class inheriting from `BaseMigration`.
3. Update `run_migration.py` to import and call the new component's migration class or function within the main loop, passing necessary arguments like `dry_run`, `force`, etc.
4. Ensure the component logic is integrated into the overall workflow orchestrated by `run_migration.py`.

The master script (`run_migration.py` at the project root) coordinates the execution of all migration components.

### Example Component Execution:

```bash
# Run only the user migration
python run_migration.py --components users

# Run custom fields and issue types using direct Rails execution
python run_migration.py --components custom_fields issue_types --direct-migration

# Run the work package migration (uses direct migration implicitly if available)
python run_migration.py --components work_packages

# Run specific components together
python run_migration.py --components users projects work_packages
```
