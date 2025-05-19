# Test Utilities

This directory contains utility functions, classes, and helpers used across the test suite for the Jira to OpenProject migration tool.

## Purpose

The `utils` directory serves as a centralized location for:

- Test fixtures
- Helper functions
- Test data generators
- Mock factories
- Test configuration utilities

These utilities help maintain DRY principles and consistency across different test types.

## Available Utilities

### Mock Factories

- `mock_factory.py` - Functions to create consistent mock objects

### Test Data

- `data_generators.py` - Functions to generate test data for various entities

### Helper Functions

- `assertions.py` - Custom assertion helpers for common test patterns
- `docker_helpers.py` - Utilities for working with Docker in tests
- `test_configuration.py` - Functions to configure test environments

## Usage Guidelines

1. Place any code used across multiple test files in this directory
2. Keep utility functions focused on one task
3. Document utility functions thoroughly with docstrings
4. Add type hints to all function signatures
5. Group related utilities into appropriate modules
6. Write tests for complex utility functions

## Example Usage

```python
import pytest
from tests.utils.mock_factory import create_mock_jira_project
from tests.utils.assertions import assert_projects_equivalent

def test_project_transformation():
    # Use utility to create mock test data
    jira_project = create_mock_jira_project(key="TEST", name="Test Project")

    # Run transformation
    op_project = transform_project(jira_project)

    # Use custom assertion helper
    assert_projects_equivalent(op_project, jira_project)
```

## Adding New Utilities

When adding new utility functions or classes:

1. Check if an existing module is appropriate for your utility
2. Create a new module if needed with a clear, specific purpose
3. Write thorough docstrings explaining the purpose and usage
4. Consider adding simple examples in docstrings
5. Use appropriate type hints for function signatures
