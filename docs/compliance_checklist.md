# Migration Component Compliance Checklist

This document provides a standardized checklist for ensuring all migration components comply with the project's development rules and architecture requirements.

## Overview

All migration components must follow these development principles:
- **YOLO Development Approach**: No legacy code or backward compatibility layers
- **Exception-based Error Handling**: Use exceptions instead of return codes or status dictionaries
- **Optimistic Execution**: Perform operations first, validate in exception handlers
- **Modern Python Typing**: Use built-in types and proper type annotations

## Pre-Verification Setup

### 1. Test Environment Preparation
- [ ] Ensure test environment is properly configured
- [ ] Verify all dependencies are installed and up to date
- [ ] Check that test data and fixtures are available
- [ ] Confirm test database connections are working

### 2. Component Identification
- [ ] Identify the target migration component file(s)
- [ ] Locate corresponding test files
- [ ] Verify component dependencies and imports
- [ ] Check if component has integration points with clients

## Core Compliance Areas

### A. Exception-based Error Handling

#### ✅ Compliant Patterns
```python
# ✅ DO: Raise exceptions for error conditions
def process_data(data: dict[str, Any]) -> dict[str, Any]:
    try:
        result = perform_operation(data)
        return result
    except SomeSpecificError as e:
        logger.exception("Failed to process data: %s", e)
        raise RuntimeError(f"Data processing failed: {e}") from e

# ✅ DO: Use exception chaining
def validate_input(value: str) -> int:
    try:
        return int(value)
    except ValueError as e:
        raise ValueError(f"Invalid integer value: {value}") from e
```

#### ❌ Non-compliant Patterns
```python
# ❌ DON'T: Return error codes or status dictionaries
def bad_method(data: dict) -> dict | None:
    try:
        result = process(data)
        return result
    except Exception:
        return None  # Should raise exception instead

# ❌ DON'T: Return success/failure dictionaries
def bad_method2(data: dict) -> dict[str, Any]:
    try:
        result = process(data)
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}  # Should raise exception
```

#### Verification Checklist
- [ ] All methods raise exceptions for error conditions (no `return None` or error objects)
- [ ] Exception chaining is used with `from e` syntax
- [ ] Custom exceptions are used when appropriate
- [ ] Error messages include sufficient context
- [ ] No methods return status dictionaries (`{"success": bool, ...}`)

### B. YOLO Development Approach

#### ✅ Compliant Patterns
```python
# ✅ DO: Direct implementation without compatibility layers
def migrate_data(source_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [transform_item(item) for item in source_data]

# ✅ DO: Clean, focused implementations
class DataMigration:
    def __init__(self, client: Client) -> None:
        self.client = client

    def run(self) -> None:
        data = self.client.fetch_data()
        transformed = self.transform(data)
        self.client.save_data(transformed)
```

#### ❌ Non-compliant Patterns
```python
# ❌ DON'T: Legacy compatibility code
def bad_migrate(data: Any, legacy_mode: bool = False) -> Any:
    if legacy_mode:
        return legacy_transform(data)  # Remove legacy code
    return new_transform(data)

# ❌ DON'T: Backward compatibility layers
class BadMigration:
    def __init__(self, client: Client, use_old_api: bool = False):
        self.client = client
        self.use_old_api = use_old_api  # Remove compatibility flags
```

#### Verification Checklist
- [ ] No legacy code or compatibility layers remain
- [ ] No conditional logic for backward compatibility
- [ ] No deprecated methods or parameters
- [ ] Implementation is direct and focused
- [ ] No migration guides or compatibility documentation references

### C. Optimistic Execution Pattern

#### ✅ Compliant Patterns
```python
# ✅ DO: Perform operations directly, handle errors in exceptions
def copy_file(source: str, target: str) -> None:
    try:
        shutil.copy2(source, target)
    except Exception as e:
        # Only perform diagnostics when operation fails
        diagnostics = {
            "source_exists": os.path.exists(source),
            "target_dir_exists": os.path.exists(os.path.dirname(target)),
            "permissions": oct(os.stat(source).st_mode) if os.path.exists(source) else None
        }
        raise FileOperationError(f"Failed to copy {source} to {target}", diagnostics) from e
```

#### ❌ Non-compliant Patterns
```python
# ❌ DON'T: Excessive precondition checking
def bad_copy_file(source: str, target: str) -> None:
    if not os.path.exists(source):
        raise FileNotFoundError(f"Source file does not exist: {source}")
    if not os.access(source, os.R_OK):
        raise PermissionError(f"No read permission for source: {source}")
    if os.path.exists(target):
        raise FileExistsError(f"Target file already exists: {target}")
    # Finally perform the operation...
    shutil.copy2(source, target)
```

#### Verification Checklist
- [ ] Operations are performed directly without extensive precondition checking
- [ ] Validation logic is inside exception handlers, not before operations
- [ ] Error diagnostics are collected only when operations fail
- [ ] Code follows "happy path" programming approach
- [ ] Exception handlers provide detailed context for debugging

### D. Modern Python Typing

