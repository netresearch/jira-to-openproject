# Functional Tests

This directory contains functional tests for the Jira to OpenProject migration tool. Functional tests verify that components work correctly when interacting with each other, but with external dependencies still mocked.

## Characteristics of Functional Tests

- Medium speed execution (milliseconds to seconds)
- External dependencies are mocked or stubbed
- Tests interaction between multiple components
- Tests complete features or workflows
- Focuses on business logic correctness

## Running Functional Tests

```bash
# Run all functional tests
pytest tests/functional

# Run a specific test file
pytest tests/functional/test_filename.py

# Run with verbose output
pytest -v tests/functional
```

## Writing Functional Tests

When adding new functional tests, please follow these guidelines:

1. Mark all functional tests with the `@pytest.mark.functional` decorator
2. Use fixtures from the top-level `conftest.py` when possible
3. Mock external services (databases, APIs, etc.)
4. Test complete user stories or features
5. Consider error handling and edge cases

Example test structure:

```python
import pytest

@pytest.mark.functional
def test_workflow_process(mock_jira_client, mock_op_client):
    # Arrange
    migration = MigrationProcess(
        jira_client=mock_jira_client,
        op_client=mock_op_client
    )
    mock_jira_client.get_projects.return_value = [{"key": "TEST", "name": "Test Project"}]

    # Act
    result = migration.migrate_projects()

    # Assert
    assert result.success is True
    assert len(result.migrated_projects) == 1
    mock_op_client.create_project.assert_called_once()
```
