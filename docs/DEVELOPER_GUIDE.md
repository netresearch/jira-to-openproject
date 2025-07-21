# Developer Guide

## Quick Testing Commands

```bash
# Quick unit tests for rapid feedback (~30s)
python scripts/test_helper.py quick

# Smoke tests for critical path validation (~2-3min)
python scripts/test_helper.py smoke

# Full test suite with coverage (~5-10min)
python scripts/test_helper.py full
```

## Test Organization

```
tests/
├── unit/          # Fast, isolated component tests
├── functional/    # Component interaction tests
├── integration/   # External service tests (mocked)
├── end_to_end/    # Complete workflow tests
├── utils/         # Shared testing utilities
└── test_data/     # Test fixtures
```

## Development Standards

### Exception-Based Error Handling

**Use exceptions, not return codes:**

```python
# ✅ DO: Raise exceptions for errors
def process_data(data: dict[str, Any]) -> dict[str, Any]:
    try:
        return perform_operation(data)
    except SomeSpecificError as e:
        raise RuntimeError(f"Data processing failed: {e}") from e

# ❌ DON'T: Return error status
def process_data(data: dict[str, Any]) -> dict[str, Any]:
    result = perform_operation(data)
    if result["status"] == "error":
        return {"status": "error", "message": "Failed"}
    return result
```

### Optimistic Execution

**Execute first, validate in exception handlers:**

```python
# ✅ DO: Optimistic execution
def copy_file(source: str, target: str) -> None:
    try:
        shutil.copy2(source, target)
    except Exception as e:
        # Diagnostics only on failure
        diagnostics = {
            "source_exists": os.path.exists(source),
            "target_dir_writable": os.access(os.path.dirname(target), os.W_OK)
        }
        raise FileOperationError(f"Copy failed: {source} → {target}", diagnostics) from e

# ❌ DON'T: Excessive precondition checking
def copy_file(source: str, target: str) -> None:
    if not os.path.exists(source):
        raise FileNotFoundError(f"Source does not exist: {source}")
    if not os.access(os.path.dirname(target), os.W_OK):
        raise PermissionError(f"Cannot write to: {target}")
    # Finally do the actual work...
```

### Modern Python Typing

**Use built-in types (Python 3.9+):**

```python
# ✅ DO: Built-in types
def process_items(items: list[str], config: dict[str, int]) -> tuple[bool, list[str]]:
    pass

# ✅ DO: Union types with pipe operator (Python 3.10+)
def get_user(user_id: int) -> User | None:
    pass

# ❌ DON'T: Legacy typing imports
from typing import Dict, List, Optional, Union
def process_items(items: List[str], config: Dict[str, int]) -> Optional[bool]:
    pass
```

### YOLO Development Approach

**No legacy code or backward compatibility:**

- Remove deprecated components entirely
- No migration guides or backward compatibility layers
- Clean, direct implementations without transitional patterns
- Focus on current functionality only

## Component Verification

### Quick Compliance Check

```bash
# Type checking
mypy src/migrations/{component}_migration.py

# Run component tests
pytest tests/functional/test_{component}_migration.py -v

# Security validation (for user input processing)
pytest tests/unit/test_security_validation.py -k {component}
```

### Verification Criteria

1. **Exception Handling**: All errors use exceptions, not return codes
2. **Type Annotations**: Proper modern Python typing throughout
3. **Optimistic Execution**: Operations attempted first, validation in handlers
4. **Test Coverage**: Unit and functional tests for all public methods
5. **Security**: Input validation for user-provided data

### Common Issues to Fix

- **Status dictionaries**: Replace with exceptions
- **Legacy typing imports**: Use built-in types
- **Precondition checking**: Move to exception handlers
- **Return codes**: Convert to exception-based flow

## Architecture Components

### Client Layer Hierarchy

```
OpenProjectClient (Orchestration)
    ├── SSHClient (Foundation)
    ├── DockerClient (Container ops, uses SSHClient)
    └── RailsConsoleClient (Console interaction)
```

### Exception Hierarchy

```
Exception
├── SSHConnectionError, SSHCommandError, SSHFileTransferError
├── RailsConsoleError
│   ├── TmuxSessionError
│   ├── ConsoleNotReadyError
│   └── CommandExecutionError
└── OpenProjectError
    ├── ConnectionError
    ├── QueryExecutionError
    ├── RecordNotFoundError
    └── JsonParseError
```

## Security Requirements

### Input Validation

All user-provided data (especially Jira keys) must be validated:

```python
def _validate_jira_key(jira_key: str) -> None:
    """Validate Jira key format to prevent injection attacks."""
    if not jira_key or not isinstance(jira_key, str):
        raise ValueError("Jira key must be a non-empty string")
    
    if len(jira_key) > 100:
        raise ValueError("Jira key too long (max 100 characters)")
    
    if not re.match(r'^[A-Z0-9\-]+$', jira_key):
        raise ValueError(f"Invalid Jira key format: {jira_key}")
```

### Output Escaping

Use proper escaping for dynamic content:

```python
# ✅ DO: Safe string formatting
f"jira_key: {jira_key.inspect}"  # Ruby's inspect method

# ❌ DON'T: Direct interpolation
f"jira_key: '{jira_key}'"  # Vulnerable to injection
```

## Performance Guidelines

### Test Performance Targets

- **Unit tests**: Complete in under 30 seconds
- **Functional tests**: Complete in under 2-3 minutes  
- **Full suite**: Complete in under 10 minutes

### Optimization Strategies

- Use appropriate test markers (`@pytest.mark.unit`, `@pytest.mark.slow`)
- Mock external dependencies in integration tests
- Use test data generators for consistent fixtures
- Implement connection pooling for repeated operations

## Migration Development Workflow

1. **Write component migration class** following architecture patterns
2. **Implement exception-based error handling** throughout
3. **Add comprehensive tests** (unit + functional)
4. **Verify security** for any user input processing
5. **Run full test suite** to ensure integration
6. **Update documentation** for any new patterns or requirements

This guide replaces the previous compliance checklist and verification process documents with a streamlined, actionable developer reference. 