# Developer Testing Guide

This guide provides comprehensive information about testing in the Jira to OpenProject migration tool, including workflows, tools, and best practices for developers.

## Quick Start

For immediate testing, use the enhanced test helper:

```bash
# Quick unit tests for rapid feedback (~30s)
python scripts/test_helper.py quick

# Smoke tests for critical path validation (~2-3min)
python scripts/test_helper.py smoke

# Full test suite with coverage (~5-10min)
python scripts/test_helper.py full
```

## Testing Infrastructure Overview

### Test Organization

The project uses a structured testing approach with clear separation of concerns:

```
tests/
├── unit/          # Fast, isolated tests for individual components
├── functional/    # Tests for component interactions
├── integration/   # Tests with external services (mocked)
├── end_to_end/    # Complete workflow tests
├── utils/         # Shared testing utilities and data generators
├── examples/      # Example tests demonstrating patterns
└── test_data/     # Test data files and fixtures
```

### Test Types and Markers

Tests are classified using pytest markers:

- `@pytest.mark.unit` - Unit tests (fast, isolated)
- `@pytest.mark.functional` - Functional tests (component interactions)
- `@pytest.mark.integration` - Integration tests (external services)
- `@pytest.mark.end_to_end` - End-to-end tests (complete workflows)
- `@pytest.mark.slow` - Tests that take longer to run
- `@pytest.mark.requires_docker` - Tests requiring Docker
- `@pytest.mark.requires_ssh` - Tests requiring SSH connection
- `@pytest.mark.requires_rails` - Tests requiring Rails console

## Developer Workflow

### 1. Pre-Development Testing

Before starting development, ensure your environment is set up:

```bash
# Set up test environment (run once)
python scripts/test_helper.py setup

# Run smoke tests to verify current state
python scripts/test_helper.py smoke
```

### 2. During Development

For rapid feedback during development:

```bash
# Quick unit tests (run frequently)
python scripts/test_helper.py quick

# Test specific module you're working on
python scripts/test_helper.py module config
python scripts/test_helper.py module migration

# Re-run only failed tests from last run
python scripts/test_helper.py failed
```

### 3. Before Committing

Use pre-commit hooks (automatically installed) or run manually:

```bash
# Run tests for changed files
python scripts/test_helper.py changed

# Run comprehensive tests
python scripts/test_helper.py full

# Clean up artifacts
python scripts/test_helper.py clean
```

### 4. Pre-Push Testing

Before pushing to remote:

```bash
# Performance tests
python scripts/test_helper.py perf

# Full suite with coverage
python scripts/test_helper.py full
```

## Testing Tools and Commands

### Enhanced Test Helper (scripts/test_helper.py)

Provides convenient shortcuts and better developer experience:

| Command | Purpose | Duration | When to Use |
|---------|---------|----------|-------------|
| quick | Fast unit tests only | ~30s | During development |
| smoke | Critical path tests | ~2-3min | Before commits |
| full | Complete suite + coverage | ~5-10min | Before push |
| module <name> | Tests for specific module | Variable | Focused testing |
| failed | Re-run failed tests | Variable | After fixing issues |
| changed | Tests for changed files | Variable | Git-aware testing |
| perf | Performance tests | ~5min | Performance validation |
| setup | Environment setup | ~1min | Initial setup |
| clean | Clean artifacts | ~30s | Maintenance |

### Traditional Test Runner (scripts/run_tests.py)

More flexible but verbose pytest wrapper.

## Best Practices

### Test Writing

1. **Keep tests focused** - One concept per test
2. **Use descriptive names** - Test name should explain what is being tested
3. **Avoid test interdependencies** - Tests should be runnable in any order
4. **Use appropriate markers** - Mark tests with proper categories
5. **Mock external dependencies** - Don't rely on external services in tests

### Performance

1. **Fast feedback** - Unit tests should run in seconds
2. **Parallel execution** - Use pytest-xdist for parallel testing
3. **Resource cleanup** - Always clean up resources in teardown
4. **Selective testing** - Use markers to run only relevant tests

