# BaseMigration API Reference

**Version**: 1.0
**Location**: `src/migrations/base_migration.py`
**Last Updated**: 2025-10-14

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Abstract Methods](#abstract-methods)
- [Execution Methods](#execution-methods)
- [State Management](#state-management)
- [Checkpoint & Recovery](#checkpoint--recovery)
- [Data Preservation](#data-preservation)
- [Cache Management](#cache-management)
- [Testing Guidelines](#testing-guidelines)
- [Complete Example](#complete-example)

---

## Overview

BaseMigration is the abstract base class for all j2o migration components. It provides comprehensive functionality for:

- **Extract-Map-Load Pipeline**: Standardized migration workflow
- **State Management**: Entity mapping and migration records
- **Checkpointing**: Resumable migrations with checkpoint support
- **Error Recovery**: Automatic retry and circuit breaker patterns
- **Data Preservation**: Conflict detection and resolution
- **Change Detection**: Idempotent migrations that skip unchanged data
- **Performance**: Thread-safe caching with memory management

**Design Principles**:
- Exception-based error handling
- Optimistic execution
- Dependency injection for clients
- Thread-safe operations

---

## Quick Start

### Creating a New Migration

```python
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

@register_entity_types("my_entities")
class MyMigration(BaseMigration):
    """Migrate my custom entities from Jira to OpenProject."""

    def extract(self) -> list[dict]:
        """Extract entities from Jira."""
        return self.jira_client.get_my_entities()

    def map(self, jira_data: list[dict]) -> list[dict]:
        """Transform Jira entities to OpenProject format."""
        return [self._map_entity(entity) for entity in jira_data]

    def load(self, openproject_data: list[dict]) -> None:
        """Load entities into OpenProject."""
        self.op_client.create_entities_batch(openproject_data)

    def run(self) -> ComponentResult:
        """Execute the migration."""
        # Extract
        jira_entities = self.extract()
        self.logger.info(f"Extracted {len(jira_entities)} entities")

        # Map
        op_entities = self.map(jira_entities)

        # Load
        self.load(op_entities)

        return ComponentResult(
            success=True,
            message=f"Migrated {len(op_entities)} entities",
            success_count=len(op_entities),
            failed_count=0,
            total_count=len(op_entities)
        )

    def _map_entity(self, jira_entity: dict) -> dict:
        """Map single entity."""
        return {
            "name": jira_entity["name"],
            "description": jira_entity["description"]
        }
```

---

## Abstract Methods

These methods **MUST** be implemented by all concrete migration classes.

### extract()

Extract data from Jira API.

```python
@abstractmethod
def extract(self) -> list[dict[str, Any]]:
    """Extract entities from Jira.

    Returns:
        List of entity dictionaries from Jira

    Raises:
        JiraClientError: If extraction fails
    """
    pass
```

**Implementation Guidelines**:
- Use `self.jira_client` for API calls
- Handle pagination for large datasets
- Apply filters/queries as needed
- Cache results with `_save_to_json()`
- Use tenacity for retries

**Example**:
```python
def extract(self) -> list[dict]:
    # Check cache first
    cached = self._load_from_json("my_entities.json")
    if cached:
        return cached

    # Extract from Jira
    entities = self.jira_client.search_issues(
        jql="project = MYPROJECT",
        fields=["key", "summary", "description"]
    )

    # Cache for idempotency
    self._save_to_json(entities, "my_entities.json")
    return entities
```

### map()

Transform Jira data to OpenProject format.

```python
@abstractmethod
def map(self, jira_data: list[dict]) -> list[dict[str, Any]]:
    """Transform Jira entities to OpenProject format.

    Args:
        jira_data: Entities from Jira extract()

    Returns:
        List of OpenProject-formatted entities

    Raises:
        MappingError: If transformation fails
    """
    pass
```

**Implementation Guidelines**:
- Transform field names and values
- Apply mappings from `self.mappings`
- Sanitize data (remove `_links`, flatten IDs)
- Validate required fields
- Preserve Jira provenance metadata

**Example**:
```python
def map(self, jira_data: list[dict]) -> list[dict]:
    return [self._map_single(entity) for entity in jira_data]

def _map_single(self, jira_entity: dict) -> dict:
    return {
        "name": jira_entity["fields"]["summary"],
        "description": jira_entity["fields"].get("description", ""),
        "j2o_origin_id": jira_entity["id"],
        "j2o_origin_key": jira_entity["key"]
    }
```

### load()

Load transformed data into OpenProject.

```python
@abstractmethod
def load(self, openproject_data: list[dict]) -> None:
    """Load entities into OpenProject.

    Args:
        openproject_data: Transformed entities from map()

    Raises:
        OpenProjectError: If load fails
    """
    pass
```

**Implementation Guidelines**:
- Use `self.op_client` for operations
- Batch operations for performance
- Register entity mappings
- Update `self.mappings` with Jira ID → OpenProject ID
- Handle errors gracefully

**Example**:
```python
def load(self, openproject_data: list[dict]) -> None:
    # Batch create for performance
    results = self.op_client.create_entities_batch(openproject_data)

    # Register mappings
    for jira_entity, result in zip(openproject_data, results):
        self.register_entity_mapping(
            jira_entity_type="my_entity",
            jira_entity_id=jira_entity["j2o_origin_id"],
            openproject_entity_type="entity",
            openproject_entity_id=result["id"]
        )

    # Update mappings file
    self.mappings.save()
```

---

## Execution Methods

BaseMigration provides multiple execution methods with different features.

### run()

Basic execution method - implement this in your migration class.

```python
def run(self) -> ComponentResult:
    """Execute migration with basic workflow."""
    pass
```

**When to use**: Always implement this method as the foundation.

### run_idempotent()

**Recommended for production** - full workflow with all safeguards.

```python
def run_idempotent(
    self,
    entity_type: str | None = None,
    operation_type: str = "migrate",
    entity_count: int = 0
) -> ComponentResult
```

**Features**:
- Change detection (skips unchanged data)
- Data preservation (conflict resolution)
- State management (entity mapping)
- Snapshot creation (rollback capability)

**Example**:
```python
# In CLI or orchestration code
migration = MyMigration()
result = migration.run_idempotent(
    entity_type="my_entities",
    entity_count=100
)
```

### run_with_change_detection()

Lightweight idempotency with change detection only.

```python
def run_with_change_detection(
    self,
    entity_type: str | None = None
) -> ComponentResult
```

**Features**:
- Checks for changes before migration
- Skips if no changes detected
- Creates snapshot after success
- Enhanced caching

**When to use**: When you need idempotency but don't need full data preservation.

### run_with_state_management()

Full state tracking without data preservation.

```python
def run_with_state_management(
    self,
    entity_type: str | None = None,
    operation_type: str = "migrate",
    entity_count: int = 0
) -> ComponentResult
```

**Features**:
- Change detection
- Migration record tracking
- Entity mapping registration
- State snapshots

**When to use**: When you need state tracking but not conflict resolution.

### run_with_data_preservation()

Comprehensive data preservation and conflict detection.

```python
def run_with_data_preservation(
    self,
    entity_type: str | None = None,
    operation_type: str = "migrate",
    entity_count: int = 0,
    analyze_conflicts: bool = True,
    create_backups: bool = True
) -> ComponentResult
```

**Features**:
- Pre-migration conflict analysis
- Entity backups before modification
- Conflict resolution
- Original state preservation
- Full state management

**When to use**: Critical migrations where data conflicts must be detected and resolved.

### run_with_recovery()

Enhanced error recovery with checkpointing.

```python
def run_with_recovery(
    self,
    entity_type: str | None = None,
    operation_type: str = "migrate",
    entity_count: int = 0,
    enable_checkpoints: bool = True,
    checkpoint_frequency: int = 10
) -> ComponentResult
```

**Features**:
- Checkpoint creation every N entities
- Automatic resume from last checkpoint
- Failure classification
- Manual recovery plan generation
- Progress tracking

**When to use**: Long-running migrations that need resumability.

### run_selective_update()

Incremental updates for changed entities only.

```python
def run_selective_update(
    self,
    entity_type: str | None = None,
    dry_run: bool = False,
    update_settings: dict[str, Any] | None = None
) -> ComponentResult
```

**Features**:
- Change detection
- Update plan creation
- Selective entity updates
- Dry-run simulation

**When to use**: Incremental migrations after initial full migration.

---

## State Management

### Migration Records

Track migration execution and results.

**start_migration_record()**

```python
def start_migration_record(
    self,
    entity_type: str,
    operation_type: str = "migrate",
    entity_count: int = 0,
    metadata: dict[str, Any] | None = None
) -> str
```

**Returns**: Migration record ID

**Example**:
```python
record_id = self.start_migration_record(
    entity_type="users",
    entity_count=100,
    metadata={"dry_run": False}
)
```

**complete_migration_record()**

```python
def complete_migration_record(
    self,
    record_id: str,
    success_count: int,
    error_count: int = 0,
    errors: list[str] | None = None
) -> None
```

**Example**:
```python
self.complete_migration_record(
    record_id=record_id,
    success_count=95,
    error_count=5,
    errors=["Entity 123 failed: invalid data"]
)
```

### Entity Mapping

Register relationships between Jira and OpenProject entities.

**register_entity_mapping()**

```python
def register_entity_mapping(
    self,
    jira_entity_type: str,
    jira_entity_id: str,
    openproject_entity_type: str,
    openproject_entity_id: str,
    metadata: dict[str, Any] | None = None
) -> str
```

**Example**:
```python
mapping_id = self.register_entity_mapping(
    jira_entity_type="user",
    jira_entity_id="10001",
    openproject_entity_type="user",
    openproject_entity_id="42",
    metadata={"migrated_at": "2025-10-14"}
)
```

**get_entity_mapping()**

```python
def get_entity_mapping(
    self,
    jira_entity_type: str,
    jira_entity_id: str
) -> dict[str, Any] | None
```

**Example**:
```python
mapping = self.get_entity_mapping("user", "10001")
if mapping:
    op_id = mapping["openproject_entity_id"]
```

---

## Checkpoint & Recovery

### Creating Checkpoints

**create_checkpoint_during_migration()**

```python
def create_checkpoint_during_migration(
    self,
    step_name: str,
    step_description: str,
    entities_processed: int = 0,
    entities_total: int = 0,
    current_entity_id: str | None = None,
    current_entity_type: str | None = None,
    metadata: dict[str, Any] | None = None
) -> str | None
```

**Example in Migration Loop**:
```python
def run(self) -> ComponentResult:
    entities = self.extract()
    total = len(entities)

    for i, entity in enumerate(entities):
        # Process entity
        self.process_entity(entity)

        # Checkpoint every 10 entities
        if (i + 1) % 10 == 0:
            self.create_checkpoint_during_migration(
                step_name=f"batch_{i//10}",
                step_description=f"Processed {i+1}/{total} entities",
                entities_processed=i+1,
                entities_total=total,
                current_entity_id=entity["id"]
            )
```

### Resuming Migrations

**resume_migration()**

```python
def resume_migration(self, migration_record_id: str) -> ComponentResult
```

**Example**:
```python
# Migration failed at entity 50/100
result = migration.resume_migration("migration_abc123")
# Resumes from last checkpoint
```

### Progress Tracking

**get_migration_progress()**

```python
def get_migration_progress(self, migration_record_id: str) -> dict[str, Any]
```

**Example**:
```python
progress = migration.get_migration_progress("migration_abc123")
print(f"Progress: {progress['progress']['percentage']}%")
print(f"Checkpoints: {progress['checkpoint_count']}")
```

---

## Data Preservation

### Storing Original States

**store_original_entity_state()**

```python
def store_original_entity_state(
    self,
    entity_id: str,
    entity_type: str,
    entity_data: dict[str, Any],
    source: str = "migration"
) -> None
```

**Example**:
```python
for entity in entities:
    # Store before modification
    self.store_original_entity_state(
        entity_id=entity["id"],
        entity_type="user",
        entity_data=entity,
        source="migration"
    )

    # Now modify
    self.update_entity(entity)
```

### Conflict Detection

**detect_preservation_conflicts()**

```python
def detect_preservation_conflicts(
    self,
    jira_changes: dict[str, Any],
    entity_id: str,
    entity_type: str,
    current_openproject_data: dict[str, Any]
) -> dict[str, Any] | None
```

**Example**:
```python
conflict = self.detect_preservation_conflicts(
    jira_changes={"name": "New Name"},
    entity_id="user_123",
    entity_type="user",
    current_openproject_data={"name": "Current Name"}
)

if conflict:
    self.logger.warning(f"Conflict detected: {conflict}")
```

### Conflict Resolution

**resolve_preservation_conflict()**

```python
def resolve_preservation_conflict(
    self,
    conflict: dict[str, Any],
    jira_data: dict[str, Any],
    openproject_data: dict[str, Any]
) -> dict[str, Any]
```

**Example**:
```python
resolved = self.resolve_preservation_conflict(
    conflict=conflict,
    jira_data=jira_entity,
    openproject_data=op_entity
)
# Use resolved data for update
```

---

## Cache Management

BaseMigration includes thread-safe entity caching with memory management.

### Cache Configuration

```python
class BaseMigration:
    MAX_CACHE_SIZE_PER_TYPE = 1000  # Max entities per type
    MAX_TOTAL_CACHE_SIZE = 5000     # Max total cached entities
    CACHE_CLEANUP_THRESHOLD = 0.8   # Cleanup at 80% full
```

### Cache Methods

**_get_cached_entities_threadsafe()**

Internal method - automatically used by run variants.

**Features**:
- Thread-safe access with RLock
- Automatic memory cleanup
- LRU-like eviction
- Cache statistics tracking

### Cache Statistics

```python
# Access cache stats
stats = migration._cache_stats
print(f"Hits: {stats['hits']}")
print(f"Misses: {stats['misses']}")
print(f"Evictions: {stats['evictions']}")
```

---

## Testing Guidelines

### Unit Testing

**Test Structure**:
```python
# tests/unit/test_my_migration.py
import pytest
from src.migrations.my_migration import MyMigration

class TestMyMigration:
    @pytest.fixture
    def migration(self, mock_jira_client, mock_op_client):
        return MyMigration(
            jira_client=mock_jira_client,
            op_client=mock_op_client
        )

    def test_extract(self, migration, mock_jira_client):
        # Setup mock
        mock_jira_client.get_my_entities.return_value = [
            {"id": "1", "name": "Entity 1"}
        ]

        # Execute
        result = migration.extract()

        # Assert
        assert len(result) == 1
        assert result[0]["id"] == "1"

    def test_map(self, migration):
        # Given
        jira_data = [{"id": "1", "fields": {"summary": "Test"}}]

        # When
        result = migration.map(jira_data)

        # Then
        assert len(result) == 1
        assert result[0]["name"] == "Test"

    def test_load(self, migration, mock_op_client):
        # Given
        op_data = [{"name": "Entity 1"}]
        mock_op_client.create_entities_batch.return_value = [
            {"id": "42"}
        ]

        # When
        migration.load(op_data)

        # Then
        mock_op_client.create_entities_batch.assert_called_once()
```

### Integration Testing

```python
# tests/integration/test_my_migration.py
@pytest.mark.integration
def test_full_migration_workflow(jira_client, op_client):
    migration = MyMigration(
        jira_client=jira_client,
        op_client=op_client
    )

    result = migration.run_idempotent(
        entity_type="my_entities",
        entity_count=10
    )

    assert result.success
    assert result.success_count == 10
    assert result.failed_count == 0
```

### Testing Best Practices

1. **Mock External Clients**: Use mock_jira_client and mock_op_client fixtures
2. **Test Each Method**: Unit test extract(), map(), load() separately
3. **Test Error Handling**: Verify exception handling
4. **Test Idempotency**: Run twice, verify no duplicates
5. **Test Mappings**: Verify entity mappings are registered
6. **Use Markers**: `@pytest.mark.unit`, `@pytest.mark.integration`

---

## Complete Example

Here's a comprehensive migration implementation:

```python
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

@register_entity_types("my_custom_entities")
class MyCustomMigration(BaseMigration):
    """Migrate custom entities from Jira to OpenProject.

    This migration handles:
    - Entity extraction from Jira custom fields
    - Field mapping and transformation
    - Batch loading to OpenProject
    - Entity mapping registration
    """

    def extract(self) -> list[dict]:
        """Extract custom entities from Jira.

        Returns:
            List of entity dictionaries
        """
        # Check cache for idempotency
        cached = self._load_from_json("my_entities_jira.json")
        if cached:
            self.logger.info("Using cached Jira entities")
            return cached

        self.logger.info("Extracting entities from Jira")

        # Extract from Jira with pagination
        entities = []
        start_at = 0
        max_results = 50

        while True:
            batch = self.jira_client.search_issues(
                jql="project = MYPROJECT AND type = CustomEntity",
                fields=["key", "summary", "customfield_10100"],
                start_at=start_at,
                max_results=max_results
            )

            entities.extend(batch["issues"])

            if len(batch["issues"]) < max_results:
                break

            start_at += max_results

        self.logger.info(f"Extracted {len(entities)} entities")

        # Cache for idempotency
        self._save_to_json(entities, "my_entities_jira.json")

        return entities

    def map(self, jira_data: list[dict]) -> list[dict]:
        """Transform Jira entities to OpenProject format.

        Args:
            jira_data: Raw Jira entities

        Returns:
            OpenProject-formatted entities
        """
        self.logger.info(f"Mapping {len(jira_data)} entities")

        mapped = []
        for entity in jira_data:
            try:
                mapped_entity = self._map_single_entity(entity)
                mapped.append(mapped_entity)
            except Exception as e:
                self.logger.error(
                    f"Failed to map entity {entity['key']}: {e}"
                )
                raise

        # Cache mapped entities
        self._save_to_json(mapped, "my_entities_openproject.json")

        return mapped

    def load(self, openproject_data: list[dict]) -> None:
        """Load entities into OpenProject.

        Args:
            openproject_data: Transformed entities
        """
        self.logger.info(f"Loading {len(openproject_data)} entities")

        # Batch size for performance
        batch_size = 50
        total_created = 0

        for i in range(0, len(openproject_data), batch_size):
            batch = openproject_data[i:i+batch_size]

            try:
                # Batch create
                results = self.op_client.create_entities_batch(batch)

                # Register mappings
                for entity, result in zip(batch, results):
                    self.register_entity_mapping(
                        jira_entity_type="custom_entity",
                        jira_entity_id=entity["j2o_jira_id"],
                        openproject_entity_type="entity",
                        openproject_entity_id=result["id"],
                        metadata={"jira_key": entity["j2o_jira_key"]}
                    )

                total_created += len(results)
                self.logger.info(
                    f"Created batch {i//batch_size + 1}: "
                    f"{total_created}/{len(openproject_data)}"
                )

            except Exception as e:
                self.logger.error(f"Failed to load batch {i//batch_size}: {e}")
                raise

        # Save updated mappings
        self.mappings.save()

    def run(self) -> ComponentResult:
        """Execute the migration pipeline.

        Returns:
            ComponentResult with migration status
        """
        try:
            # Extract
            jira_entities = self.extract()

            if not jira_entities:
                return ComponentResult(
                    success=True,
                    message="No entities to migrate",
                    success_count=0,
                    failed_count=0,
                    total_count=0
                )

            # Map
            op_entities = self.map(jira_entities)

            # Load
            self.load(op_entities)

            return ComponentResult(
                success=True,
                message=f"Successfully migrated {len(op_entities)} entities",
                success_count=len(op_entities),
                failed_count=0,
                total_count=len(jira_entities)
            )

        except Exception as e:
            self.logger.exception("Migration failed")
            return ComponentResult(
                success=False,
                message=f"Migration failed: {e}",
                errors=[str(e)],
                success_count=0,
                failed_count=1,
                total_count=0
            )

    def _map_single_entity(self, jira_entity: dict) -> dict:
        """Map a single Jira entity to OpenProject format.

        Args:
            jira_entity: Raw Jira entity

        Returns:
            OpenProject-formatted entity
        """
        fields = jira_entity["fields"]

        return {
            "name": fields["summary"],
            "description": fields.get("description", ""),
            "custom_field_value": fields.get("customfield_10100", ""),
            # Provenance
            "j2o_jira_id": jira_entity["id"],
            "j2o_jira_key": jira_entity["key"],
            "j2o_jira_url": f"{self.jira_client.server_url}/browse/{jira_entity['key']}"
        }


# Usage
if __name__ == "__main__":
    migration = MyCustomMigration()

    # Recommended: use idempotent execution
    result = migration.run_idempotent(
        entity_type="my_custom_entities",
        entity_count=100
    )

    if result.success:
        print(f"✓ Migrated {result.success_count} entities")
    else:
        print(f"✗ Migration failed: {result.errors}")
```

---

## Related Documentation

- **[MIGRATION_COMPONENTS.md](MIGRATION_COMPONENTS.md)** - Complete migration catalog
- **[CLIENT_API.md](CLIENT_API.md)** - Client layer API reference
- **[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)** - Development standards
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - System architecture

---

## Support

For issues or questions:
1. Review existing migrations in `src/migrations/` for examples
2. Check tests in `tests/unit/migrations/` for patterns
3. Consult [Developer Guide](DEVELOPER_GUIDE.md) for conventions
4. Open issue in repository with details

---

**Last Updated**: 2025-10-14
**Maintained By**: j2o Development Team
**License**: MIT
