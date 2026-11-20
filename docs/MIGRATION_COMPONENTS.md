# Migration Components Catalog

**Version**: 2.0
**Last Updated**: 2025-12-11

## Overview

The j2o migration tool consists of 40+ specialized migration components, each handling a specific aspect of data migration from Jira to OpenProject. All components inherit from `BaseMigration` and follow the extract → map → load pipeline pattern.

**Related Documentation**:
- [Entity Mapping Reference](ENTITY_MAPPING.md) - Detailed Jira→OpenProject field mappings
- [Client API Reference](CLIENT_API.md)
- [Architecture Overview](ARCHITECTURE.md)
- [Developer Guide](DEVELOPER_GUIDE.md)
- [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)

---

## Module Development State

| Component | CLI Name | Status | Tested | Notes |
|-----------|----------|--------|--------|-------|
| **Core Entities** |
| UserMigration | `users` | Stable | Yes | Foundation - run first |
| GroupMigration | `groups` | Stable | Yes | Requires users and projects; grants project access |
| ProjectMigration | `projects` | Stable | Yes | Hierarchical structure |
| WorkPackageMigration | `work_packages` | Stable | Yes | Legacy single-phase |
| WorkPackageSkeletonMigration | `work_packages_skeleton` | Stable | Yes | Phase 1 - mapping |
| WorkPackageContentMigration | `work_packages_content` | Stable | Yes | Phase 2 - content |
| **Configuration** |
| StatusMigration | `status_types` | Stable | Yes | Workflow states |
| PriorityMigration | `priorities` | Stable | Yes | Issue priority |
| IssueTypeMigration | `issue_types` | Stable | Yes | Work package types |
| CustomFieldMigration | `custom_fields` | Stable | Yes | Field definitions |
| CustomFieldsGenericMigration | `customfields_generic` | Stable | Yes | Field values |
| WorkPackageFieldHistoryMigration | `work_package_field_history` | New | Unit | Structured field-change history |
| ResolutionMigration | `resolutions` | Stable | Yes | Resolution values |
| LinkTypeMigration | `link_types` | Stable | Yes | Relation types |
| **Tempo Integration** |
| CompanyMigration | `companies` | Stable | Yes | Tempo customers |
| AccountMigration | `accounts` | Stable | Yes | Tempo accounts |
| TempoAccountMigration | `tempo_accounts` | Stable | Partial | Alternative strategy |
| TimeEntryMigration | `time_entries` | Stable | Yes | Worklogs |
| **Attachments & Files** |
| AttachmentsMigration | `attachments` | Stable | Yes | Binary transfer |
| AttachmentProvenanceMigration | `attachment_provenance` | Stable | Yes | Metadata |
| **Relationships** |
| RelationMigration | `relations` | Stable | Yes | Issue links |
| WatcherMigration | `watchers` | Stable | Yes | Notifications |
| **Agile** |
| SprintEpicMigration | `sprint_epic` | Stable | Yes | Sprint/Epic links on WPs |
| AgileBoardMigration | `agile_boards` | Stable | Yes | Board queries + sprint versions |
| NativeBoardsMigration | `native_boards` | Stable | Yes | Native board snapshots; needs an archive project |
| VersionsMigration | `versions` | Stable | Yes | Release tracking |
| AffectsVersionsMigration | `affects_versions` | Stable | Yes | Version links |
| **Labels & Tags** |
| LabelsMigration | `labels` | Stable | Yes | Tag migration |
| NativeTagsMigration | `native_tags` | Stable | Yes | OP native tags |
| **Supplementary** |
| ComponentsMigration | `components` | Stable | Yes | Jira components |
| VotesMigration | `votes_reactions` | Stable | Yes | Vote counts |
| RemoteLinksMigration | `remote_links` | Stable | Yes | External links |
| InlineRefsMigration | `inline_refs` | Stable | Yes | Reference updates |
| EstimatesMigration | `estimates` | Stable | Yes | Time estimates |
| StoryPointsMigration | `story_points` | Stable | Yes | Agile estimation |
| SecurityLevelsMigration | `security_levels` | Stable | Yes | Access levels |
| **Workflow & Admin** |
| WorkflowMigration | `workflows` | Stable | Yes | Status transitions |
| AdminSchemeMigration | `admin_schemes` | Stable | Yes | Role memberships |
| ReportingMigration | `reporting` | Stable | Yes | Filters/dashboards |
| **Utilities** |
| CategoryDefaultsMigration | `category_defaults` | Stable | Yes | Default values |
| SimpleTasksMigration | `simpletasks` | Experimental | No | Task items |
| WPDefaults | `wp_defaults` | Utility | N/A | Helper class |

