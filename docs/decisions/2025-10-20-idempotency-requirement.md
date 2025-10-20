# ADR: Idempotency as Mandatory Requirement for All Migration Components

**Date**: 2025-10-20
**Status**: Accepted
**Deciders**: Development Team
**Context**: j2o migration tool architecture

## Context and Problem Statement

The j2o migration tool must support **idempotent execution** - the ability to run migrations multiple times without creating duplicates or corrupting data. We must decide how to implement and enforce idempotency across all migration components.

## Decision Drivers

- **Re-runnability**: Migrations must be safely re-runnable after failures or interruptions
- **Data Integrity**: No duplicate entities or corrupted references
- **Source of Truth**: Clear, authoritative way to identify entity relationships
- **Performance**: Efficient change detection without full data scans
- **Maintainability**: Consistent pattern across all migration components

## Considered Options

1. **Mapping Files as Primary Source** - Use JSON mapping files to track entity relationships
2. **Metadata as Primary Source** - Use provenance metadata on OpenProject entities
3. **Hybrid Approach** - Metadata as primary, mapping files as cache/fallback

## Decision Outcome

**Chosen**: Metadata as Primary Source with Mapping Files as Cache (Option 3)

**Rationale**: Metadata-based tracking provides authoritative, persistent source of truth that survives across migration runs and doesn't require external file synchronization.

### Architectural Principle

**CRITICAL RULE**: Mapping files (`var/data/*_mapping.json`) are **CACHE ONLY**, not authoritative sources. The authoritative source of entity relationships is **provenance metadata** stored on OpenProject entities.

## Provenance Metadata System

### Standard Provenance Fields

All migrated OpenProject entities **MUST** include provenance metadata identifying their Jira origin:

#### Custom Fields (Work Packages, Projects, etc.)

Required provenance custom fields for all migrated entities:

| Custom Field Name | Type | Purpose | Example Value |
|------------------|------|---------|---------------|
| `J2O Origin System` | string | Source system identifier | `jira` |
| `J2O Origin ID` | string | External entity ID | `10523` |
| `J2O Origin Key` | string | External entity key | `SRVAC-42` |
| `J2O Origin URL` | string | Direct link to source | `https://jira.example.com/browse/SRVAC-42` |

#### Description Markers (Projects, Work Packages)

HTML comment markers embedded in description field:

```html
<!-- J2O_ORIGIN_START -->
system=jira;key=SRVAC;id=10012;url=https://jira.example.com/browse/SRVAC
<!-- J2O_ORIGIN_END -->
```

**Purpose**: Provides backup provenance when custom fields are unavailable or deleted.

### Idempotency Pattern

```python
def migrate_entity(jira_entity: dict) -> None:
    """Idempotent migration pattern using provenance metadata."""

    # 1. Extract Jira identifiers
    jira_id = jira_entity["id"]
    jira_key = jira_entity["key"]

    # 2. Check if entity already exists via provenance metadata
    existing = find_by_provenance(
        origin_system="jira",
        origin_id=jira_id,
        origin_key=jira_key,
    )

    if existing:
        # Idempotent: Entity already migrated
        logger.info(f"Entity {jira_key} already exists (OP ID: {existing['id']})")
        return existing

    # 3. Create new entity with provenance metadata
    op_entity = transform_to_openproject(jira_entity)
    op_entity["custom_fields"] = [
        {"id": cf_id("J2O Origin System"), "value": "jira"},
        {"id": cf_id("J2O Origin ID"), "value": str(jira_id)},
        {"id": cf_id("J2O Origin Key"), "value": jira_key},
        {"id": cf_id("J2O Origin URL"), "value": f"https://jira.../browse/{jira_key}"},
    ]

    created = create_entity(op_entity)

    # 4. Update mapping cache for performance
    update_mapping_cache(jira_key, created["id"])

    return created
```

### Query by Provenance

```ruby
# Rails console query to find entity by Jira origin
WorkPackage
  .joins(:custom_values)
  .joins("INNER JOIN custom_fields ON custom_values.custom_field_id = custom_fields.id")
  .where("custom_fields.name = ?", "J2O Origin Key")
  .where("custom_values.value = ?", "SRVAC-42")
  .first
```

## Mapping Files as Cache

### Purpose of Mapping Files

Mapping files serve **ONLY** as performance optimization:

