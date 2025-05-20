# Testing Infrastructure

This directory contains tests for the Jira to OpenProject migration tool. The tests are organized by scope, from unit tests to end-to-end tests.

## Test Organization

The tests are organized into the following directories:

- `unit/`: Unit tests for individual components in isolation
- `functional/`: Functional tests that test interactions between components
- `integration/`: Integration tests that verify integration with external services
- `e2e/`: End-to-end tests that test complete workflows
- `utils/`: Shared test utilities, including mocks, assertions, and data generators
- `examples/`: Example tests demonstrating usage patterns

## Environment Configuration

The application uses a hierarchical environment configuration system:

### Environment Files

- `.env`: Base configuration for all environments
- `.env.local`: Local development overrides (not committed to version control)
- `.env.test`: Test-specific configuration
- `.env.test.local`: Local test-specific overrides (not committed to version control)

In test mode, both `.env` and `.env.test` files are loaded, with `.env.test` taking precedence. This allows for test-specific configuration without modifying the base configuration.

### Test Environment Fixture

The `test_env` fixture in `conftest.py` provides a mechanism to:

1. Set environment variables specifically for a test
2. Automatically clean up changes to the environment after each test
3. Ensure environment isolation between tests

Example usage:

```python
def test_example(test_env: dict[str, str]) -> None:
    # Set a test-specific environment variable
    test_env["J2O_CUSTOM_SETTING"] = "test_value"

    # Use the configured application
    # ...

    # The environment will be restored after the test
```

## Testing Configuration

The `test_config_loader.py` file contains tests for the ConfigLoader class that handles environment configuration. The test approach:

1. Uses a test-specific subclass of ConfigLoader that allows direct testing without depending on actual environment variables or files
2. Directly tests the individual methods of the ConfigLoader for proper functionality
3. Validates type conversion, default values, and the configuration hierarchy

This approach ensures that tests are reliable, deterministic, and not affected by the developer's local environment.

## Test Configuration

The project uses pytest for testing, with the following key configurations:

- **pytest.ini**: Main configuration file for pytest
- **pyproject.toml**: Contains pytest configuration in the `tool.pytest.ini_options` section
- **.coveragerc**: Configuration for test coverage reporting

## Running Tests

Use the test runner script to run the tests:

```bash
# Run all tests
python -m scripts.run_tests

# Run specific test types
python -m scripts.run_tests --unit
python -m scripts.run_tests --functional
python -m scripts.run_tests --integration
python -m scripts.run_tests --e2e

# Run tests with coverage
python -m scripts.run_tests --coverage
```

Or use pytest directly:

```bash
# Run all tests
pytest

# Run specific test types
pytest -m unit
pytest -m functional
pytest -m integration
pytest -m end_to_end

# Run tests with coverage
pytest --cov=src
```

## Test Suite Documentation

This directory contains tests for the Jira to OpenProject migration tool.

## Overview

The test suite uses `