---

## BaseMigration Abstract Class

**Location**: `src/migrations/base_migration.py`

### Overview

Base class for all migration components providing common functionality:
- Extract-Map-Load pipeline pattern
- State management and checkpointing
- Error recovery and retry logic
- Idempotency support
- Performance monitoring
- Data preservation

### Abstract Methods

Must be implemented by all concrete migrations:

```python
@abstractmethod
def extract(self) -> list[dict[str, Any]]:
    """Extract data from Jira API."""

@abstractmethod
def map(self, jira_data: list[dict]) -> list[dict[str, Any]]:
    """Transform Jira data to OpenProject format."""

@abstractmethod
def load(self, openproject_data: list[dict]) -> None:
    """Load data into OpenProject via Rails console."""
```

### Key Features

- **Caching**: JSON-based idempotent caching (`_load_from_json`, `_save_to_json`)
- **Checkpointing**: SQLite-based checkpoint management for resumable migrations
- **Error Recovery**: Automatic retry with exponential backoff
- **State Management**: Track migration progress and status
- **Data Preservation**: Detect and resolve conflicts
- **Performance Tracking**: Monitor execution metrics

### Usage Pattern

```python
class CustomMigration(BaseMigration):
    def extract(self) -> list[dict]:
        # Fetch from Jira API
        return self.jira_client.get_data()

    def map(self, jira_data: list[dict]) -> list[dict]:
        # Transform data
        return [self._map_item(item) for item in jira_data]

    def load(self, openproject_data: list[dict]) -> None:
        # Load via Rails console
        self.op_client.create_batch(openproject_data)
```

---

## Core Entity Migrations

### UserMigration

**Location**: `src/migrations/user_migration.py`

Migrates Jira users to OpenProject with comprehensive metadata.

**Features**:
- J2O provenance fields (origin system, ID, key, URL)
- Locale → language mapping (Jira locale to OpenProject language preference)
- Avatar backfill via Avatars module
- Timezone metadata for accurate timestamp conversions
- Role mapping based on Jira groups
- Inactive Jira accounts: created locked unless an active Jira identity shares their login or email (both would end up on one account); on re-runs, mapped OpenProject users whose every Jira identity is inactive are locked (admins excepted). The tool never unlocks, because nothing records who locked an account

**Cleanup**: Automatically removes legacy "Jira user key" and "Tempo Account" custom fields

**Dependencies**: None (run first)

**Example Usage**:
```bash
uv run python -m src.main migrate --components users --no-confirm
```

**Related**: GroupMigration for group memberships

---

### GroupMigration

**Location**: `src/migrations/group_migration.py`

Synchronizes Jira groups and role-based memberships to OpenProject.

**Features**:
- Group creation with Jira provenance
- Role-based membership mapping
- Hierarchical group support
- Membership synchronization

**Dependencies**: UserMigration (users must exist), ProjectMigration (project mapping for the project-role groups)

**Sequence**: after `components` and before `category_defaults`. Its project-role groups add the first project members, while `simpletasks` and `components` change only projects without members and `category_defaults` needs them.

**Example Usage**:
```bash
uv run python -m src.main migrate --components groups --no-confirm
```

---

### ProjectMigration

**Location**: `src/migrations/project_migration.py`

Migrates Jira projects to OpenProject sub-projects with adaptive configuration.

**Features**:
- Lead assignment (Jira project lead → OpenProject Project admin role)
- Module enablement based on project characteristics:
  - `work_package_tracking`, `wiki` (always enabled)
  - `time_tracking`, `costs` (for Tempo-linked projects)
  - `calendar`, `news` (when categories/types present)
- Provenance metadata (key, ID, category, type, URL, avatar)
- Hierarchical project structure (under customer hierarchy)

**Dependencies**: UserMigration (for lead assignment)

**Example Usage**:
```bash
uv run python -m src.main migrate --components projects --no-confirm
```

**Related**: CompanyMigration (Tempo customers), AccountMigration (Tempo accounts)

---

### WorkPackageMigration (Legacy Single-Phase)

**Location**: `src/migrations/work_package_migration.py`

Migrates Jira issues to OpenProject work packages with full metadata in a single pass.

**Features**:
- Full issue metadata migration (type, status, priority, assignee, etc.)
- Start date derivation:
  1. Custom field precedence: `customfield_18690` → `12590` → `11490` → `15082`
  2. Fallback: First "In Progress" status transition timestamp
- Custom field value migration
- Checkpoint-based fast-forward for delta migrations
- Batch processing for large datasets

