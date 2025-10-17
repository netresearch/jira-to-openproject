# Developer Guide

## Quick Testing Commands

```bash
# Quick unit tests for rapid feedback (~30s)
python scripts/test_helper.py quick

# Smoke tests for critical path validation (~2-3min)
python scripts/test_helper.py smoke

# Full test suite with coverage (~5-10min)
python scripts/test_helper.py full

# Run the unit suite inside the container (ensures libffi/cffi and other system deps exist)
make container-test

# Override the subset executed in Docker (example: integration-only)
make container-test-integration

# Run a containerised rehearsal (starts mocks, runs users→groups→projects→work_packages, collects artefacts)
python scripts/run_rehearsal.py --collect --stop
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
6. **Checkpoint Hygiene**: Use `--reset-wp-checkpoints` after restoring rehearsal snapshots or when the SQLite store is rotated; the migration auto-rebuilds it on demand.

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

## Caching Best Practices

### Overview

The migration system provides thread-safe API call caching that reduces Jira API calls by 30-50%. Migrations support two execution modes:

1. **Standard ETL**: `run()` method - Simple extract-transform-load pattern
2. **Idempotent with Change Detection**: `run_with_change_detection()` - Skip migrations when no changes detected

### When to Use Idempotent Workflows

Use `run_with_change_detection()` when:

- Migration processes large datasets that rarely change (users, projects, issue types)
- Frequent re-runs are expected during development/testing
- API call reduction is critical for performance
- Migration can safely skip when source data unchanged

**Do NOT use** when:

- Migration performs data transformations on already-migrated data (e.g., CategoryDefaultsMigration)
- Migration depends on OpenProject state changes
- Migration has side effects beyond entity creation

### Implementing `_get_current_entities_for_type()`

This method enables change detection by fetching current entities from Jira. Implementation pattern:

```python
from typing import TYPE_CHECKING, Any

from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient


