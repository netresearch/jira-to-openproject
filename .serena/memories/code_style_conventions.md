# Code Style and Conventions

## Core Principles

### Exception-Based Error Handling
**Always raise exceptions, never return error codes or status dictionaries.**

```python
# ✅ CORRECT: Raise exceptions
def process_data(data: dict[str, Any]) -> dict[str, Any]:
    try:
        return perform_operation(data)
    except SomeSpecificError as e:
        raise RuntimeError(f"Data processing failed: {e}") from e

# ❌ WRONG: Return error status
def process_data(data: dict[str, Any]) -> dict[str, Any]:
    if result["status"] == "error":
        return {"status": "error", "message": "Failed"}
```

### Optimistic Execution
**Execute first, validate in exception handlers. No excessive precondition checking.**

```python
# ✅ CORRECT: Optimistic execution
def copy_file(source: str, target: str) -> None:
    try:
        shutil.copy2(source, target)
    except Exception as e:
        diagnostics = {
            "source_exists": os.path.exists(source),
            "target_dir_writable": os.access(os.path.dirname(target), os.W_OK)
        }
        raise FileOperationError(f"Copy failed: {source} → {target}", diagnostics) from e

# ❌ WRONG: Excessive precondition checking
def copy_file(source: str, target: str) -> None:
    if not os.path.exists(source):
        raise FileNotFoundError(...)
    if not os.access(...):
        raise PermissionError(...)
    # Finally do the work...
```

### Modern Python Typing
**Use built-in types (Python 3.9+) and pipe operators (Python 3.10+).**

```python
# ✅ CORRECT: Built-in types
def process_items(items: list[str], config: dict[str, int]) -> tuple[bool, list[str]]:
    pass

# ✅ CORRECT: Union with pipe operator
def get_user(user_id: int) -> User | None:
    pass

# ❌ WRONG: Legacy typing imports
from typing import Dict, List, Optional, Union
def process_items(items: List[str], config: Dict[str, int]) -> Optional[bool]:
    pass
```

**Use ABCs from collections.abc:**
```python
from collections.abc import Iterable, Mapping, Sequence
```

## Code Organization

### BaseMigration Pattern
All migration components inherit from `BaseMigration` and implement:
- `extract()`: Fetch data from Jira API
- `map()`: Transform Jira data to OpenProject format
- `load()`: Insert data via Rails console

### Logging
Use `structlog` for structured logging:
```python
self.logger.info("processing_batch", batch_size=len(items), component="users")
```

### Error Recovery
- **tenacity**: Retry with exponential backoff for transient failures
- **pybreaker**: Circuit breaker pattern for fault tolerance

### Rails Script Generation
Split scripts into parameterized head (f-string) and literal body block:
```python
# Head: interpolated parameters
head = f"""
results_file = '{results_path}'
data = JSON.parse(File.read('{json_path}'))
"""

# Body: literal Ruby code
body = """
data.each do |item|
  record = Model.create!(item)
  results << {id: record.id, status: 'created'}
end
"""

script = head + body
```

## Naming Conventions
- **Functions/methods**: `snake_case`
- **Classes**: `PascalCase`
- **Constants**: `UPPER_SNAKE_CASE`
- **Private methods**: `_leading_underscore`

## Line Length & Formatting
- **Line length**: 120 characters (configured in ruff)
- **Formatter**: ruff format (replaces black/isort)
- **Linter**: ruff (replaces flake8)

## Type Checking
- **Tool**: mypy (non-strict mode)
- **Currently scoped**: `src/config_loader.py` (gradually expanding)
- **Annotate new code fully** even though global strict mode is off

## Import Organization
```python
# Standard library
import os
from pathlib import Path

# Third-party
import requests
from pydantic import BaseModel

# Local
from src.clients.jira_client import JiraClient
from src.utils.validation import validate_key
```

## YOLO Development Approach
- **No legacy code**: Remove deprecated components immediately
- **No backward compatibility**: Clean, direct implementations only
- **No migration guides**: Focus on current functionality
- **No transitional patterns**: Modernize aggressively

## Security Requirements

### Input Validation
Always validate user-provided data (especially Jira keys):
```python
def _validate_jira_key(jira_key: str) -> None:
    if not jira_key or not isinstance(jira_key, str):
        raise ValueError("Jira key must be a non-empty string")
    if len(jira_key) > 100:
        raise ValueError("Jira key too long (max 100 characters)")
    if not re.match(r'^[A-Z0-9\-]+$', jira_key):
        raise ValueError(f"Invalid Jira key format: {jira_key}")
```

### Output Escaping
Use Ruby's `inspect` method for safe string formatting:
```python
# ✅ CORRECT: Safe escaping
f"jira_key: {jira_key.inspect}"

# ❌ WRONG: Direct interpolation (injection risk)
f"jira_key: '{jira_key}'"
```

## Common Patterns to Avoid
- ❌ Status dictionaries: Use exceptions
- ❌ Legacy typing imports: Use built-in types
- ❌ Precondition checking: Use optimistic execution
- ❌ Return codes: Use exception-based flow