**Dependencies**: ProjectMigration, UserMigration, StatusMigration, PriorityMigration, IssueTypeMigration

**Checkpoint Management**:
```bash
# Reset checkpoints after snapshot restore
uv run python -m src.main migrate --reset-wp-checkpoints --components work_packages
```

**Example Usage**:
```bash
uv run python -m src.main migrate --components work_packages --no-confirm
```

**Related**: AttachmentsMigration, RelationMigration, WatcherMigration

---

### Two-Phase Work Package Migration (Recommended)

For migrations with cross-references between issues, use the two-phase approach for correct link resolution. See [ADR-001](adr/ADR-001-two-phase-work-package-migration.md) for architecture decision.

#### WorkPackageSkeletonMigration (Phase 1)

**Location**: `src/migrations/work_package_skeleton_migration.py`

Creates minimal work packages to establish complete Jira-to-OpenProject ID mapping.

**Creates**:
- Work package with type, status, subject
- Project assignment
- J2O Origin Key custom field (for traceability)
- Outputs `work_package_mapping.json` with all Jira→OP ID mappings

**Does NOT migrate**:
- Descriptions (requires link resolution)
- Custom field values
- Journals/comments
- Attachments, watchers

**Dependencies**: Same as WorkPackageMigration

**Example Usage**:
```bash
# Phase 1: Create skeletons and build mapping
uv run python -m src.main migrate --components work_packages_skeleton --no-confirm
```

#### WorkPackageContentMigration (Phase 2)

**Location**: `src/migrations/work_package_content_migration.py`

Populates all content using the complete mapping for link resolution.

**Migrates**:
- Descriptions with `PROJ-123` → `WP#456` link conversion
- Custom field values
- Journals/comments with link conversion
- User mentions (`@accountId` → `@username`)

**Requires**: `work_package_mapping.json` from Phase 1

**Dependencies**: WorkPackageSkeletonMigration must complete first

**Example Usage**:
```bash
# Phase 2: Populate content with resolved links
uv run python -m src.main migrate --components work_packages_content --no-confirm
```

#### Two-Phase Workflow

```bash
# Complete two-phase migration
uv run python -m src.main migrate --components work_packages_skeleton --no-confirm
uv run python -m src.main migrate --components work_packages_content --no-confirm

# Or use legacy single-phase (backward compatible)
uv run python -m src.main migrate --components work_packages --no-confirm
```

**Benefits**:
- Correct cross-reference resolution (all links resolve)
- Incremental re-runs (Phase 2 can be re-run without Phase 1)
- Minimal API overhead (2 calls per WP vs 4+ for finer granularity)

---

## Configuration Migrations

### StatusMigration

**Location**: `src/migrations/status_migration.py`

Migrates Jira statuses to OpenProject work package statuses.

**Features**:
- Status creation with Jira provenance
- Category mapping (To Do, In Progress, Done)
- Status ordering preservation

**Dependencies**: None

**Example Usage**:
```bash
uv run python -m src.main migrate --components status --no-confirm
```

**Related**: [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)

---

### PriorityMigration

**Location**: `src/migrations/priority_migration.py`

Migrates Jira priorities to OpenProject priorities.

**Features**:
- Priority creation with ordering
- Color/icon mapping
- Default priority configuration

**Dependencies**: None

**Example Usage**:
```bash
uv run python -m src.main migrate --components priority --no-confirm
```

---

### IssueTypeMigration

**Location**: `src/migrations/issue_type_migration.py`

Migrates Jira issue types to OpenProject work package types.

**Features**:
- Type creation with Jira provenance
- Workflow association
- Custom field mapping per type

**Dependencies**: None

**Example Usage**:
```bash
uv run python -m src.main migrate --components issue_types --no-confirm
```

---

### ResolutionMigration

**Location**: `src/migrations/resolution_migration.py`

Migrates Jira resolutions to OpenProject custom field values.

**Features**:
- Resolution as custom field
- Value mapping
- Closed status association

**Dependencies**: CustomFieldMigration

---

### CustomFieldMigration

**Location**: `src/migrations/custom_field_migration.py`

Migrates Jira custom field definitions.

**Features**:
- Field type mapping
- List/select options migration
- Required field configuration
- Project association

**Dependencies**: ProjectMigration

**Related**: CustomFieldsGenericMigration for values

---

### CustomFieldsGenericMigration

**Location**: `src/migrations/customfields_generic_migration.py`

Migrates custom field values for work packages.

**Features**:
- Value migration for all custom field types
- Multi-value field support
- Type-specific formatting

**Dependencies**: CustomFieldMigration, WorkPackageMigration

---