#### ✅ Compliant Patterns
```python
# ✅ DO: Use built-in types (Python 3.9+)
from collections.abc import Callable, Sequence, Mapping

def process_items(
    items: list[str],
    config: dict[str, int],
    callback: Callable[[str], bool]
) -> tuple[bool, list[str]]:
    pass

# ✅ DO: Use pipe operator for unions (Python 3.10+)
def get_data(user_id: int) -> dict[str, Any] | None:
    pass

# ✅ DO: Proper generic typing
class DataProcessor[T]:
    def process(self, data: T) -> T:
        return data
```

#### ❌ Non-compliant Patterns
```python
# ❌ DON'T: Use typing module for built-in types
from typing import Dict, List, Optional, Union

def bad_process(items: List[str], config: Dict[str, int]) -> Optional[List[str]]:
    pass

# ❌ DON'T: Missing type annotations
def bad_method(data):  # Missing type hints
    return data
```

#### Verification Checklist
- [ ] All functions and methods have proper type annotations
- [ ] Built-in types are used instead of `typing` imports (`list` vs `List`)
- [ ] Union types use pipe operator (`int | str` vs `Union[int, str]`)
- [ ] Optional types use pipe syntax (`T | None` vs `Optional[T]`)
- [ ] Collections.abc imports are used for abstract base classes
- [ ] No missing type annotations on public methods

### E. Client Architecture Integration

#### ✅ Compliant Patterns
```python
# ✅ DO: Proper dependency injection
class ComponentMigration:
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        self.jira_client = jira_client
        self.op_client = op_client

# ✅ DO: Use client methods directly
def extract_data(self) -> list[dict[str, Any]]:
    return self.jira_client.get_issues()  # Let client handle errors
```

#### ❌ Non-compliant Patterns
```python
# ❌ DON'T: Create clients internally
class BadMigration:
    def __init__(self):
        self.client = JiraClient()  # Should be injected

# ❌ DON'T: Wrap client calls unnecessarily
def bad_extract(self) -> list[dict[str, Any]] | None:
    try:
        return self.client.get_issues()
    except Exception:
        return None  # Let exceptions propagate
```

#### Verification Checklist
- [ ] Clients are injected via constructor dependency injection
- [ ] No internal client instantiation within migration components
- [ ] Client method calls are made directly without unnecessary wrapping
- [ ] Client exceptions are allowed to propagate naturally
- [ ] No redundant error handling around client calls

## Testing Requirements

### Test Coverage Verification
- [ ] All public methods have corresponding tests
- [ ] Error paths and exception handling are tested
- [ ] Integration with client dependencies is tested
- [ ] Mock objects are used appropriately for client dependencies

### Test Execution
- [ ] All existing tests pass before starting compliance review
- [ ] All tests continue to pass after compliance fixes
- [ ] Test execution time remains reasonable
- [ ] No test flakiness introduced by changes

## Verification Process

### Step 1: Initial Assessment
1. [ ] Run targeted tests for the component
2. [ ] Review component source code for obvious violations
3. [ ] Check imports and dependencies
4. [ ] Identify methods that need detailed review

### Step 2: Detailed Code Review
1. [ ] Review each public method against compliance checklist
2. [ ] Check error handling patterns in all methods
3. [ ] Verify type annotations are complete and modern
4. [ ] Ensure no legacy or compatibility code remains

### Step 3: Fix Implementation
1. [ ] Address each compliance violation found
2. [ ] Maintain existing functionality while fixing violations
3. [ ] Update method signatures and return types as needed
4. [ ] Add proper exception handling and chaining

### Step 4: Validation
1. [ ] Re-run all tests to ensure functionality is preserved
2. [ ] Verify fixes address the identified violations
3. [ ] Check that no new violations were introduced
4. [ ] Document changes made and their impact

### Step 5: Documentation
1. [ ] Update method docstrings to reflect new exception behavior
2. [ ] Document any breaking changes in component behavior
3. [ ] Update integration documentation if needed
4. [ ] Record compliance status in tracking system

## Common Violation Patterns

### Return-based Error Handling
**Pattern:** Methods returning `None`, empty lists, or error dictionaries instead of raising exceptions.

**Fix:** Replace return-based errors with appropriate exception types.

### Legacy Compatibility Code
**Pattern:** Conditional logic, deprecated parameters, or old API support.

**Fix:** Remove all legacy code paths and compatibility layers.

### Excessive Precondition Checking
**Pattern:** Extensive validation before performing operations.

**Fix:** Move validation into exception handlers, perform operations optimistically.

### Outdated Type Annotations
**Pattern:** Using `typing` module imports for built-in types.

**Fix:** Update to use built-in types and modern union syntax.

## Compliance Status Tracking

### Component Status Levels
- 🔴 **Non-compliant**: Multiple violations identified, fixes needed
- 🟡 **Partially compliant**: Minor violations, targeted fixes needed
- 🟢 **Fully compliant**: Passes all checklist items, no violations found

### Required Documentation
For each component review:
1. List of violations found
2. Fixes implemented
3. Test results before and after
4. Current compliance status
5. Date of last verification

## Conclusion

This checklist ensures consistent evaluation of migration components against project standards. All components must achieve full compliance before being considered production-ready.

Regular re-verification is recommended when:
- Component code is significantly modified
- Project development rules are updated
- New team members join the project
- Before major releases or deployments
