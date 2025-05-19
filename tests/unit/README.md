# Unit Tests

This directory contains unit tests for the Jira to OpenProject migration tool. Unit tests focus on testing isolated components with mocked dependencies.

## Characteristics of Unit Tests

- Fast execution (milliseconds)
- No external dependencies (databases, servers, etc.)
- Tests a single unit of functionality
- All dependencies are mocked or stubbed
- Focus on code correctness, not integration

## Running Unit Tests

```bash
# Run all unit tests
pytest tests/unit

# Run a specific test file
pytest tests/unit/test_filename.py

# Run with verbose output
pytest -v tests/unit
```

## Writing Unit Tests

When adding new unit tests, please follow these guidelines:

1. Mark all unit tests with the `@pytest.mark.unit` decorator
2. Use fixtures from the top-level `conftest.py` when possible
3. Mock all external dependencies
4. Keep tests focused on a single functionality
5. Aim for high coverage of code paths within the unit

Example test structure:

```python
import pytest

@pytest.mark.unit
def test_function_behavior():
    # Arrange
    expected = "expected_result"
    input_value = "test_input"

    # Act
    result = function_under_test(input_value)

    # Assert
    assert result == expected
```