## Attachment & File Migrations

### AttachmentsMigration

**Location**: `src/migrations/attachments_migration.py`

Migrates Jira issue attachments to OpenProject work package files.

**Features**:
- Binary file transfer
- Author/timestamp preservation via Rails metadata
- MIME type mapping
- Attachment provenance tracking

**Dependencies**: WorkPackageMigration, UserMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components attachments --no-confirm
```

**Related**: AttachmentProvenanceMigration

---

### AttachmentProvenanceMigration

**Location**: `src/migrations/attachment_provenance_migration.py`

Enriches attachment metadata with Jira provenance.

**Features**:
- Jira attachment ID/URL tracking
- Original filename preservation
- Upload metadata (author, timestamp)

**Dependencies**: AttachmentsMigration

---

## Relationship Migrations

### RelationMigration

**Location**: `src/migrations/relation_migration.py`

Migrates Jira issue links to OpenProject work package relations.

**Features**:
- Link type mapping (blocks, relates, duplicates, etc.)
- Bidirectional relationship creation
- Cross-project relations

**Dependencies**: WorkPackageMigration, LinkTypeMigration

---

### LinkTypeMigration

**Location**: `src/migrations/link_type_migration.py`

Migrates Jira issue link types to OpenProject relation types.

**Features**:
- Link type creation
- Forward/reverse names
- Custom link type support

**Dependencies**: None

---

## Agile & Sprint Migrations

### SprintEpicMigration

**Location**: `src/application/components/sprint_epic_migration.py`

Applies Jira sprint membership and Epic Links to already-migrated work packages.
The sprint versions themselves are created by `AgileBoardMigration`.

**Features**:
- Subtask parent or Epic Link → `parent_id` hierarchy on the child work package. A direct parent takes precedence.
- Sprint → `version_id` on the work package, resolved via the `sprint` mapping
  (first matching sprint only, for issues without a Jira fix version)
- Sprint → "Sprint" text custom field, holding all sprint names comma-separated

**Dependencies**: ProjectMigration, WorkPackageMigration, AgileBoardMigration
(provides the `sprint` mapping)

**Related**: AgileBoardMigration, [Entity Mapping §11](ENTITY_MAPPING.md#11-agile-migration)

---

### AgileBoardMigration

**Location**: `src/application/components/agile_board_migration.py`

Creates one OpenProject saved query per Jira board and one OpenProject version
per Jira sprint. Handles the entity types `agile_boards` and `sprints`.

**Features**:
- Board → saved query named `[Board] <name>`, initially private. The agile snapshot records board metadata and the query uses project defaults.
- Sprint → project version (name, goal → description, start/due date, open/closed
  status), persisted in the `sprint` mapping for `SprintEpicMigration`

After the work-package import, the optional [native sprint runner](native-completed-sprints.md)
creates planned, active and completed sprints, project-specific goals and work-package
links on OpenProject 17.9. For native board snapshots see `NativeBoardsMigration` below.

**Dependencies**: ProjectMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components agile_boards --no-confirm
```

---

### NativeBoardsMigration

**Location**: `src/application/components/native_boards_migration.py`

Runs the `j2o-boards` import for every migrated project in the full profile. Each project-scoped Jira board becomes a native basic board in a private archive project, with the columns and the ranked cards of that project. The import code is shared with `j2o-boards`.

The component runs only when `migration.native_board_archive_project` in `config/config.yaml` names an existing private, memberless root project. Each migrated target project must be private too; the writer refuses boards for a public target. Without this setting the stage is reported as skipped, so a default run imports no native boards. It is also skipped when no project has been migrated. `--jira-project-filter` limits the projects.

**Features**:
- Each board is read twice. A board that changes between the reads is counted as failed.
- An unchanged board is verified in place. A changed board creates a new revision and keeps the earlier one.
- A failing board or a failing board listing is counted and reported. The remaining boards and projects are still imported.
- The result reports `created` and `verified` boards. Total, successful and failed counts appear on the dashboard.

**Sequence**: after `wp_metadata_backfill`. The writer matches each card by the J2O Origin ID, key, URL and system of its work package. `work_packages_skeleton` writes these fields and `wp_metadata_backfill` completes them on existing work packages.

