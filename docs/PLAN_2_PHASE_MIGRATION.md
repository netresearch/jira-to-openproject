# Two-Phase Work Package Migration Architecture

## Problem Statement

The current monolithic `work_packages` migration processes everything in a single pass per issue:
- Creates work package
- Sets description (with link conversion)
- Populates custom fields
- Migrates journals/comments (with link conversion)

**Critical Issue**: When migrating descriptions and comments, links to other Jira issues (e.g., "See PROJ-123") cannot be converted to OpenProject work package links ("See WP#456") if the target issue hasn't been migrated yet.

## Solution: Two-Phase Migration

### Phase 1: `work_packages_skeleton`

**Purpose**: Create all work packages with minimal data to establish complete Jira→OpenProject mapping.

**Scope**: ALL projects (not per-project)

**Data migrated**:
- Work package type (from Jira issue type)
- Status (from Jira status)
- Subject/Summary
- Project assignment
- J2O Origin Key custom field (Jira key for traceability)

**Data NOT migrated** (deferred to Phase 2):
- Description
- Custom field values
- Journals/comments
- Attachments
- Watchers

**Output**: Complete `work_package_mapping.json` with ALL Jira keys mapped to OpenProject WP IDs.

```
work_package_mapping.json
{
  "12345": {  // Jira issue ID
    "jira_key": "NRS-1",
    "openproject_id": 5572961,
    "project_key": "NRS"
  },
  "12346": {
    "jira_key": "PROJ-A-100",
    "openproject_id": 5572962,
    "project_key": "PROJ-A"
  },
  ...
}
```

### Phase 2: `work_packages_content`

**Purpose**: Populate all work package content using the complete mapping for accurate link conversion.

**Scope**: ALL projects (uses mapping from Phase 1)

**Data migrated**:
- Description (with full link resolution)
- Custom field values
- Journals/comments (with full link resolution)
- Watchers

**Link Resolution**: With complete mapping available:
- `"See NRS-123"` → `"See [WP#456](../work_packages/456)"`
- `"Depends on PROJ-A-789"` → `"[WP#1234](../work_packages/1234)"`
- Cross-project links work because ALL mappings exist

---

## Implementation Plan

### File Changes

#### 1. New Migration Class: `WorkPackageSkeletonMigration`

**Location**: `src/migrations/work_package_skeleton_migration.py`

```python
class WorkPackageSkeletonMigration(BaseMigration):
    """Phase 1: Create work package skeletons and establish mapping."""

    def migrate(self):
        """Create WP skeletons for ALL projects."""
        for project in self.get_all_jira_projects():
            for issue in self.iter_project_issues(project.key):
                wp_id = self._create_skeleton(issue, project)
                self._update_mapping(issue, wp_id)

        self._save_complete_mapping()

    def _create_skeleton(self, issue, project):
        """Create minimal WP: type, status, subject, project."""
        return self.op_client.create_work_package({
            "subject": issue.fields.summary,
            "_links": {
                "type": {"href": f"/api/v3/types/{type_id}"},
                "status": {"href": f"/api/v3/statuses/{status_id}"},
                "project": {"href": f"/api/v3/projects/{project_id}"},
            }
        })
```

#### 2. New Migration Class: `WorkPackageContentMigration`

**Location**: `src/migrations/work_package_content_migration.py`

```python
class WorkPackageContentMigration(BaseMigration):
    """Phase 2: Populate work package content with link resolution."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._load_complete_mapping()  # Required from Phase 1

    def migrate(self):
        """Populate content for ALL work packages."""
        for project in self.get_all_jira_projects():
            for issue in self.iter_project_issues(project.key):
                wp_id = self.mapping[issue.id]["openproject_id"]
                self._populate_content(issue, wp_id)

    def _populate_content(self, issue, wp_id):
        """Populate description, custom fields, journals."""
        # Description with link resolution
        description = self._convert_links(issue.fields.description)

        # Custom fields
        custom_fields = self._prepare_custom_fields(issue)

        # Update WP
        self.op_client.update_work_package(wp_id, {
            "description": {"raw": description},
            **custom_fields
        })

        # Journals/comments
        self._migrate_journals(issue, wp_id)

    def _convert_links(self, text):
        """Convert Jira issue keys to OpenProject WP links."""
        # Pattern: PROJ-123 → WP#456
        for jira_key, wp_id in self.key_to_wp_mapping.items():
            text = text.replace(jira_key, f"WP#{wp_id}")
        return text
```

#### 3. Update Component Registry

**Location**: `src/migrations/__init__.py`

```python
MIGRATION_COMPONENTS = {
    # ... existing components ...
    "work_packages_skeleton": WorkPackageSkeletonMigration,
    "work_packages_content": WorkPackageContentMigration,
    # Keep legacy for backward compatibility
    "work_packages": WorkPackageMigration,  # Monolithic (deprecated)
}
```