@register_entity_types("workflows")  # Register entity type for change detection
class WorkflowMigration(BaseMigration):
    """Example migration with idempotent support."""

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for change detection.

        Args:
            entity_type: The type of entities to retrieve (e.g., "workflows")

        Returns:
            List of entity dictionaries with consistent structure

        Raises:
            ValueError: If entity_type is not supported
        """
        # Validate entity type
        if entity_type != "workflows":
            msg = (
                f"WorkflowMigration does not support entity type: {entity_type}. "
                f"Supported types: ['workflows']"
            )
            raise ValueError(msg)

        # Fetch entities (automatically cached by BaseMigration)
        try:
            issue_types = self.jira_client.get_issue_types()
            schemes = self.jira_client.get_workflow_schemes()
            roles = self.op_client.get_roles()
        except Exception as exc:
            self.logger.exception("Failed to fetch entities: %s", exc)
            return []

        # Return structured data (will be checksummed for change detection)
        return [
            {
                "issue_types": issue_types,
                "schemes": schemes,
                "roles": roles,
            },
        ]
```

### Cache Invalidation Strategies

#### Local Cache (In-Run)

Thread-safe cache exists for duration of migration run:

```python
# Cache automatically populated during _get_current_entities_for_type()
# and reused in run_with_change_detection()

# No manual intervention needed - cache cleaned up after migration
```

#### Global Cache (Across Runs)

Force fresh data fetch with `--force` flag:

```bash
# Skip disk caches, fetch fresh data
python src/main.py migrate --components work_packages --force
```

Use `--force` when:
- Source data changed significantly
- Testing cache behavior
- Debugging stale data issues
- First run after Jira schema changes

#### Checkpoint Reset

Clear work package checkpoints after snapshot restore:

```bash
# Reset checkpoint store (auto-rebuilds on demand)
python src/main.py migrate --components work_packages --reset-wp-checkpoints
```

Use `--reset-wp-checkpoints` when:
- Restoring from rehearsal snapshot
- SQLite checkpoint store rotated
- Reprocessing work packages from scratch

### Thread-Safe Caching Patterns

BaseMigration provides automatic caching in `run_with_change_detection()`:

```python
def run_with_change_detection(self, entity_type: str | None = None) -> ComponentResult:
    """Run migration with change detection and caching.

    Workflow:
    1. Fetch entities via _get_current_entities_for_type() → CACHED
    2. Calculate checksums, compare with previous snapshot
    3. Skip if no changes detected
    4. Run migration if changes found
    5. Create new snapshot → uses CACHED entities

    Cache benefits:
    - API calls made once per run
    - Thread-safe access
    - Automatic cleanup after migration
    """
    if not entity_type:
        return self.run()

    # Check for changes (uses cached entities internally)
    should_skip, change_report = self.should_skip_migration(entity_type)

    if should_skip:
        return ComponentResult(
            success=True,
            message=f"No changes detected for {entity_type}, migration skipped",
            details={"change_report": change_report}
        )

    # Run migration
    result = self.run()

    # Create snapshot (reuses cached entities - no additional API calls!)
    if result.success:
        current_entities = get_cached_entities(entity_type)
        snapshot_path = self.create_snapshot(current_entities, entity_type)

    return result
```

### Performance Optimization Tips

#### API Call Reduction

**Before caching** (3 API calls per run):
1. Fetch for change detection
2. Fetch for migration execution
3. Fetch for snapshot creation

**After caching** (1 API call per run):
1. Fetch once → cached → reused everywhere

**Performance gain**: 30-50% reduction in API calls

#### Memory Management

BaseMigration enforces cache limits:

```python
# Maximum cache entries per type (default: 1000)
MAX_CACHE_SIZE = 1000

# Automatic cleanup at 90% threshold
cleanup_threshold = 0.9

# Cache cleared automatically after migration completes
```

**When cache limit exceeded**:
- Oldest entries evicted (FIFO)
- Warning logged at 90% threshold
- Critical alert at 100% (triggers cleanup)

### Common Pitfalls and Solutions

#### Pitfall 1: Stale Cache During Development

**Problem**: Local changes not reflected in migration

**Solution**:
```bash
# Force fresh fetch
python src/main.py migrate --components users --force
```

#### Pitfall 2: Memory Pressure

**Problem**: Large datasets exceed cache limits

**Solution**:
```python
# Process in batches in _get_current_entities_for_type()
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Fetch in batches to avoid memory pressure."""
    batch_size = 100
    all_entities = []

    for offset in range(0, total_count, batch_size):
        batch = self.jira_client.get_entities(offset=offset, limit=batch_size)
        all_entities.extend(batch)

    return all_entities
