# Test Suite Documentation

This directory contains tests for the Jira to OpenProject migration tool.

## Overview

The test suite uses Python's built-in `unittest` framework and covers:

1. Environment and configuration validation
2. Individual migration component functionality
3. Core utilities and client operations
4. End-to-end migration processes

## Key Test Files

- `test_environment.py`: Basic tests to ensure the environment is properly set up
- `test_main.py`: Tests for the main CLI interface
- `test_user_migration.py`: Tests for user migration functionality
- `test_custom_field_migration.py`: Tests for custom field extraction, mapping, and migration via Rails console

## Running Tests

To run all tests:

```bash
python -m unittest discover tests
```

To run a specific test file:

```bash
python -m unittest tests/test_custom_field_migration.py
```

To run a specific test case:

```bash
python -m unittest tests.test_custom_field_migration.TestCustomFieldMigration.test_extract_jira_custom_fields
```

## Test Environment

Tests use mocking to avoid making actual API calls to Jira or OpenProject. The `unittest.mock` module is used extensively to patch external dependencies and simulate API responses.

## Code Coverage

Code coverage is not currently measured automatically. Consider adding a coverage tool if needed.

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