**Dependencies**: ProjectMigration, WorkPackageSkeletonMigration, WpMetadataBackfillMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components native_boards --no-confirm
```

The [native sprint runner](native-completed-sprints.md) and the [deferred reference replay](deferred-references.md) remain separate operator commands. Both require a private project without members; in the full profile `groups` adds the first members. The sprint runner also accepts only one frozen manifest per project, and the reference replay needs the operator's age identity.

---

## Tempo & Time Tracking Migrations

### TimeEntryMigration

**Location**: `src/migrations/time_entry_migration.py`

Migrates Tempo worklogs to OpenProject time entries.

**Features**:
- Worklog → Time entry mapping
- Activity mapping
- Author preservation via Rails metadata
- Billable flag support

**Dependencies**: WorkPackageMigration, UserMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components time_entries --no-confirm
```

---

### CompanyMigration

**Location**: `src/migrations/company_migration.py`

Migrates Tempo customers to top-level OpenProject projects.

**Features**:
- Customer → Project mapping
- Hierarchical project structure
- Customer metadata preservation

**Dependencies**: None

**Related**: AccountMigration, ProjectMigration

---

### AccountMigration

**Location**: `src/migrations/account_migration.py`

Migrates Tempo accounts to custom field values on projects.

**Features**:
- Account → Custom field mapping
- Account categories
- Project association
- Work package "Tempo Account" field (string): `custom_fields` maps Jira's Tempo account issue field onto it, and `work_packages_content` writes the account key

**Dependencies**: CompanyMigration, ProjectMigration

---

### TempoAccountMigration

**Location**: `src/migrations/tempo_account_migration.py`

Alternative Tempo account migration strategy.

**Features**:
- Direct account mapping
- Category structure
- Lead assignment

**Dependencies**: ProjectMigration

---

## Workflow & Permission Migrations

### WorkflowMigration

**Location**: `src/migrations/workflow_migration.py`

Creates OpenProject workflow entries based on Jira workflows.

**Features**:
- Status transition mapping
- Role-based permissions per type
- Transition conditions
- Post-functions guidance

**Dependencies**: StatusMigration, IssueTypeMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components workflow --no-confirm
```

**Related**: [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)

---

### AdminSchemeMigration

**Location**: `src/migrations/admin_scheme_migration.py`

Migrates Jira role memberships to OpenProject project memberships.

**Features**:
- Role mapping (Jira role → OpenProject role)
- Project-specific permissions
- Group-based memberships
- User-level assignments

**Dependencies**: ProjectMigration, UserMigration, GroupMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --profile full --no-confirm
```

---

## Component & Module Migrations

### ComponentsMigration

Run `uv run j2o migrate --components components --no-confirm` after migrating the projects and work packages. Stage changes in private OpenProject projects before granting project membership. The component checks the target project's Jira origin and each participating work package’s immutable Jira ID, key, URL and source system. It accepts software and business project issues positively returned by `level IS EMPTY` and verifies their component references against the Jira project catalogue. The result's `unconfirmed_work_packages` counts mapped issues that could not be confirmed in this scope.

OpenProject supports one native category per work package. The first Jira component in source order supplies that category. The admin-only project attribute **Jira component membership origins** retains the complete ordered assignment. To recover it via `GET /api/v3/projects/{id}` as an administrator, read the attribute's raw JSON. Look up the immutable Jira issue ID in `issues`, then resolve each ID in its `components` array through the project's `components` object. Each entry contains the original Jira component ID, name and URL, and its verified OpenProject category ID.

Each batch of up to 100 work packages commits its category links and provenance together. A failed batch rolls back and earlier batches can be replayed. A Jira component rename updates the registered category with the same immutable source ID and URL while the target remains private and memberless. An independently renamed target category requires administrator review.


The component reuses earlier category IDs from the migration mapping and checks them against the project and name. A category name collision without a known origin, a locally edited native assignment, or a foreign target rolls back that project. Replaying an unchanged project writes zero assignments. A confirmed empty Jira component list clears the native category only when the previous assignment was recorded by this importer. This assignment import leaves category default assignees unchanged.

---

### VersionsMigration

The `versions` component imports the complete Jira release catalogue for each mapped project in scope, including versions without work packages. OpenProject stores the description, start date and release date on each native version.

| Jira state | OpenProject status |
| --- | --- |
| Released (including released and archived) | Closed |
| Archived and unreleased | Locked |
| Unreleased and active | Open |

The native version custom field `Jira Version Origin` stores the Jira URL, immutable version ID and both source flags. A rerun can update a renamed release by its ID. Earlier name-only imports acquire their source identity. Conflicting pre-existing target metadata stops that project transaction. The component leaves versions that exist only in OpenProject in place.

The target project must have matching Jira key, ID and base URL provenance. Work package assignments require a positive unrestricted Jira search result and matching target issue ID, key, URL and project. Shared versions or conflicting origins stop the operation.

---

### AffectsVersionsMigration

