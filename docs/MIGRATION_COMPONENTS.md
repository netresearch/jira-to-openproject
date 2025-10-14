# Migration Components Catalog

**Version**: 1.0
**Last Updated**: 2025-10-14

## Overview

The j2o migration tool consists of 40+ specialized migration components, each handling a specific aspect of data migration from Jira to OpenProject. All components inherit from `BaseMigration` and follow the extract → map → load pipeline pattern.

**Related Documentation**:
- [Client API Reference](CLIENT_API.md)
- [Architecture Overview](ARCHITECTURE.md)
- [Developer Guide](DEVELOPER_GUIDE.md)
- [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)

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

**Dependencies**: UserMigration (users must exist)

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

### WorkPackageMigration

**Location**: `src/migrations/work_package_migration.py`

Migrates Jira issues to OpenProject work packages with full metadata.

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

**Location**: `src/migrations/sprint_epic_migration.py`

Migrates Jira sprints and epics.

**Features**:
- Sprint → Version mapping
- Epic → Work package type
- Sprint dates and goals
- Epic hierarchies

**Dependencies**: ProjectMigration, WorkPackageMigration

**Related**: AgileBoardMigration

---

### AgileBoardMigration

**Location**: `src/migrations/agile_board_migration.py`

Migrates Jira agile boards to OpenProject saved queries.

**Features**:
- Board → Saved query mapping
- Sprint filters
- Column configuration
- Quick filters

**Dependencies**: ProjectMigration, SprintEpicMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components agile_boards --no-confirm
```

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

**Location**: `src/migrations/components_migration.py`

Migrates Jira components to OpenProject custom field values.

**Features**:
- Component → Custom field mapping
- Component lead assignment
- Multi-component support

**Dependencies**: ProjectMigration, CustomFieldMigration

---

### VersionsMigration

**Location**: `src/migrations/versions_migration.py`

Migrates Jira versions to OpenProject versions.

**Features**:
- Version creation with dates
- Release/archive status
- Version description

**Dependencies**: ProjectMigration

---

### AffectsVersionsMigration

**Location**: `src/migrations/affects_versions_migration.py`

Associates work packages with affected versions.

**Features**:
- Fix version mapping
- Affects version association
- Version timeline

**Dependencies**: WorkPackageMigration, VersionsMigration

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

Handles Jira security level mapping.

**Features**:
- Security level → Custom field
- Access restriction notes
- Visibility tracking

**Dependencies**: CustomFieldMigration

---

### CategoryDefaultsMigration

**Location**: `src/migrations/category_defaults_migration.py`

Applies category-based defaults to work packages.

**Features**:
- Default assignee per category
- Default type per category
- Bulk updates

**Dependencies**: WorkPackageMigration, ProjectMigration

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

**Location**: `src/migrations/simpletasks_migration.py`

Handles simple task items from Jira.

**Features**:
- Task checklist items
- Completion status
- Task assignment

**Dependencies**: WorkPackageMigration

---

## Migration Execution

### Migration Profiles

Pre-defined component groups:

**Default Profile**:
```bash
uv run python -m src.main migrate --no-confirm
# Runs: users, groups, projects, work_packages, attachments
```

**Full Profile**:
```bash
uv run python -m src.main migrate --profile full --no-confirm
# Includes: workflows, agile boards, admin schemes, reporting, all entities
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

1. **Foundation**: users, groups
2. **Configuration**: status, priority, issue_types, custom_fields
3. **Structure**: projects, components, versions
4. **Content**: work_packages, attachments
5. **Relationships**: relations, watchers, links
6. **Time Tracking**: time_entries
7. **Agile**: sprints, boards
8. **Workflow**: workflow, admin_schemes
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
