# End-to-End Tests

This directory contains end-to-end tests for the Jira to OpenProject migration tool. End-to-end tests verify complete workflows from start to finish using real services whenever possible.

## Characteristics of End-to-End Tests

- Slowest execution (minutes)
- Tests complete migration flows from input to output
- Uses real services when possible (Docker containers or actual services)
- Verifies user-facing functionality works correctly
- Simulates real usage scenarios

## Running End-to-End Tests

```bash
# Run all end-to-end tests
pytest tests/end_to_end

# Run a specific test file
pytest tests/end_to_end/test_filename.py

# Run with Docker setup for services (if implemented)
DOCKER_E2E=1 pytest tests/end_to_end
```

## Writing End-to-End Tests

When adding new end-to-end tests, please follow these guidelines:

1. Mark all tests with the `@pytest.mark.end_to_end` decorator
2. Add additional markers for required infrastructure (`requires_docker`, etc.)
3. Set up complete environments with realistic data
4. Test the full migration workflow from preparation to completion
5. Clean up all resources after testing
6. Add appropriate timeouts for long-running operations

Example test structure:

```python
import pytest
import os
import time

@pytest.mark.end_to_end
@pytest.mark.requires_docker
def test_complete_project_migration(docker_environment):
    # Arrange - Set up test data in Jira instance
    jira_client = setup_jira_with_test_data()
    op_client = setup_openproject_instance()

    # Act - Run the complete migration
    migration = CompleteProjectMigration(
        jira_client=jira_client,
        op_client=op_client,
        config=MigrationConfig(project_key="TEST")
    )
    result = migration.run()

    # Wait for background jobs to complete
    time.sleep(5)

    # Assert - Verify all entities were properly migrated
    assert result.success is True
    assert result.migrated_projects == 1
    assert result.migrated_issues == 10

    # Verify data in OpenProject
    op_projects = op_client.get_projects()
    assert len(op_projects) == 1
    assert op_projects[0]["name"] == "Test Project"
```

## Test Data and Fixtures

The end-to-end tests require realistic test data. You can:

1. Use the fixtures in tests/test_data directory
2. Create Docker containers with pre-configured data
3. Set up test instances programmatically at the start of tests

## Containerized Testing Environment

We recommend using Docker Compose for setting up complete test environments with:
- Jira instance with test data
- OpenProject instance
- Any required supporting services

See the compose.yml file in the tests/end_to_end directory for details.