The `affects_versions` component imports Jira's `versions` issue field into OpenProject's native observed-in versions. It uses immutable IDs from the project release catalogue and confirms each unrestricted Jira issue's identity at the target. The existing `Affects Versions` text remains available on older OpenProject releases.

A rerun reconciles only native links previously added by this component. Independently added links and target-version assignments remain intact. An issue omitted from Jira's unrestricted result or a missing source field never clears a target.

Requires the project and work-package mappings. Run `versions` first on targets with native observed-in links.

---

## Additional Feature Migrations

### WatcherMigration

**Location**: `src/migrations/watcher_migration.py`

Migrates Jira watchers to OpenProject watchers.

**Features**:
- Watcher association
- Notification preferences
- Bulk watcher addition

**Dependencies**: WorkPackageMigration, UserMigration

---

### VotesMigration

**Location**: `src/migrations/votes_migration.py`

Migrates Jira votes to OpenProject custom field.

**Features**:
- Vote count tracking
- Voter list preservation (optional)
- Vote timestamp

**Dependencies**: WorkPackageMigration, CustomFieldMigration

---

### LabelsMigration

**Location**: `src/migrations/labels_migration.py`

Migrates Jira labels to OpenProject tags.

**Features**:
- Label → Tag mapping
- Tag creation
- Multi-label support

**Dependencies**: WorkPackageMigration

**Related**: NativeTagsMigration

---

### NativeTagsMigration

**Location**: `src/migrations/native_tags_migration.py`

Alternative tag migration using OpenProject native tags.

**Features**:
- Native tag system integration
- Tag colors
- Tag hierarchies

**Dependencies**: WorkPackageMigration

---

### RemoteLinksMigration

**Location**: `src/migrations/remote_links_migration.py`

Migrates Jira remote links to OpenProject custom field.

**Features**:
- Remote link URL tracking
- Link title/description
- Icon/application metadata

**Dependencies**: WorkPackageMigration, CustomFieldMigration

---

### InlineRefsMigration

**Location**: `src/migrations/inline_refs_migration.py`

Updates inline references in descriptions/comments.

**Features**:
- Jira key → OpenProject ID replacement
- Mention conversion (@user)
- Link rewriting

**Dependencies**: WorkPackageMigration, UserMigration

---

## Estimation & Planning Migrations

### EstimatesMigration

**Location**: `src/migrations/estimates_migration.py`

Migrates Jira estimates to OpenProject estimated hours.

**Features**:
- Original estimate mapping
- Remaining estimate
- Time tracking integration

**Dependencies**: WorkPackageMigration

---

### StoryPointsMigration

**Location**: `src/migrations/story_points_migration.py`

Migrates Jira story points to OpenProject custom field.

**Features**:
- Story point custom field creation
- Value migration
- Agile planning support

**Dependencies**: WorkPackageMigration, CustomFieldMigration

---

## Reporting & Analytics Migrations

### ReportingMigration

**Location**: `src/migrations/reporting_migration.py`

Migrates Jira saved filters and dashboards.

**Features**:
- Saved filter → Saved query mapping
- Dashboard → Wiki summary page
- Filter criteria conversion
- Query sharing permissions

