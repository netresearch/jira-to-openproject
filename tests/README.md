# Tests Directory (`tests/`)

This directory contains test modules for the Jira to OpenProject migration tool.

## Testing Strategy

The testing approach for this project combines unit tests, integration tests, and manual validation:

1. **Unit Tests**: Test individual components in isolation using mocks for external dependencies
2. **Integration Tests**: Test the interaction between components and with actual APIs in test environments
3. **Manual Validation**: Perform data validation and quality checks by comparing data between systems

## Test Modules

- **`test_environment.py`**: Verifies that the Python environment is correctly set up with all required dependencies.
- **`test_user_migration.py`**: Tests the user migration component (`src/migrations/user_migration.py`), validating the extraction, mapping, and creation of users.

## Test Coverage Roadmap

The following test modules should be implemented to achieve comprehensive test coverage:

- **API Clients**:
  - Test Jira client connection and data retrieval
  - Test OpenProject client connection and entity creation
  - Test Rails client connection and command execution

- **Migration Components**:
  - Test each migration component (similar to `test_user_migration.py`)
  - Test custom field mapping and creation
  - Test work package type mapping and creation
  - Test status and workflow mapping
  - Test work package migration with attachment and comment handling

- **Configuration & Utils**:
  - Test configuration loading mechanism
  - Test utility functions

## Running Tests

You can run tests using the provided test runner script:

```bash
# Run all tests
python scripts/run_tests.py

# Run a specific test file
python scripts/run_tests.py --pattern test_user_migration.py

# Run tests with verbose output
python scripts/run_tests.py --verbose
```

### Running Individual Tests

To run a specific test case or test method:

```bash
# Run a specific test method
python scripts/run_tests.py --pattern tests/test_user_migration.py:TestUserMigration.test_extract_data
```

## Test Configuration

Tests use a mock configuration for testing purposes. No real API calls are made during unit testing, as external dependencies are mocked.

## Adding New Tests

When adding new tests:

1. Create a new test file following the naming convention `test_*.py`
2. Implement test cases using the `unittest` framework
3. Use mocks (`unittest.mock`) to isolate the component being tested
4. Add appropriate assertions to validate expected behavior
5. Update the TASKS.md file to mark testing tasks as completed
6. Update this README file to document the new test module

## Test Data

Test fixtures and sample data are defined within each test module as needed.
