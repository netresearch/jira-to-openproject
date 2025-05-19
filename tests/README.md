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

- `.env`: Base configuration for all environments (committed to git)
- `.env.local`: Local development overrides (git-ignored)
- `.env.test`: Default test configuration (committed to git)
- `.env.test.local`: Local test overrides (git-ignored)

### Loading Order

The environment files are loaded in order of increasing specificity, with later files overriding values from earlier files:

**In Development Mode:**

1. `.env` (base configuration)
2. `.env.local` (if present)

**In Test Mode:**

1. `.env` (base configuration)
2. `.env.local` (if present)
3. `.env.test` (test-specific configuration)
4. `.env.test.local` (if present)

Test mode is automatically detected when running under pytest.

### Using Environment Variables in Tests

The `test_env` fixture provides a way to override environment variables during tests:

```python
@pytest.mark.unit
def test_example(test_env: dict) -> None:
    # Override environment variables for this test only
    test_env["J2O_JIRA_URL"] = "https://test-jira.example.com"

    # Use the API that reads from environment
    # ...

    # Original environment is automatically restored after the test
```

For a complete example, see `examples/test_env_fixture_example.py`.

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