**Dependencies**: ProjectMigration, WorkPackageMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --profile full --no-confirm
```

---

## Special Purpose Migrations

### SecurityLevelsMigration

**Location**: `src/migrations/security_levels_migration.py`

The standard work-package import asks Jira for issues with an empty security level. It excludes issues with a level from ordinary creation and content updates. Existing work-package mappings outside the confirmed scope require an access review before cutover. The migration preserves earlier targets.

**Features**:
- On previously mapped issues, this component records the source level name in the “Security Level” custom field.
- OpenProject project permissions govern access to those earlier work packages. The custom field stores metadata.

**Dependencies**: CustomFieldMigration

---

### CategoryDefaultsMigration

The `category_defaults` stage reads each selected project's component catalogue through the Jira SDK. Run `components` first to establish the native category IDs. In a private target project the administrator can recover each component's configured policy, effective policy, validity, and original user identifiers from **Jira category default policies**.

A valid component-lead or project-lead rule becomes a fixed OpenProject category default only when Jira confirms the same effective assignee and OpenProject confirms the category's source, the user's migration identifiers, and existing project membership. OpenProject represents this as the current person's ID. Run the stage again after a Jira leadership change. Inherited policies remain in the administrative project attribute. An explicitly unassigned policy leaves the native default empty. The stage reports unresolved policies and preserves independently configured category defaults. It changes no work packages, memberships or accounts.

---

### WPDefaults

**Location**: `src/migrations/wp_defaults.py`

Utility for work package default value application.

**Features**:
- Default field values
- Bulk default application
- Project-specific defaults

**Dependencies**: WorkPackageMigration

---

### SimpleTasksMigration

The default and `full` profiles run `simpletasks` immediately after `work_packages_content`. When Jira advertises no Simple Tasklists fields, the stage skips target changes. For installations without these fields, an explicit `simpletasks_property_key` enables the source check.

The `simpletasks` stage selects mapped issues from the configured projects through Jira's empty-security-level search. It compares issue descriptions with their rendered checkboxes. For Jira Software issues with a task count, it also reads the add-on's per-issue list, including lists made entirely of description tasks. Only a matching description task or a directly created sidebar task is eligible. Comment, custom-field and uncorroborated issue-property content stays out of both target representations. Customer-facing projects require separate audience verification. See the [vendor's task location documentation](https://simpletasks.tss.io/faq).

A private project's work package description displays the ordered Markdown checklist with completion and mandatory states. The administrator-only **Jira tasklist origins** project attribute records each issue's Jira ID, key and target work package ID. Each task retains its source ID, text, location and checked state, along with the ordinal, mandatory flag, editing account identifiers, created/changed timestamps and overdue flag when supplied. The source Markdown description order is used for description-only add-on versions without task ordinals.

The stage verifies the target project and each affected work package's origin inside the write transaction. Description edits require a private, memberless staging project. Each batch of up to 100 work packages commits its register and descriptions together. A retry keeps unmodified sections and unrelated work package text. A changed or missing previously registered section needs operator review.

**Implementation:** `src/application/components/simpletasks_migration.py`

**Depends on:** work package mappings

---

### WorkPackageFieldHistoryMigration

The default and `full` profiles run `work_package_field_history` directly after `customfields_generic`. It keeps each migrated issue's Jira field changes as structured history in the work-package text custom field **Jira Field Changes (structured history)**. It does not create native OpenProject journals: those cannot keep each change's reader audience (see [ADR-001](adr/ADR-001-two-phase-work-package-migration.md)). A changelog event that touches a worklog (`WorklogId`, `WorklogTimeSpent`, together with the time-tracking totals it changes) stays out of this value, because a worklog can have its own visibility; the operator archive keeps it. When an issue later gains a security level or keeps no change, a re-run removes its stored value and the copies of that value in the work package's journals.

The selection is the one `j2o-history` uses (`src/infrastructure/jira/field_history.py`). Recognized built-in fields, supported Agile and Tempo fields and reviewed standard custom fields are kept. Comment changes and every event or item that carries audience or visibility metadata are withheld.

The stage reads each configured project through Jira's empty-security-level search with `expand=changelog`. Only mapped issues that this search returns are considered; an issue with a security level is skipped. The work-package mapping is a cache: a work package in the mapped project that holds a stored value but has no mapping entry is found through its `J2O Origin ID` and `J2O Origin Key` and checked like a mapped one. A blank stored value is ignored. When the work package has no usable origin key, or another work package already targets its issue, its stored value is removed; so is the value of one whose origin key belongs to another Jira project, because that project's search never returns it. A stored value whose work package has no `J2O Origin ID` cannot be confirmed: it counts as `clear_unconfirmed` and has to be removed by hand. Customer-facing project types (anything other than software and business) are skipped and need a separate access check. An issue's changelog must be one complete page (`startAt` 0, every history present once, `isLast` not false); otherwise the issue is skipped and counted as incomplete.

The stored value is canonical JSON (sorted keys, no whitespace, UTF-8) of `{"jiraIssueId": "<id>", "events": [{"id", "created", "author", "changes": [{"position", "field", "fieldId", "fieldtype", "from", "fromString", "to", "toString"}]}]}`. `position` is the item's index in the original Jira event. An issue with no kept change gets no value.

Before writing, the stage reads the stored values and the `J2O Origin ID` of each target. A work package whose origin differs from the Jira issue, or that is not in the mapped project, is skipped; when its stored value was due for removal, the skip counts as `clear_unconfirmed` and makes the result unsuccessful. A value is written only when it differs from the stored one, so a repeat run writes nothing. The write enables the field for the work package's project and type and stores the custom value directly, as `customfields_generic` does, without saving the work package.

The result's `details` report `issues_seen`, `issues_written`, `cleared` (stored values removed), `unchanged`, `no_kept_changes`, `incomplete`, `restricted_skipped`, `not_returned` (mapped issues the empty-security-level search did not return), `unsupported_project_type`, `target_unconfirmed`, `failed`, `clear_unconfirmed` (stored values due for removal whose removal was not confirmed), `source_changes`, `kept_changes` and `withheld_changes`. The result is unsuccessful when `failed` or `clear_unconfirmed` is above zero.

Standard Jira text and select custom fields (`com.atlassian.jira.plugin.system.customfieldtypes:*`) are withheld unless reviewed. List each reviewed field in `migration.history_approved_field_ids` in `config/config.yaml`, as a list of `customfield_<number>` IDs or one comma-separated string. The default is empty. It has the same effect as `j2o-history seal --field-id`. Review a free-text field before approving it.

**Implementation:** `src/application/components/work_package_field_history_migration.py`

**Depends on:** work package mappings and the `J2O Origin ID` provenance field (`work_packages_skeleton`, `wp_metadata_backfill`)

---

## Migration Execution

### Migration Profiles

Pre-defined component groups:

**Default and Full Profile**:
```bash
uv run python -m src.main migrate --no-confirm
uv run python -m src.main migrate --profile full --no-confirm
# Both run DEFAULT_COMPONENT_SEQUENCE (src/application/components/registry.py):
# the two-phase work_packages_skeleton -> attachments -> work_packages_content
# flow, then metadata, field history, relations, watchers, time entries and
# finalization. The legacy work_packages component is not part of it.
```

### Individual Components

```bash
# Single component
uv run python -m src.main migrate --components users --no-confirm