1. **Fast lookups**: Avoid database queries during batch operations
2. **Batch preparation**: Pre-compute entity relationships
3. **Change detection**: Compare current vs previous state efficiently
4. **Debugging**: Human-readable audit trail

### Critical Constraint

**Mapping files are NEVER the authoritative source**. They can be:
- Deleted (regenerated from provenance metadata)
- Corrupted (ignored and rebuilt)
- Stale (refreshed from OpenProject)
- Missing (created on-demand)

### Regeneration Pattern

```python
def rebuild_mapping_from_provenance(entity_type: str) -> dict[str, int]:
    """Rebuild mapping file from OpenProject provenance metadata."""

    # Query all entities with Jira provenance
    entities = query_by_custom_field(
        field_name="J2O Origin Key",
        field_type=entity_type,
    )

    # Build mapping: jira_key -> openproject_id
    mapping = {}
    for entity in entities:
        jira_key = get_custom_field_value(entity, "J2O Origin Key")
        op_id = entity["id"]

        if jira_key and op_id:
            mapping[jira_key] = op_id

    # Cache to disk for performance
    save_mapping_cache(entity_type, mapping)

    return mapping
```

## Idempotency Requirements by Component

### Fully Idempotent Components

These components **MUST** support idempotent execution:

- ✅ `users` - Uses provenance custom fields
- ✅ `groups` - Uses name-based matching + provenance
- ✅ `projects` - Uses identifier + provenance markers
- ✅ `custom_fields` - Uses name-based matching
- ✅ `issue_types` (work package types) - Uses name-based matching
- ✅ `status_types` - Uses name-based matching
- ✅ `priorities` - Uses name-based matching
- ✅ `workflows` - Uses project + type combination
- ✅ `agile_boards` - Uses name-based matching
- ✅ `companies` (Tempo customers) - Uses name-based matching
- ✅ `accounts` (Tempo accounts) - Uses name-based matching
- ✅ `link_types` - Uses custom fields for unmapped types

### Transformation-Only Components

These components **operate on already-migrated data** and document why they don't support standalone idempotency:

- ⚠️ `work_packages` - **SHOULD BE IDEMPOTENT** but currently blocks on missing mapping
- ⚠️ `versions` - Operates on work package mapping (transformation-only)
- ⚠️ `components` - Operates on work package mapping (transformation-only)
- ⚠️ `labels` - Operates on work package mapping (transformation-only)
- ⚠️ `resolutions` - Operates on work package mapping (transformation-only)
- ⚠️ `story_points` - Operates on work package mapping (transformation-only)
- ⚠️ `security_levels` - Operates on work package mapping (transformation-only)
- ⚠️ `votes_reactions` - Operates on work package mapping (transformation-only)
- ⚠️ `relations` - Operates on work package mapping (transformation-only)
- ⚠️ `watchers` - Operates on work package mapping (transformation-only)
- ⚠️ `attachments` - Operates on work package mapping (transformation-only)
- ⚠️ `attachment_provenance` - Operates on attachment data (transformation-only)
- ⚠️ `time_entries` - Operates on work package mapping (transformation-only)

**Note**: Transformation-only components raise `ValueError` in `_get_current_entities_for_type()` to explicitly document this design choice.

## Current Architectural Issue

### The Problem

**Work packages migration is incorrectly BLOCKING on missing `custom_field_mapping.json` file**, when it should:

1. **Query OpenProject** for custom fields with Jira provenance metadata
2. **Build mapping dynamically** from provenance custom fields
3. **Proceed with migration** using metadata-based entity resolution
4. **Update mapping cache** after successful migration

### Evidence from Codebase

```python
# src/migrations/work_package_migration.py:1645
# Attach origin mapping custom fields for provenance
if cf_vals:
    work_package["custom_fields"] = cf_vals
```

```python
# src/migrations/user_migration.py:206
# Prefer deterministic mapping via J2O provenance custom fields
op_users_by_origin_key: dict[str, dict[str, Any]] = {}
```

**The architecture ALREADY supports metadata-based idempotency**, but work_packages migration is not using it consistently.

### Required Fix