```

#### Pitfall 3: Transformation-Only Migrations

**Problem**: Migration operates on already-migrated data, change detection inappropriate

**Solution**:
```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Explicitly reject change detection for transformation migrations."""
    msg = (
        "CategoryDefaultsMigration is a transformation-only migration and does not "
        "support idempotent workflow. It operates on data from other migrations."
    )
    raise ValueError(msg)
```

### Code Examples: Three Tiers

#### Tier 1: Basic Migration (No Change Detection)

Simple migrations without caching needs:

```python
@register_entity_types("inline_refs")
class InlineRefsMigration(BaseMigration):
    """Transformation-only migration - no change detection needed."""

    def _extract(self) -> ComponentResult:
        """Extract work package IDs from mapping."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        wp_ids: list[int] = []
        for entry in wp_map.values():
            if isinstance(entry, dict) and entry.get("openproject_id"):
                try:
                    wp_ids.append(int(entry["openproject_id"]))
                except Exception:
                    continue
        return ComponentResult(success=True, data={"work_package_ids": wp_ids})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Pass through - no transformation."""
        return ComponentResult(success=True, data=extracted.data)

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Rewrite attachment references in work packages."""
        data = mapped.data or {}
        ids = data.get("work_package_ids", []) if isinstance(data, dict) else []
        if not ids:
            return ComponentResult(success=True, updated=0)

        # Execute Rails script for in-place updates
        script = "..."  # Rails script for attachment reference rewriting
        res = self.op_client.execute_script_with_data(script, ids)
        updated = int(res.get("updated", 0)) if isinstance(res, dict) else 0
        failed = int(res.get("failed", 0)) if isinstance(res, dict) else 0
        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run migration using standard ETL pattern."""
        logger.info("Starting inline refs migration...")
        try:
            extracted = self._extract()
            if not extracted.success:
                return ComponentResult(success=False, message="Extraction failed")

            mapped = self._map(extracted)
            if not mapped.success:
                return ComponentResult(success=False, message="Mapping failed")

            result = self._load(mapped)
            logger.info("Migration completed: updated=%s, failed=%s",
                       result.updated, result.failed)
            return result
        except Exception as e:
            logger.exception("Migration failed")
            return ComponentResult(success=False, message=str(e))
```

#### Tier 2: Migration with Change Detection

Idempotent migration with automatic caching:

```python
@register_entity_types("native_tags")
class NativeTagsMigration(BaseMigration):
    """Idempotent migration with change detection."""

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Fetch Jira issues for change detection."""
        if entity_type != "native_tags":
            msg = f"Unsupported entity type: {entity_type}"
            raise ValueError(msg)

        # Fetch work package keys from mapping
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        if not keys:
            return []

        # Batch fetch issues (automatically cached)
        try:
            issues = self.jira_client.batch_get_issues(keys)
        except Exception as exc:
            self.logger.exception("Failed to fetch issues: %s", exc)
            return []

        # Return structured data for checksumming
        return [
            {
                "key": key,
                "labels": getattr(issue.fields, "labels", []),
            }
            for key, issue in issues.items()
        ]

    def _extract(self) -> ComponentResult:
        """Extract labels from Jira issues."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        if not keys:
            return ComponentResult(success=True, data={"by_key": {}})

        issues = self.jira_client.batch_get_issues(keys)
        by_key: dict[str, list[str]] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                labels = self._coerce_labels(fields)
                if labels:
                    by_key[k] = sorted(set(labels))
            except Exception:
                continue
        return ComponentResult(success=True, data={"by_key": by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Map Jira labels to OpenProject tags with colors."""
        data = extracted.data or {}
        by_key: dict[str, list[str]] = data.get("by_key", {})
        wp_map = self.mappings.get_mapping("work_package") or {}
        updates: list[dict[str, Any]] = []

        for jira_key, names in by_key.items():
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])
            tag_defs = [{"name": n, "color": self._name_to_color_hex(n)} for n in names]
            updates.append({"work_package_id": wp_id, "tags": tag_defs})

        return ComponentResult(success=True, data={"updates": updates})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Create tags and assign to work packages."""
        data = mapped.data or {}
        updates = data.get("updates", [])
        if not updates:
            return ComponentResult(success=True, updated=0)

        script = "..."  # Rails script for tag creation/assignment
        res = self.op_client.execute_script_with_data(script, updates)
        updated = int(res.get("updated", 0)) if isinstance(res, dict) else 0
        failed = int(res.get("failed", 0)) if isinstance(res, dict) else 0
        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run with change detection if supported, otherwise standard ETL."""
        logger.info("Starting native tags migration...")
        try:
            # Use run_with_change_detection() from BaseMigration
            # Automatically caches entities, skips if no changes
            result = self.run_with_change_detection("native_tags")

            # If change detection not applicable, falls back to standard ETL
            if result is None:
                extracted = self._extract()
                if not extracted.success:
                    return ComponentResult(success=False, message="Extraction failed")

                mapped = self._map(extracted)
                if not mapped.success:
                    return ComponentResult(success=False, message="Mapping failed")

                result = self._load(mapped)

            logger.info("Migration completed: success=%s, updated=%s",
                       result.success, result.updated)
            return result
        except Exception as e:
            logger.exception("Migration failed")
            return ComponentResult(success=False, message=str(e))
