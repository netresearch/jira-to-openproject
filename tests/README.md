# Test Suite Documentation

This directory contains tests for the Jira to OpenProject migration tool.

## Overview

The test suite uses `pytest` and covers:

1. Environment and configuration validation
2. Individual migration component functionality
3. Core utilities and client operations
4. End-to-end migration processes

## Key Test Files

- `test_environment.py`: Basic tests to ensure the environment is properly set up
- `test_main.py`: Tests for the main CLI interface
- `test_user_migration.py`: Tests for user migration functionality
- `test_custom_field_migration.py`: Tests for custom field extraction, mapping, and migration via Rails console
- `test_status_migration.py`: Tests for status extraction, mapping, and migration
- `test_project_migration.py`: Tests for project extraction and migration
- `test_work_package_migration.py`: Tests for work package (issue) migration, including attachments and comments
- `test_account_migration.py`: Tests for Tempo Account migration
- `test_company_migration.py`: Tests for company migration
- `test_link_type_migration.py`: Tests for link type/relation migration
- `test_issue_type_migration.py`: Tests for issue type/work package type migration
- `test_workflow_migration.py`: Tests for workflow extraction and mapping

## Running Tests

### Prerequisites

Before running tests, make sure to activate the Python virtual environment:

```bash
# From the project root
source .venv/bin/activate
# Or use the provided script
source activate.sh
```

You should see the virtual environment activated in your terminal prompt.

### Using pytest

To run all tests:

```bash
# From the project root
pytest

# Or from within the tests directory
cd tests && pytest
```

To run a specific test file:

```bash
pytest tests/test_custom_field_migration.py
```

To run a specific test case:

```bash
pytest tests/test_custom_field_migration.py::test_extract_jira_custom_fields
```

To run tests with verbose output:

```bash
pytest -v tests/test_environment.py
```

Additional pytest options:

```bash
# Show more detailed output
pytest -vv

# Only run tests matching a pattern
pytest -k "environment or project"

# Stop after first failure
pytest -x
```

### Running tests in Docker

If you're using Docker:

```bash
docker exec -it j2o-app pytest
```

## Test Environment

Tests use the `.env.test` file for configuration during test runs. This file contains mock credentials and settings suitable for testing.

Tests use mocking to avoid making actual API calls to Jira or OpenProject. The `unittest.mock` module is used extensively to patch external dependencies and simulate API responses.

## Code Coverage

Code coverage is not currently measured automatically. Consider adding a coverage tool (e.g., `pytest-cov`) if needed.

## Adding New Tests

When adding new tests:

1. Follow the existing pattern of test files and classes
2. Use descriptive names for test methods (e.g., `test_extract_jira_custom_fields`)
3. Mock external dependencies to avoid actual API calls
4. Include assertions that verify both the functionality and error handling

## Testing Custom Field Migration

The `test_custom_field_migration.py` file tests the following aspects of custom field migration:

1. **Extraction:** Tests extracting custom fields from Jira and OpenProject
2. **Mapping:** Tests mapping Jira field types to OpenProject field formats
3. **Rails Migration:** Tests the direct Rails console migration process
4. **Script Generation:** Tests generating Ruby scripts for custom field creation

These tests ensure that custom fields are properly identified, mapped to appropriate OpenProject field types, and correctly created in the target system.