### Maintainability

1. **Use test utilities** - Leverage shared utilities and data generators
2. **Keep test data realistic** - Use realistic but minimal test data
3. **Document complex tests** - Add comments for complex test logic
4. **Regular cleanup** - Remove obsolete tests and update assertions

# Using Monkeypatch Helpers

The project includes standardized helper functions for common monkeypatch patterns to improve test isolation and cleanup. Always prefer monkeypatch over direct mock assignments.

## Available Helper Functions

### Basic Method Mocking

```python
def test_method_mocking(monkeypatch: pytest.MonkeyPatch, monkeypatch_helpers):
    """Example of method mocking with monkeypatch helpers."""
    mock_client = MagicMock(spec=JiraClient)

    # ✅ DO: Use monkeypatch helpers for return values
    monkeypatch_helpers.mock_method_return_value(
        monkeypatch, mock_client, "get_users", [{"id": 1, "name": "test"}]
    )

    # ✅ DO: Use monkeypatch helpers for side effects
    monkeypatch_helpers.mock_method_side_effect(
        monkeypatch, mock_client, "create_user", Exception("Creation failed")
    )

    # ❌ DON'T: Direct mock assignments
    # mock_client.get_users.return_value = [{"id": 1}]
    # mock_client.create_user.side_effect = Exception("Failed")
```

### Class Constructor Mocking

```python
def test_class_mocking(monkeypatch: pytest.MonkeyPatch, monkeypatch_helpers):
    """Example of class constructor mocking."""
    mock_jira_instance = MagicMock(spec=JiraClient)

    # ✅ DO: Use monkeypatch helpers for class constructors
    monkeypatch_helpers.mock_class_return_value(
        monkeypatch, "src.migrations.user_migration", "JiraClient", mock_jira_instance
    )

    # ❌ DON'T: Use @patch decorators for class-based tests
    # @patch("src.migrations.user_migration.JiraClient")
```

### File System Operations

```python
def test_file_operations(monkeypatch: pytest.MonkeyPatch, monkeypatch_helpers):
    """Example of file system mocking."""
    # ✅ DO: Use helpers for path existence
    monkeypatch_helpers.mock_path_exists(monkeypatch, return_value=True)

    # ✅ DO: Use helpers for file opening
    mock_file = monkeypatch_helpers.mock_path_open(
        monkeypatch, read_data='{"test": "data"}'
    )

    # ❌ DON'T: Direct patching of builtins
    # monkeypatch.setattr("builtins.open", mock_open(read_data="data"))
```

### Configuration Mocking

```python
def test_config_mocking(monkeypatch: pytest.MonkeyPatch, monkeypatch_helpers):
    """Example of configuration mocking."""
    # ✅ DO: Use helpers for config values
    monkeypatch_helpers.mock_config_get(monkeypatch, {
        "dry_run": True,
        "force": False,
        "batch_size": 100
    })

    # ❌ DON'T: Complex side_effect functions
    # def config_side_effect(key, default=None):
    #     return {"dry_run": True}.get(key, default)
    # monkeypatch.setattr("config.get", MagicMock(side_effect=config_side_effect))
```

### JSON Operations

```python
def test_json_operations(monkeypatch: pytest.MonkeyPatch, monkeypatch_helpers):
    """Example of JSON operation mocking."""
    test_data = {"users": [{"id": 1, "name": "test"}]}

    # ✅ DO: Use helpers for JSON operations
    monkeypatch_helpers.mock_json_operations(
        monkeypatch,
        load_data=test_data,
        dump_data=test_data
    )

    # ❌ DON'T: Manual JSON mocking
    # monkeypatch.setattr("json.load", MagicMock(return_value=test_data))
```

## Migration from Direct Mock Assignments

### Before (Direct Assignments)

