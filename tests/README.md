# Tests (`tests/`)

This directory contains automated tests for the Jira to OpenProject migration tool.

## Current Status

The test suite is currently **minimal** and requires significant expansion to ensure the reliability and correctness of the migration tool.

## Running Tests

Tests are executed using `pytest` from within the Docker development environment.

1.  **Ensure the Docker container is running:**
    ```bash
    docker compose up -d
    ```

2.  **Execute tests within the container:**
    ```bash
    # Run all tests
    docker exec -it j2o-app pytest

    # Run tests with verbose output
    docker exec -it j2o-app pytest -v

    # Run tests in a specific file
    docker exec -it j2o-app pytest tests/test_environment.py

    # Run a specific test function
    docker exec -it j2o-app pytest tests/test_environment.py::test_python_version
    ```

## Test Structure

*(Proposed structure - to be implemented)*

*   **Unit Tests:** Focus on testing individual functions, methods, or classes in isolation. Examples:
    *   Testing parsing logic in `utils.py`.
    *   Testing configuration loading in `config_loader.py`.
    *   Testing mapping functions.
    *   Mocking API clients to test migration component logic without external calls.
*   **Integration Tests:** Test the interaction between different components. Examples:
    *   Testing the connection logic in API clients (`test_connection.py`).
    *   Testing the Rails console client connection (`test_rails_connection.py`).
    *   Testing a migration component with mocked API responses.
*   **End-to-End (E2E) Tests:** Simulate a real migration scenario, potentially requiring live (but separate test instances) of Jira and OpenProject. These are more complex to set up and maintain.
    *   Could involve running `run_migration.py` with specific components and validating the results in the test OpenProject instance.
    *   Might use `src/cleanup_openproject.py` to reset the state between test runs.

## Key Areas for Test Expansion

*   **Migration Components (`src/migrations/`)**: Each component needs tests covering:
    *   Extraction logic (mocking Jira API).
    *   Mapping logic (various scenarios, edge cases).
    *   Loading logic (mocking OpenProject API/Rails client).
    *   Handling of different data variations (e.g., missing fields, different custom field types).
*   **API Clients (`src/clients/`)**: More robust connection tests, error handling tests, and tests for specific API call parsing.
*   **Rails Client (`src/clients/openproject_rails_client.py`)**: Tests for different command execution scenarios, prompt detection, and error handling.
*   **Configuration Loading (`src/config_loader.py`)**: Tests for priority rules, environment variable overrides, and handling of missing files/variables.
*   **Mapping Logic (`src/mappings/`)**: Tests for loading/saving mapping files and applying different mapping strategies.

## Contributing Tests

Please refer to the [Development Guide](../docs/development.md) for contribution guidelines. Adding tests, especially for existing untested functionality, is highly encouraged.