```

#### Tier 3: Advanced Cache Management

Complex migration with custom caching logic:

```python
@register_entity_types("workflows")
class WorkflowMigration(BaseMigration):
    """Advanced migration with multi-source caching."""

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Fetch workflows from multiple sources with caching."""
        if entity_type != "workflows":
            msg = f"Unsupported entity type: {entity_type}"
            raise ValueError(msg)

        # Fetch from multiple sources (all cached automatically)
        try:
            issue_types = self.jira_client.get_issue_types()  # API call 1
            schemes = self.jira_client.get_workflow_schemes()  # API call 2
            roles = self.op_client.get_roles()  # API call 3
        except Exception as exc:
            self.logger.exception("Failed to fetch metadata: %s", exc)
            return []

        # Build workflow name index
        issue_type_by_id = {
            str(item.get("id")): item.get("name")
            for item in issue_types
            if item.get("id") and item.get("name")
        }

        # Extract workflow names from schemes
        workflow_names: set[str] = set()
        issue_type_to_workflow: dict[str, str] = {}
        for scheme in schemes:
            mappings = scheme.get("issueTypeMappings") or {}
            for issue_type_id, workflow_name in mappings.items():
                jira_name = issue_type_by_id.get(str(issue_type_id))
                if jira_name and workflow_name:
                    issue_type_to_workflow[jira_name] = workflow_name
                    workflow_names.add(workflow_name)

        # Fetch transitions and statuses per workflow (API calls 4-N)
        workflow_transitions: dict[str, list[dict[str, Any]]] = {}
        workflow_statuses: dict[str, list[dict[str, Any]]] = {}
        for workflow_name in workflow_names:
            try:
                transitions = self.jira_client.get_workflow_transitions(workflow_name)
                workflow_transitions[workflow_name] = transitions
            except Exception:
                workflow_transitions[workflow_name] = []

            try:
                statuses = self.jira_client.get_workflow_statuses(workflow_name)
                workflow_statuses[workflow_name] = statuses
            except Exception:
                workflow_statuses[workflow_name] = []

        # Return aggregated data (single checksum for all workflow data)
        return [
            {
                "issue_type_to_workflow": issue_type_to_workflow,
                "workflow_transitions": workflow_transitions,
                "workflow_statuses": workflow_statuses,
                "roles": roles,
            },
        ]

    def _extract(self) -> ComponentResult:
        """Extract workflow metadata (same as _get_current_entities_for_type)."""
        # Duplicate logic here for standard run() path
        # In run_with_change_detection(), this is skipped and cached data used
        try:
            issue_types = self.jira_client.get_issue_types()
            schemes = self.jira_client.get_workflow_schemes()
            roles = self.op_client.get_roles()
        except Exception as exc:
            return ComponentResult(
                success=False,
                message=f"Failed to extract: {exc}",
            )

        # ... rest of extraction logic ...
        return ComponentResult(success=True, data=data)

    def run(self) -> ComponentResult:
        """Execute with change detection."""
        self.logger.info("Starting workflow migration")

        # Attempt change detection first
        try:
            result = self.run_with_change_detection("workflows")
            if result.message and "skipped" in result.message.lower():
                self.logger.info("No changes detected, migration skipped")
                return result
        except Exception as exc:
            self.logger.warning("Change detection failed, falling back to standard run: %s", exc)

        # Fall back to standard ETL if change detection unavailable
        extracted = self._extract()
        if not extracted.success:
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            return mapped

        result = self._load(mapped)
        if result.success:
            self.logger.info("Workflow migration completed (created=%s, existing=%s)",
                           result.details.get("created", 0),
                           result.details.get("existing", 0))
        return result
```

## Migration Development Workflow

1. **Write component migration class** following architecture patterns
2. **Decide on caching strategy**:
   - Tier 1: Simple ETL for transformations
   - Tier 2: Change detection for large, stable datasets
   - Tier 3: Advanced caching for multi-source aggregation
3. **Implement exception-based error handling** throughout
4. **Add comprehensive tests** (unit + functional)
5. **Verify security** for any user input processing
6. **Test cache behavior** with `--force` and without
7. **Run full test suite** to ensure integration
8. **Update documentation** for any new patterns or requirements

This guide replaces the previous compliance checklist and verification process documents with a streamlined, actionable developer reference.
