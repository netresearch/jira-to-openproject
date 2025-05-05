# YOLO Development Plan: Complete Removal of Old Client

This plan describes the steps to completely remove the old OpenProjectRailsClient, the adapter, and all migration/backward-compatibility code. We follow YOLO development principles with no transition or backward compatibility.

## Step 1: Remove Migration/Compatibility Files

Files to delete:
- `src/clients/openproject_rails_client.py`
- `src/clients/openproject_rails_adapter.py`
- `scripts/update_migration_tests.py`
- `scripts/update_migration_sources.py`
- `scripts/performance_profiler.py` (uses the adapter)
- `docs/migration_guide_rails_client.md`
- `docs/rails_client_refactoring_status.md`

## Step 2: Update Imports and References

Files to update:
- `src/cleanup_openproject.py` - Update to use OpenProjectClient
- `src/migrations/work_package_migration.py` - Remove adapter imports
- `src/migrations/account_migration.py` - Remove adapter imports
- `tests/test_company_migration.py` - Update to use OpenProjectClient
- Update all migration components to use new client directly
- Remove all references to old client and adapter

## Step 3: Update Tests

- Update all test files to work with OpenProjectClient directly
- Remove any tests specific to the adapter or old client
- Fix any failing tests that relied on the old client

## Step 4: Update Documentation

- Update `README.md` to remove migration information
- Update `rails_tasks.md` to reflect the removal
- Remove references to the old client from all documentation

## Step 5: Verify Everything Works

- Run all tests to ensure they pass with the new client
- Run linter to check for any remaining references
- Test the application to ensure it works without the old client

## Detailed Implementation Plan

### Task 1: Remove Files
```bash
rm src/clients/openproject_rails_client.py
rm src/clients/openproject_rails_adapter.py
rm scripts/update_migration_tests.py
rm scripts/update_migration_sources.py
rm scripts/performance_profiler.py
rm docs/migration_guide_rails_client.md
rm docs/rails_client_refactoring_status.md
```

### Task 2: Fix Imports in Migrations

The following files need updating:
1. `src/cleanup_openproject.py` - Change imports and type annotations
2. `src/migrations/work_package_migration.py` - Remove adapter imports
3. `src/migrations/account_migration.py` - Remove adapter imports
4. `src/migrations/base_migration.py` - Update typehints
5. `src/migrations/custom_field_migration.py` - Ensure it uses new client
6. `src/migrations/project_migration.py` - Update references
7. `src/migrations/status_migration.py` - Update references
8. Other migration files with adapter references

### Task 3: Fix Tests

The following test files need updating:
1. `tests/test_account_migration.py` - Update mock objects
2. `tests/test_work_package_migration.py` - Update mock objects
3. `tests/test_company_migration.py` - Remove adapter references
4. `tests/test_custom_field_migration.py` - Update to use new client directly
5. `tests/test_issue_type_migration.py` - Update mock objects
6. Other test files with references to the old client

### Task 4: Update Documentation

1. `README.md`:
   - Remove migration information
   - Remove references to the old client
   - Update architecture section to only reference the new client

2. `rails_tasks.md`:
   - Remove migration sections
   - Update to reflect direct use of new client

3. `docs/component_architecture.md`:
   - Remove adapter section
   - Update to focus only on the new client architecture

### Task 5: Run Tests and Verify

```bash
pytest tests/
ruff check .
```
