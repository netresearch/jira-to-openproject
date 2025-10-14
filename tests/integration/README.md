# Integration Tests

This directory contains integration tests for the Jira to OpenProject migration tool. Integration tests verify that our code interacts correctly with real external dependencies or services.

## Characteristics of Integration Tests

- Slower execution (seconds or minutes)
- Connects to actual external services where possible
- Tests integration with external APIs, databases, etc.
- Validates contract requirements with external services
- Reveals issues with configuration and connectivity

## Running Integration Tests

```bash
# Run all integration tests (Docker profile with mocks)
make container-test-integration

# Run a specific test file
pytest tests/integration/test_filename.py

# Skip tests requiring Docker
pytest tests/integration -k "not requires_docker"

# Skip tests requiring Rails
pytest tests/integration -k "not requires_rails"
```

## Writing Integration Tests

When adding new integration tests, please follow these guidelines:

1. Mark all integration tests with the `@pytest.mark.integration` decorator
2. Add relevant requirement markers if needed (e.g., `@pytest.mark.requires_docker`)
3. Write setup that connects to actual services when possible
4. Handle cleanup to avoid test pollution
5. Add markers for slow-running tests with `@pytest.mark.slow`
6. Consider environment variables for configuring service connections

Example test structure:

```python
import pytest
import os

@pytest.mark.integration
@pytest.mark.requires_docker
def test_docker_container_start():
    # Skip test if no Docker connection
    if not os.environ.get("DOCKER_HOST"):
        pytest.skip("No Docker connection available")

    # Arrange
    client = DockerClient()

    # Act
    container = client.start_container("postgres:13")

    # Assert
    assert container.status == "running"

    # Cleanup
    client.stop_container(container.id)
```

## Managing Test Data

For integration tests, use the test_data directory for:

- Fixtures for initial database state
- Sample API responses
- Configuration files for services

Always clean up after tests to avoid affecting other tests.
