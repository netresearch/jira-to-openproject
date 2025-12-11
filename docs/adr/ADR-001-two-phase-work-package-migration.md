# ADR-001: Two-Phase Work Package Migration

## Status

Accepted (2024-12-11)

## Context

The monolithic `work_packages` migration processes everything in a single pass per issue: creates work package, sets description, populates custom fields, and migrates journals/comments.

**Problem**: When migrating descriptions and comments, links to other Jira issues (e.g., "See PROJ-123") cannot be converted to OpenProject work package links ("See WP#456") if the target issue hasn't been migrated yet. This is a chicken-and-egg problem inherent to single-pass migrations with cross-references.

**Alternatives considered**:
1. **Single-pass with post-processing**: Migrate everything, then run a second pass to fix links. Doubles API calls and complexity.
2. **Four-phase approach**: Separate phases for skeleton, description, custom fields, journals. More granular but 4x orchestration overhead.
3. **Two-phase approach**: Skeleton phase for mapping, content phase for everything else. Balances simplicity with correctness.

## Decision

Implement two-phase work package migration:

### Phase 1: `work_packages_skeleton`

Creates minimal work packages to establish complete Jira-to-OpenProject mapping:
- Work package type, status, subject, project assignment
- J2O Origin Key custom field (for traceability)
- Outputs `work_package_mapping.json` with all mappings

Does NOT migrate: descriptions, custom field values, journals, attachments, watchers.

### Phase 2: `work_packages_content`

Populates all content using the complete mapping for link resolution:
- Descriptions with `PROJ-123` → `WP#456` conversion
- Custom field values
- Journals/comments with link conversion

## Consequences

### Positive

- **Correct link resolution**: All cross-references resolve because complete mapping exists before any content migration
- **Incremental re-runs**: Phase 2 can be re-run without Phase 1 (idempotent updates)
- **Minimal API overhead**: 2 calls per WP (create + update) vs 4+ for finer granularity
- **Simple orchestration**: Two commands vs four
- **Industry standard**: Follows ETL best practice for referential integrity

### Negative

- **Two-step process**: Operators must run both phases in order
- **Mapping file dependency**: Phase 2 requires Phase 1's output file
- **Memory usage**: Complete mapping loaded in Phase 2 (mitigated by streaming for large datasets)

## Usage

```bash
# Phase 1: Create skeletons (builds mapping)
python -m src.main migrate --components work_packages_skeleton

# Phase 2: Populate content (uses mapping for links)
python -m src.main migrate --components work_packages_content
```

## Implementation

- `src/migrations/work_package_skeleton_migration.py` - Phase 1
- `src/migrations/work_package_content_migration.py` - Phase 2
- Registered as `work_packages_skeleton` and `work_packages_content` components
- Legacy `work_packages` component retained for backward compatibility
