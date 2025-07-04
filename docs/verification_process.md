# Migration Component Verification Process

This document outlines the standardized process for conducting compliance verification of migration components, ensuring consistent evaluation across all components in the project.

## Process Overview

The verification process consists of five main phases:
1. **Preparation**: Set up environment and identify scope
2. **Assessment**: Run tests and review code
3. **Analysis**: Identify violations and plan fixes
4. **Implementation**: Apply fixes and validate changes
5. **Documentation**: Record results and update tracking

## Phase 1: Preparation

### Environment Setup
```bash
# Ensure clean test environment
pytest --cache-clear
source .venv/bin/activate
pip install -e .

# Verify all dependencies are current
pip check
```

### Component Identification
1. **Locate target component**: `src/migrations/{component_name}_migration.py`
2. **Find test files**: `tests/functional/test_{component_name}_migration.py`
3. **Check dependencies**: Review imports and client usage
4. **Identify scope**: Determine which methods need verification

### Pre-verification Test Run
```bash
# Run component-specific tests to establish baseline
pytest tests/functional/test_{component_name}_migration.py -v --log-level=DEBUG
```

**Expected Result**: All tests should pass before starting compliance review.

## Phase 2: Assessment

### Code Review Methodology

#### 2.1 Automated Analysis
```bash
# Type checking
mypy src/migrations/{component_name}_migration.py

# Code style and pattern analysis
ruff check src/migrations/{component_name}_migration.py
```

#### 2.2 Manual Code Review
Review the component against each compliance area:

**Exception-based Error Handling:**
- [ ] Scan for methods returning `None` or empty collections on errors
- [ ] Look for `return {"success": False, ...}` patterns
- [ ] Check exception chaining (`from e`) usage
- [ ] Verify meaningful error messages

**YOLO Development Approach:**
- [ ] Search for legacy compatibility parameters
- [ ] Check for conditional logic supporting old behavior
- [ ] Look for deprecated method calls or patterns
- [ ] Verify no backward compatibility documentation

**Optimistic Execution:**
- [ ] Identify excessive precondition checking
- [ ] Look for validation before operations
- [ ] Check if diagnostics are performed upfront vs. in exception handlers
- [ ] Verify operations are performed directly

**Modern Python Typing:**
- [ ] Check for `typing` module imports that should use built-ins
- [ ] Verify union types use pipe operator (`|`)
- [ ] Ensure all public methods have type annotations
- [ ] Check collections.abc usage for abstract types

**Client Architecture Integration:**
- [ ] Verify dependency injection in constructor
- [ ] Check for internal client instantiation
- [ ] Look for unnecessary client method wrapping
- [ ] Ensure client exceptions propagate naturally

## Phase 3: Analysis

### Violation Prioritization

**High Priority (Fix Required):**
- Methods returning error objects instead of raising exceptions
- Legacy compatibility code or parameters
- Missing type annotations on public methods
- Internal client instantiation

**Medium Priority (Should Fix):**
- Outdated typing imports (using `typing` vs built-ins)
- Excessive precondition checking
- Inconsistent exception chaining

**Low Priority (Nice to Have):**
- Verbose error messages that could be more concise
- Minor type annotation improvements

## Phase 4: Implementation

### Fix Implementation Guidelines

#### 4.1 Exception-based Error Handling Fixes
```python
# Before (non-compliant)
def process_data(data: dict) -> dict | None:
    try:
        result = perform_operation(data)
        return result
    except Exception as e:
        logger.error(f"Error: {e}")
        return None

# After (compliant)
def process_data(data: dict[str, Any]) -> dict[str, Any]:
    try:
        result = perform_operation(data)
        return result
    except Exception as e:
        logger.exception("Failed to process data: %s", e)
        raise RuntimeError(f"Data processing failed: {e}") from e
```

#### 4.2 YOLO Development Fixes
```python
# Before (non-compliant)
def migrate_items(items: list, use_legacy: bool = False) -> list:
    if use_legacy:
        return legacy_migrate(items)
    return new_migrate(items)

# After (compliant)
def migrate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return new_migrate(items)
```

#### 4.3 Optimistic Execution Fixes
```python
# Before (non-compliant)
def save_file(content: str, path: str) -> None:
    if not content:
        raise ValueError("Content cannot be empty")
    if not os.path.exists(os.path.dirname(path)):
        raise FileNotFoundError("Directory does not exist")
    # Finally perform operation
    with open(path, 'w') as f:
        f.write(content)

# After (compliant)
def save_file(content: str, path: str) -> None:
    try:
        with open(path, 'w') as f:
            f.write(content)
    except Exception as e:
        diagnostics = {
            "content_length": len(content) if content else 0,
            "directory_exists": os.path.exists(os.path.dirname(path)),
        }
        raise FileOperationError(f"Failed to save file: {path}", diagnostics) from e
```

#### 4.4 Modern Python Typing Fixes
```python
# Before (non-compliant)
from typing import Dict, List, Optional, Union

def process(items: List[str], config: Dict[str, int]) -> Optional[List[str]]:
    pass

# After (compliant)
from collections.abc import Sequence

def process(items: list[str], config: dict[str, int]) -> list[str] | None:
    pass
```

## Phase 5: Documentation

### Update Component Documentation

Update docstrings to reflect new exception behavior:

```python
def extract_data(self) -> list[dict[str, Any]]:
    """Extract data from source system.
    
    Returns:
        List of extracted data items
        
    Raises:
        RuntimeError: When data extraction fails
        ConnectionError: When source system is unreachable
        
    """
```

### Compliance Verification Record

Create or update the compliance tracking record:

```markdown
## {component_name}_migration Compliance Status

**Last Verified**: {date}  
**Reviewer**: {reviewer_name}  
**Status**: 🟢 Fully Compliant  

### Violations Fixed
1. **Return-based Error Handling** (3 methods)
   - `method_a()`: Now raises RuntimeError instead of returning None
   - `method_b()`: Now raises ValueError instead of returning empty dict

### Test Results
- **Tests Run**: 7
- **Tests Passed**: 7  
- **Coverage**: 95%
- **Execution Time**: 0.23s

### Next Review Due**: {date + 6 months}
```

## Quality Assurance

### Final Validation Checklist

Before marking a component as compliant:

- [ ] All compliance violations have been addressed
- [ ] All existing tests continue to pass
- [ ] New tests added for modified error behavior (if needed)
- [ ] Documentation updated to reflect changes
- [ ] No new violations introduced during fixes
- [ ] Code review by second team member completed
- [ ] Breaking changes properly documented

This verification process ensures consistent, thorough evaluation of all migration components while maintaining high code quality and adherence to project standards.
