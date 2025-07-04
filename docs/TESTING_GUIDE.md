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