```python
class TestExample(unittest.TestCase):
    @patch("src.module.SomeClass")
    def test_something(self, mock_class):
        # Direct assignments
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        mock_instance.method.return_value = "result"
        mock_instance.other_method.side_effect = Exception("error")

        # Test logic here
```

### After (Monkeypatch Helpers)

```python
class TestExample:
    def test_something(self, monkeypatch: pytest.MonkeyPatch, monkeypatch_helpers):
        # Clean monkeypatch setup
        mock_instance = MagicMock(spec=SomeClass)
        monkeypatch_helpers.mock_class_return_value(
            monkeypatch, "src.module", "SomeClass", mock_instance
        )
        monkeypatch_helpers.mock_method_return_value(
            monkeypatch, mock_instance, "method", "result"
        )
        monkeypatch_helpers.mock_method_side_effect(
            monkeypatch, mock_instance, "other_method", Exception("error")
        )

        # Test logic here
```

## Benefits of Monkeypatch Approach

1. **Better Isolation**: Each test gets a clean environment
2. **Automatic Cleanup**: No need to manually restore mocks
3. **Clearer Intent**: Helper functions make mocking patterns explicit
4. **Type Safety**: Better integration with type checking
5. **Consistent Patterns**: Standardized approach across all tests

## Common Patterns

### Testing Migration Classes

```python
def test_migration_component(monkeypatch: pytest.MonkeyPatch, monkeypatch_helpers):
    """Standard pattern for testing migration components."""
    # Setup mock clients
    mock_jira = MagicMock(spec=JiraClient)
    mock_op = MagicMock(spec=OpenProjectClient)

    # Mock data responses
    monkeypatch_helpers.mock_method_return_value(
        monkeypatch, mock_jira, "get_users", [{"key": "user1"}]
    )
    monkeypatch_helpers.mock_method_return_value(
        monkeypatch, mock_op, "get_users", [{"id": 1, "login": "user1"}]
    )

    # Mock configuration
    monkeypatch_helpers.mock_config_get(monkeypatch, {"dry_run": False})

    # Mock file operations
    monkeypatch_helpers.mock_path_exists(monkeypatch, True)

    # Test the migration
    migration = UserMigration(mock_jira, mock_op)
    result = migration.run()

    assert result.success
```

### Testing Error Scenarios

```python
def test_error_handling(monkeypatch: pytest.MonkeyPatch, monkeypatch_helpers):
    """Pattern for testing error scenarios."""
    mock_client = MagicMock(spec=JiraClient)

    # Setup error conditions
    monkeypatch_helpers.mock_method_side_effect(
        monkeypatch, mock_client, "get_users", ConnectionError("Network failed")
    )

    migration = UserMigration(mock_client, MagicMock())

    # Verify error handling
    with pytest.raises(ConnectionError):
        migration.extract_jira_users()
```

## Best Practices

1. **Use Type Specifications**: Always specify `spec=ClassName` for mocks
2. **Prefer Helpers**: Use helper functions over direct monkeypatch calls
3. **One Pattern Per Test**: Don't mix direct assignments with monkeypatch
4. **Clean Setup**: Group all mocking setup at the beginning of tests
5. **Meaningful Names**: Use descriptive variable names for mocks

## Common Mistakes to Avoid

```python
# ❌ DON'T: Mix unittest.TestCase with monkeypatch
class TestExample(unittest.TestCase):
    def test_with_monkeypatch(self, monkeypatch):  # Won't work!
        pass

# ✅ DO: Use plain class for pytest-style tests
class TestExample:
    def test_with_monkeypatch(self, monkeypatch):
        pass

# ❌ DON'T: Direct mock assignments in monkeypatch tests
def test_bad_pattern(monkeypatch):
    mock_obj = MagicMock()
    mock_obj.method.return_value = "value"  # Direct assignment

# ✅ DO: Use monkeypatch helpers
def test_good_pattern(monkeypatch, monkeypatch_helpers):
    mock_obj = MagicMock()
    monkeypatch_helpers.mock_method_return_value(
        monkeypatch, mock_obj, "method", "value"
    )
```