```python
def _get_or_create_custom_field_mapping(self) -> dict[str, Any]:
    """Get custom field mapping from cache or rebuild from provenance metadata."""

    # Try to load cached mapping
    mapping = self._load_cached_mapping("custom_field")

    if mapping:
        return mapping

    # Cache miss: rebuild from OpenProject provenance metadata
    logger.info("Rebuilding custom field mapping from provenance metadata")

    # Query OpenProject custom fields with Jira provenance
    op_custom_fields = self.op_client.get_custom_fields()

    mapping = {}
    for cf in op_custom_fields:
        # Check for J2O provenance markers
        jira_cf_id = get_custom_field_value(cf, "J2O Origin ID")
        jira_cf_name = get_custom_field_value(cf, "J2O Origin Key")

        if jira_cf_id or jira_cf_name:
            mapping[jira_cf_id or jira_cf_name] = {
                "id": cf["id"],
                "name": cf["name"],
                "type": cf["field_format"],
            }

    # Cache for performance
    self._save_mapping_cache("custom_field", mapping)

    return mapping
```

## Consequences

### Positive

- **True Idempotency**: Migrations can be re-run safely at any time
- **Data Integrity**: Provenance metadata survives mapping file deletions
- **Debuggability**: Entity relationships traceable through metadata
- **Performance**: Mapping cache provides fast lookups
- **Resilience**: Missing mapping files are non-fatal (rebuilt automatically)
- **Consistency**: Single architectural pattern across all components

### Negative

- **Initial Setup**: Requires provenance custom fields to be created
- **Query Overhead**: Provenance lookups require custom field joins
- **Storage Cost**: Additional custom field values per entity
- **Complexity**: Dual tracking (metadata + cache) requires maintenance

### Risks and Mitigation

| Risk | Mitigation |
|------|------------|
| Custom fields deleted | Description markers provide backup provenance |
| Metadata corruption | Mapping cache can restore from last known good state |
| Performance degradation | Indexed custom field lookups + aggressive caching |
| Migration failures | Provenance allows precise resume from failure point |

## Implementation Requirements

### For All New Migrations

1. **MUST** implement provenance metadata tracking
2. **MUST** query by provenance before creating entities
3. **SHOULD** update mapping cache after successful operations
4. **MUST** document if transformation-only (no standalone idempotency)

### For Existing Migrations

1. **Fix work_packages**: Remove dependency on mapping file, use provenance query
2. **Audit all components**: Verify provenance metadata is properly set
3. **Add rebuild tools**: CLI commands to regenerate mappings from provenance
4. **Document patterns**: Update AGENTS.md with idempotency rules

## Validation Criteria

A migration component satisfies idempotency requirements if:

1. ✅ Running migration twice creates no duplicates
2. ✅ Deleting mapping cache and re-running succeeds
3. ✅ Entity relationships preserved after mapping rebuild
4. ✅ Provenance metadata present on all migrated entities
5. ✅ Component documents transformation-only status (if applicable)

## Related Decisions

- [2025-10-20: Rails Console Requirement](2025-10-20-rails-console-requirement.md) - Why Rails console access required
- [2025-10-20: Tmux Session Requirement](2025-10-20-tmux-session-requirement.md) - Why persistent tmux session required
- [ARCHITECTURE.md](../ARCHITECTURE.md) - System architecture overview
- [AGENTS.md](../../AGENTS.md) - Development rules and patterns

## References

- `src/migrations/user_migration.py:206` - Provenance-based user matching
- `src/migrations/work_package_migration.py:1645` - Provenance custom fields
- `src/clients/openproject_client.py:2123-2124` - Description marker pattern
- `src/migrations/base_migration.py:234` - Change detector for idempotent operations
- `docs/ARCHITECTURE.md:Idempotency Patterns` - Technical implementation

## Conclusion

**Idempotency is a mandatory requirement** for all migration components that create or modify OpenProject entities. This is achieved through:

1. **Provenance metadata as authoritative source** (custom fields + description markers)
2. **Mapping files as performance cache only** (can be deleted and rebuilt)
3. **Query-before-create pattern** (check provenance before entity creation)
4. **Transformation-only exceptions** (explicitly documented components that operate on existing data)

Any migration component that blocks execution due to missing mapping files is **violating this architectural principle** and must be fixed to query provenance metadata instead.

### Enforcement

- **Code reviews MUST** verify provenance metadata implementation
- **Tests MUST** validate idempotent execution (run twice, check no duplicates)
- **Documentation MUST** explain transformation-only status when applicable
- **AGENTS.md MUST** reference this ADR in rules for migration development