#### 4. Update CLI

**Location**: `src/main.py`

```python
# Add to component choices
VALID_COMPONENTS = [
    "users", "projects", "issue_types", "statuses",
    "work_packages_skeleton",  # NEW: Phase 1
    "work_packages_content",   # NEW: Phase 2
    "work_packages",           # Legacy monolithic
    # ...
]
```

---

## Usage

### Full Migration (Recommended Order)

```bash
# Prerequisites
python -m src.main migrate --components users projects issue_types statuses

# Phase 1: Create all work package skeletons (builds complete mapping)
python -m src.main migrate --components work_packages_skeleton

# Phase 2: Populate all content (uses mapping for link resolution)
python -m src.main migrate --components work_packages_content
```

### Project-Filtered Migration

```bash
# Phase 1 for specific projects
python -m src.main migrate --components work_packages_skeleton --jira-project-filter NRS,PROJ-A

# Phase 2 for same projects (mapping must include all referenced projects!)
python -m src.main migrate --components work_packages_content --jira-project-filter NRS,PROJ-A
```

### Re-running Content Migration

Phase 2 can be re-run without re-running Phase 1 (idempotent updates):

```bash
# Re-migrate descriptions only (future enhancement)
python -m src.main migrate --components work_packages_content --content-types description

# Re-migrate journals only (future enhancement)
python -m src.main migrate --components work_packages_content --content-types journals
```

---

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         JIRA                                         │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐                 │
│  │  NRS    │  │ PROJ-A  │  │ PROJ-B  │  │ PROJ-C  │                 │
│  │ issues  │  │ issues  │  │ issues  │  │ issues  │                 │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘                 │
└───────┼────────────┼────────────┼────────────┼──────────────────────┘
        │            │            │            │
        └────────────┴─────┬──────┴────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    PHASE 1: SKELETON                                 │
│                                                                      │
│  For each issue across ALL projects:                                │
│  1. Create WP with: type, status, subject, project                  │
│  2. Store mapping: jira_id → openproject_wp_id                      │
│                                                                      │
│  Output: work_package_mapping.json (COMPLETE)                       │
└─────────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                 work_package_mapping.json                            │
│  {                                                                   │
│    "12345": {"jira_key": "NRS-1", "openproject_id": 5572961},       │
│    "12346": {"jira_key": "NRS-2", "openproject_id": 5572962},       │
│    "23456": {"jira_key": "PROJ-A-100", "openproject_id": 5572963},  │
│    ...                                                               │
│  }                                                                   │
└─────────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    PHASE 2: CONTENT                                  │
│                                                                      │
│  For each issue across ALL projects:                                │
│  1. Load WP ID from mapping                                         │
│  2. Convert description links: NRS-2 → WP#5572962                   │
│  3. Convert journal links: PROJ-A-100 → WP#5572963                  │
│  4. Update WP with: description, custom_fields, journals            │
│                                                                      │
│  Link resolution works because mapping is COMPLETE                  │
└─────────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      OPENPROJECT                                     │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ Work Packages with:                                          │    │
│  │ - Correct types, statuses, subjects                          │    │
│  │ - Descriptions with resolved WP links                        │    │
│  │ - Comments with resolved WP links                            │    │
│  │ - Custom field values                                        │    │
│  │ - Full audit trail                                           │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Validation Checklist

### Phase 1 Completion
- [ ] All Jira issues have corresponding OpenProject WPs
- [ ] `work_package_mapping.json` contains all entries
- [ ] WPs have correct: type, status, subject, project
- [ ] J2O Origin Key custom field is set on all WPs

### Phase 2 Completion
- [ ] All descriptions are populated
- [ ] Intra-project links resolve correctly (NRS-1 → WP#X)
- [ ] Cross-project links resolve correctly (PROJ-A-1 → WP#Y)
- [ ] Custom fields are populated
- [ ] Journals/comments are migrated with resolved links
- [ ] Timestamps are preserved

---

## Future Enhancements (Deferred)

### Content Type Filtering
```bash
--content-types description,custom_fields,journals  # all (default)
--content-types description                         # just descriptions
--content-types journals                            # just journals
```

### Incremental Updates
```bash
--since 2024-01-01  # Only issues updated since date
```

### Parallel Processing
```bash
--parallel 4  # Process 4 projects concurrently in Phase 2
```

---

## Consensus

**Agreed**: 2024-12-11

**Participants**: User + Claude (with gemini-2.5-pro/flash consensus)

**Decision**:
- Two-phase approach (skeleton → content) instead of four-phase
- Complete mapping before any link conversion
- Single content pass (not separate passes for description/custom_fields/journals)
- Extensibility for future content-type filtering

**Rationale**:
- Solves link resolution chicken-and-egg problem
- Minimizes API calls (2 per WP vs 4)
- Simpler orchestration than 4 phases
- Industry best practice for data migrations with referential integrity