# Multiple components
uv run python -m src.main migrate --components users,projects,work_packages --no-confirm
```

### Dry Run Mode

```bash
uv run python -m src.main migrate --dry-run --components users --no-confirm
```

---

## Component Dependencies

### Dependency Graph

```
UserMigration (foundation)
    ├── GroupMigration
    ├── ProjectMigration
    │   ├── StatusMigration
    │   ├── PriorityMigration
    │   ├── IssueTypeMigration
    │   ├── CustomFieldMigration
    │   └── WorkPackageMigration
    │       ├── AttachmentsMigration
    │       │   └── AttachmentProvenanceMigration
    │       ├── RelationMigration
    │       ├── WatcherMigration
    │       ├── TimeEntryMigration
    │       ├── SprintEpicMigration
    │       │   └── AgileBoardMigration
    │       ├── NativeBoardsMigration
    │       ├── VotesMigration
    │       ├── LabelsMigration
    │       ├── InlineRefsMigration
    │       ├── EstimatesMigration
    │       └── StoryPointsMigration
    ├── WorkflowMigration
    ├── AdminSchemeMigration
    └── ReportingMigration

CompanyMigration (Tempo)
    └── AccountMigration
        └── TempoAccountMigration
```

### Recommended Order

1. **Foundation**: users
2. **Configuration**: status, priority, issue_types, custom_fields
3. **Structure**: projects, components, versions
4. **Content**: work_packages, attachments
5. **Relationships**: relations, watchers, links
6. **Time Tracking**: time_entries
7. **Agile**: sprints, boards
8. **Workflow**: workflow, groups, admin_schemes
9. **Reporting**: reporting

---

## Post-Migration Validation

### Data QA Script

```bash
uv run --active --no-cache python scripts/data_qa.py --projects <KEY>
```

**Checks**:
- Issue/work package count matching
- Attachment verification
- Start date coverage
- Project module configuration

### Manual Verification

```bash
# Check mapping files
ls var/data/*_mapping.json

# Review migration logs
ls var/logs/migration_*.log

# Inspect results
cat var/results/migration_summary_*.json
```

---

## Troubleshooting

### Common Issues

**Checkpoint Corruption**:
```bash
# Reset work package checkpoints
uv run python -m src.main migrate --reset-wp-checkpoints --components work_packages
```

**Missing Dependencies**:
- Verify migration order follows dependency graph
- Check mapping files exist for dependent entities

**Rails Console Errors**:
- Review `var/logs/rails_console_*.log`
- Check JSON payload sanitization (no `_links`)
- Verify required ActiveRecord attributes present

### Debug Mode

```bash
uv run python -m src.main migrate --debug --components <component> --limit 10
```

---

## Related Documentation

- **[Client API Reference](CLIENT_API.md)**: Client layer details
- **[Architecture Overview](ARCHITECTURE.md)**: System design
- **[Developer Guide](DEVELOPER_GUIDE.md)**: Development standards
- **[Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)**: Workflow configuration
- **[Security Guidelines](SECURITY.md)**: Security best practices

---

## Support

For component-specific issues:
1. Review component source in `src/migrations/`
2. Check related tests in `tests/unit/migrations/`
3. Consult [Developer Guide](DEVELOPER_GUIDE.md)
4. Run data QA validation script
