# Migration Safety Patterns & Recovery Mechanisms

**Owner**: j2o-7 (P1)
**Last Updated**: 2025-10-17
**Status**: Active

## Overview

This document explains the safety patterns, recovery mechanisms, and rehearsal workflows built into the j2o migration system. These mechanisms ensure safe, idempotent migrations with clear recovery paths when issues arise.

## Table of Contents

1. [Rehearsal Workflow](#rehearsal-workflow)
2. [Reset Mechanisms](#reset-mechanisms)
3. [Idempotent Re-run Pattern](#idempotent-re-run-pattern)
4. [Recovery Strategies](#recovery-strategies)
5. [Why NOT Rollback](#why-not-rollback)
6. [Checkpoint System](#checkpoint-system)
7. [Change Detection System](#change-detection-system)

---

## 1. Rehearsal Workflow

The rehearsal workflow enables safe testing of migration configurations before production runs. It follows a "snapshot → test → restore → fix → repeat" cycle.

### Workflow Steps

```
1. Create OpenProject Snapshot
   ↓
2. Run Migration (test configuration)
   ↓
3. Validate Results (inspect work packages, users, etc.)
   ↓
4. Restore OpenProject Snapshot (rollback to clean state)
   ↓
5. Fix Configuration/Code
   ↓
6. Repeat until satisfied
```

### Creating Snapshots

**Database Snapshot** (via Docker):
```bash
# Create snapshot before migration
docker-compose exec postgres pg_dump -U openproject openproject > var/backups/pre-migration-$(date +%Y%m%d-%H%M%S).sql

# Or use the built-in backup mechanism
docker-compose exec openproject backup:database:create
```

**File System Snapshot**:
```bash
# Backup OpenProject attachments and data
docker-compose exec openproject backup:files:create

# Or manual backup
tar -czf var/backups/openproject-files-$(date +%Y%m%d-%H%M%S).tar.gz /var/openproject
```

### Restoring Snapshots

**Database Restore**:
```bash
# Stop OpenProject services
docker-compose stop openproject

# Restore database
docker-compose exec postgres psql -U openproject -d openproject < var/backups/pre-migration-20251017-143000.sql

# Restart services
docker-compose start openproject
```

**File Restore**:
```bash
# Restore files
tar -xzf var/backups/openproject-files-20251017-143000.tar.gz -C /
```

### Rehearsal Best Practices

1. **Always snapshot before first migration attempt**
   - Database snapshot captures all OpenProject data
   - File snapshot captures attachments
   - Both are needed for complete restoration

2. **Test incrementally**
   - Start with small project subset (use `--jira-project-filter`)
   - Validate each component before proceeding
   - Use `--dry-run` for validation without writes

3. **Document each iteration**
   - Record what configuration was tested
   - Note which issues were found
   - Track fixes applied between iterations

4. **Verify restoration**
   - After restore, verify OpenProject state
   - Check entity counts match pre-migration
   - Validate no orphaned references

---

## 2. Reset Mechanisms

The system provides several reset mechanisms to handle different failure scenarios and restart needs.

### 2.1 Work Package Checkpoint Reset

**Flag**: `--reset-wp-checkpoints`

**Purpose**: Clear the work package fast-forward checkpoint store before running migrations.

**When to Use**:
- After data corruption in checkpoint storage
- When restarting work package migration from scratch
- After changing work package migration logic that invalidates existing checkpoints
- When checkpoint data becomes inconsistent with actual OpenProject state

**Implementation** (src/main.py:194-196):
```python
migrate_parser.add_argument(
    "--reset-wp-checkpoints",
    action="store_true",
    help="Clear the work package fast-forward checkpoint store before running",
)
```

**Location**: Checkpoint files stored in `var/state/checkpoints/`

**Example Usage**:
```bash
# Reset work package checkpoints and re-run migration
python -m src.main migrate --components work_packages --reset-wp-checkpoints
```

**Effect**:
- Removes all checkpoint files from `var/state/checkpoints/active/`
- Removes all checkpoint files from `var/state/checkpoints/completed/`
- Removes all checkpoint files from `var/state/checkpoints/failed/`
- Work package migration will start from the beginning
- Previous progress is lost but can be rebuilt idempotently

### 2.2 Mapping File Reset

**Purpose**: Clear mapping files to restart migration with fresh ID mappings.

**When to Use**:
- After deleting entities in OpenProject (e.g., test projects)
- When mapping files contain stale or incorrect IDs
- After major configuration changes requiring fresh mappings
- When migrating to a new OpenProject instance

**Manual Process**:
```bash
# Backup existing mappings (recommended)
cp -r var/data/mappings var/backups/mappings-$(date +%Y%m%d-%H%M%S)

# Clear specific mapping type
rm var/data/mappings/project.json

# Or clear all mappings
rm var/data/mappings/*.json
```

**Effect**:
- Next migration run will create fresh mappings
- Jira entities will be re-created in OpenProject with new IDs
- Existing OpenProject entities are NOT deleted
- May result in duplicate entities if not careful

**WARNING**: Only clear mappings if you've restored OpenProject to clean state or created new instance.

### 2.3 Cache Invalidation

**Purpose**: Force fresh data fetch from Jira/OpenProject APIs.

**Flag**: `--force`

**Implementation** (src/main.py:170-176):
```python
migrate_parser.add_argument(
    "--force",
    action="store_true",
    help=(
        "Force fresh extraction and mapping re-generation (skip disk caches). "
        "Does not force re-writing into OpenProject; keeps in-run in-memory caches; "
        "also overrides pre-migration validation/security gating."
    ),
)
```

**Effect**:
- Skips disk-cached entity data
- Forces fresh API calls to Jira/OpenProject
- Keeps in-memory caches during run (for performance)
- Does NOT clear mapping files
- Overrides validation gates

**When to Use**:
- After making changes in Jira that need immediate migration
- When cached data is suspected to be stale
- During development/debugging
- When troubleshooting data inconsistencies

### 2.4 Full Database Rebuild

**Purpose**: Complete reset of OpenProject for clean migration.

**When to Use**:
- After catastrophic data corruption
- When starting fresh migration to clean instance
- During testing/development cycles
- After major migration logic changes

**Process**:
```bash
# 1. Stop OpenProject
docker-compose stop openproject

# 2. Drop and recreate database
docker-compose exec postgres psql -U postgres -c "DROP DATABASE openproject;"
docker-compose exec postgres psql -U postgres -c "CREATE DATABASE openproject OWNER openproject;"

# 3. Run OpenProject migrations
docker-compose exec openproject rake db:migrate

# 4. Clear all j2o mappings and checkpoints
rm -rf var/data/mappings/*.json
rm -rf var/state/checkpoints/*/*.json

# 5. Restart OpenProject
docker-compose start openproject

# 6. Run migration from scratch
python -m src.main migrate
```

---

## 3. Idempotent Re-run Pattern

The migration system is designed to be **idempotent** - running the same migration multiple times produces the same result. This is the **preferred** recovery mechanism.

### How It Works

The system uses **change detection** and **entity snapshots** to track what has been migrated:

1. **Before Migration**: System fetches current Jira entities
2. **Change Detection**: Compares current entities with previous snapshot
3. **Selective Migration**: Only processes entities that have changed
4. **After Migration**: Creates new snapshot for next run

**Implementation** (src/utils/change_detector.py):
```python
class ChangeDetector:
    """Detects changes in Jira entities for idempotent migration operations.

    This class provides functionality to:
    - Store snapshots of Jira entities after successful migrations
    - Compare current entity state with stored snapshots
    - Identify created, updated, and deleted entities
    - Generate prioritized change reports
    """
```

### Why Fix-Forward Works

The idempotent pattern **preserves entity IDs and relationships**:

- **Users**: OpenProject user IDs remain stable across re-runs
- **Projects**: OpenProject project IDs remain stable
- **Work Packages**: OpenProject work package IDs remain stable
- **Relationships**: Parent-child links, cross-references preserved
- **Custom Fields**: Field definitions and values remain consistent

**Example Scenario**:
```
1. Initial migration creates:
   - User #1 (Jira: john.doe) → OpenProject User #42
   - Project #1 (Jira: MYPROJECT) → OpenProject Project #10
   - Work Package #1 (Jira: MYPROJECT-123) → OpenProject WP #100

2. Fix bug in description formatting code

3. Re-run migration:
   - User #1: No changes → Skip
   - Project #1: No changes → Skip
   - Work Package #1: Description differs → Update WP #100 (same ID!)

Result: Work package #100 now has correct description, all IDs preserved
```

### Common Fix-Forward Scenarios

#### Scenario 1: Data Quality Issues

**Problem**: Work package descriptions missing formatting
**Fix**: Update description template in migration code
**Recovery**: Re-run work_packages component with `--force`

```bash
# 1. Fix code in src/migrations/work_package_migration.py
# 2. Re-run migration
python -m src.main migrate --components work_packages --force
```

**Result**: Existing work packages updated with correct formatting, IDs preserved

#### Scenario 2: Missing Custom Field Values

**Problem**: Custom field mapping was incorrect
**Fix**: Update custom field mapping in configuration
**Recovery**: Re-run custom_fields and work_packages components

```bash
# 1. Fix mapping in config.yaml
# 2. Re-run migration
python -m src.main migrate --components custom_fields,work_packages --force
```

**Result**: Work packages updated with correct custom field values

#### Scenario 3: User Assignment Issues

**Problem**: Some users not assigned correctly to work packages
**Fix**: Update user mapping resolution logic
**Recovery**: Re-run work_packages component

```bash
# 1. Fix user resolution in src/migrations/work_package_migration.py
# 2. Re-run migration
python -m src.main migrate --components work_packages
```

**Result**: Work packages re-assigned to correct users

### Change Detection Details

**Snapshot Location**: `var/snapshots/`

**Directory Structure**:
```
var/snapshots/
├── current/               # Pointers to latest snapshots
│   ├── users.json
│   ├── projects.json
│   └── work_packages.json
└── archive/               # Historical snapshots
    ├── users_2025-10-17T14:30:00.json
    ├── projects_2025-10-17T14:35:00.json
    └── work_packages_2025-10-17T15:00:00.json
```

**Checksum Calculation** (src/utils/change_detector.py:80-114):
```python
def _calculate_entity_checksum(self, entity_data: dict[str, Any]) -> str:
    """Calculate SHA256 checksum of entity data.

    Ignores volatile fields like:
    - self (API URL)
    - lastViewed
    - expand
    - renderedFields
    - transitions
    - operations
    - editmeta
    """
```

**Change Types Detected**:
- **Created**: Entity exists in Jira but not in last snapshot
- **Updated**: Entity exists in both but checksum differs
- **Deleted**: Entity exists in snapshot but not in current Jira fetch

**Priority Calculation** (src/utils/change_detector.py:403-453):
```python
base_priority = {
    "projects": 9,        # High priority - affects everything
    "users": 8,           # High priority - affects assignments
    "customfields": 7,    # Medium-high - affects data structure
    "issuetypes": 6,      # Medium - affects work packages
    "statuses": 6,        # Medium - affects workflows
    "issues": 5,          # Medium - core content
    "worklogs": 4,        # Medium-low - time tracking
    "comments": 3,        # Lower - additional content
    "attachments": 3,     # Lower - files
}
```

---

## 4. Recovery Strategies

Decision tree for operators to determine the appropriate recovery strategy based on failure type.

### Decision Tree

```
┌─────────────────────────┐
│  Migration Failed?      │
└────────────┬────────────┘
             │
             ▼
      ┌─────────────┐
      │ What failed? │
      └──────┬──────┘
             │
    ┌────────┴────────┐
    │                 │
    ▼                 ▼
┌────────┐      ┌──────────┐
│ Data   │      │ System   │
│ Quality│      │ Failure  │
└───┬────┘      └────┬─────┘
    │                │
    ▼                ▼
┌─────────────┐  ┌───────────────┐
│ Fix-Forward │  │ Restore Backup │
└─────────────┘  └───────────────┘
```

### Strategy 1: Fix-Forward (Preferred)

**When to Use**:
- Data quality issues (wrong formatting, missing values)
- Logic bugs in transformation code
- Configuration errors (wrong mappings)
- Missing or incorrect custom field values
- User assignment errors
- Description/comment formatting issues

**Characteristics**:
- ✅ Preserves all entity IDs
- ✅ Preserves relationships
- ✅ Can be applied incrementally
- ✅ Maintains user modifications made post-migration
- ✅ Fast (only processes changed entities)

**Steps**:
1. Identify root cause of data quality issue
2. Fix code or configuration
3. Re-run affected component with `--force` if needed
4. Verify fixes applied correctly

**Example**:
```bash
# Problem: Work package descriptions missing HTML formatting
# Fix: Update description transformation in work_package_migration.py
# Recovery:
python -m src.main migrate --components work_packages --force
```

### Strategy 2: Reset Checkpoints

**When to Use**:
- Checkpoint data corruption
- Checkpoint storage inconsistent with OpenProject state
- Need to restart long-running migration from beginning
- After changing migration logic that invalidates checkpoints

**Characteristics**:
- ⚠️ Loses migration progress
- ✅ Can resume from beginning
- ✅ Idempotent - safe to restart
- ⚠️ May take longer (re-processes all entities)

**Steps**:
1. Backup current state (optional but recommended)
2. Run migration with `--reset-wp-checkpoints`
3. Monitor progress from beginning

**Example**:
```bash
# Problem: Checkpoint files corrupted
# Recovery:
python -m src.main migrate --components work_packages --reset-wp-checkpoints
```

### Strategy 3: Clear Mappings

**When to Use**:
- Migrating to new/clean OpenProject instance
- After deleting test data from OpenProject
- Mapping files contain incorrect/stale IDs
- Major reconfiguration requiring fresh start

**Characteristics**:
- ⚠️ Loses all ID mappings
- ⚠️ May create duplicate entities if OpenProject not clean
- ⚠️ User modifications lost (entities re-created)
- ⚠️ Relationships may break if partial clear

**Steps**:
1. **CRITICAL**: Ensure OpenProject is in clean state (restored from backup or fresh instance)
2. Backup existing mappings
3. Remove mapping files
4. Re-run migration from beginning

**Example**:
```bash
# Problem: Migrating to new OpenProject instance
# Recovery:
cp -r var/data/mappings var/backups/mappings-backup
rm var/data/mappings/*.json
python -m src.main migrate
```

### Strategy 4: Restore from Backup (Last Resort)

**When to Use**:
- Catastrophic data corruption in OpenProject
- Critical system errors during migration
- Unrecoverable state (e.g., deleted required entities)
- Need to revert all changes to pre-migration state

**Characteristics**:
- ✅ Complete restoration to known-good state
- ✅ Guaranteed consistency
- ❌ Loses ALL changes since backup (including user modifications)
- ❌ Time-consuming
- ❌ Requires downtime

**Steps**:
1. Stop OpenProject services
2. Restore database from snapshot
3. Restore file system from snapshot
4. Restart OpenProject services
5. Verify restoration successful
6. Fix root cause
7. Re-run migration

**Example**:
```bash
# Problem: Critical data corruption in OpenProject
# Recovery:
docker-compose stop openproject
docker-compose exec postgres psql -U openproject -d openproject < var/backups/pre-migration-20251017.sql
tar -xzf var/backups/openproject-files-20251017.tar.gz -C /
docker-compose start openproject

# Verify restoration
docker-compose exec openproject rails console
> Project.count  # Should match pre-migration count

# Fix root cause, then re-run
python -m src.main migrate
```

### Recovery Decision Matrix

| Failure Type | Recommended Strategy | Alternative |
|--------------|---------------------|-------------|
| Wrong formatting in descriptions | Fix-Forward | - |
| Missing custom field values | Fix-Forward | - |
| User assignment errors | Fix-Forward | - |
| Configuration error (wrong mapping) | Fix-Forward | - |
| Checkpoint corruption | Reset Checkpoints | Clear Mappings |
| Stale mapping IDs | Clear Mappings | Restore Backup |
| New OpenProject instance | Clear Mappings | - |
| Critical data corruption | Restore Backup | - |
| Unrecoverable state | Restore Backup | - |

---

## 5. Why NOT Rollback

The migration system **explicitly avoids** delete-based rollback mechanisms. Here's why:

### 5.1 Cross-System Migration Has No Distributed Transactions

**Problem**: Jira and OpenProject are separate systems with independent databases.

**Why Rollback Fails**:
- Cannot create atomic transaction across both systems
- If rollback deletes OpenProject entities, Jira entities remain
- No way to guarantee both systems stay synchronized
- Partial failures leave inconsistent state

**Example of Failure**:
```
1. Migration creates User #42 in OpenProject (from Jira user john.doe)
2. Migration creates Project #10 in OpenProject (owned by User #42)
3. Migration fails at Work Package #100
4. Rollback attempts to delete User #42 and Project #10
5. Meanwhile, admin has assigned User #42 to Project #11 in OpenProject
6. Rollback deletion fails due to foreign key constraint
7. System now in undefined state
```

### 5.2 Preserving Entity IDs is Critical

**Why IDs Matter**:
- External systems may reference OpenProject entities by ID
- Email notifications contain entity URLs with IDs
- Webhooks and integrations rely on stable IDs
- User bookmarks break if IDs change

**Delete-Based Rollback Problem**:
```
1. Migration creates Work Package #100 (JIRA-123)
2. User bookmarks: https://openproject.example.com/work_packages/100
3. Email sent: "You were assigned to WP #100"
4. Rollback deletes WP #100
5. Re-migration creates WP #101 (same JIRA-123)
6. User's bookmark and email now point to non-existent WP #100
```

**Fix-Forward Solution**:
```
1. Migration creates Work Package #100 (JIRA-123)
2. User bookmarks: https://openproject.example.com/work_packages/100
3. Email sent: "You were assigned to WP #100"
4. Bug found in description formatting
5. Fix-forward updates WP #100 with correct description
6. User's bookmark and email still valid - points to WP #100 ✓
```

### 5.3 User Modifications Would Be Lost

**Problem**: After migration, users begin working in OpenProject.

**User Activities**:
- Creating new work packages
- Adding comments to migrated work packages
- Modifying work package descriptions/fields
- Creating relationships between work packages
- Uploading attachments
- Time logging
- Status transitions

**Delete-Based Rollback Impact**:
```
Timeline:
T0: Migration creates WP #100 (from JIRA-123)
T1: User adds comment to WP #100: "This is urgent!"
T2: User creates new WP #101 as child of WP #100
T3: Migration bug discovered
T4: Rollback deletes WP #100
    → User's comment on WP #100: LOST ❌
    → User's WP #101: ORPHANED (parent deleted) ❌
    → User frustrated: data loss, work wasted ❌
```

**Fix-Forward Impact**:
```
Timeline:
T0: Migration creates WP #100 (from JIRA-123)
T1: User adds comment to WP #100: "This is urgent!"
T2: User creates new WP #101 as child of WP #100
T3: Migration bug discovered
T4: Fix-forward updates WP #100 description
    → User's comment on WP #100: PRESERVED ✓
    → User's WP #101: STILL CHILD OF WP #100 ✓
    → User happy: their work preserved ✓
```

### 5.4 Relationship Complexity

**Problem**: OpenProject entities have complex relationships.

**Relationship Types**:
- Projects → Work Packages (one-to-many)
- Work Packages → Users (assignee, author, watchers)
- Work Packages → Work Packages (parent-child, related)
- Work Packages → Custom Fields (many-to-many)
- Work Packages → Comments (one-to-many)
- Work Packages → Attachments (one-to-many)
- Work Packages → Time Entries (one-to-many)
- Users → Roles → Projects (many-to-many-to-many)

**Delete Cascading Issues**:
```
Delete User #42 triggers cascading deletes:
├─ User #42's created Work Packages (author foreign key)
├─ User #42's assigned Work Packages (assignee foreign key)
├─ User #42's time entries
├─ User #42's comments
└─ User #42's role assignments

This may delete hundreds of entities in cascade!
```

### 5.5 The Right Pattern: Idempotent Re-runs

**Instead of Rollback, Use**:

1. **Snapshots** for catastrophic failures (external to OpenProject)
2. **Fix-Forward** for data quality issues (within OpenProject)
3. **Idempotent Re-runs** to apply fixes safely
4. **Change Detection** to minimize re-processing

**Benefits**:
- ✅ Preserves entity IDs
- ✅ Preserves relationships
- ✅ Preserves user modifications
- ✅ Allows incremental fixes
- ✅ Safe to run multiple times
- ✅ No risk of data loss from cascading deletes

**Mental Model**:
```
Traditional (WRONG):
    Create → Fail → Delete → Fix → Create (new IDs!)

Idempotent (CORRECT):
    Create → Fail → Fix → Update (same IDs!)
```

---

## 6. Checkpoint System

The checkpoint system provides fine-grained progress tracking and resume capabilities for long-running migrations.

### 6.1 Architecture

**Location**: `src/utils/checkpoint_manager.py`

**Storage**: `var/state/checkpoints/`

**Directory Structure**:
```
var/state/checkpoints/
├── active/              # Currently in-progress checkpoints
│   ├── abc123.json
│   └── def456.json
├── completed/           # Successfully completed checkpoints
│   ├── ghi789.json
│   └── jkl012.json
├── failed/              # Failed checkpoints
│   ├── mno345.json
│   └── pqr678.json
└── recovery_plans/      # Recovery plan metadata
    ├── stu901.json
    └── vwx234.json
```

### 6.2 Checkpoint States

```python
class CheckpointStatus(Enum):
    PENDING = "pending"        # Created but not started
    IN_PROGRESS = "in_progress"  # Currently executing
    COMPLETED = "completed"      # Successfully finished
    FAILED = "failed"            # Encountered error
    ROLLED_BACK = "rolled_back"  # Reverted to previous state
```

### 6.3 Checkpoint Structure

```python
class ProgressCheckpoint(TypedDict):
    checkpoint_id: str               # Unique ID
    migration_record_id: str         # Migration run ID
    step_name: str                   # E.g., "extract_users"
    step_description: str            # Human-readable description
    status: str                      # CheckpointStatus
    created_at: str                  # ISO timestamp
    completed_at: str | None         # ISO timestamp
    failed_at: str | None            # ISO timestamp
    progress_percentage: float       # 0.0 - 100.0
    entities_processed: int          # Count of entities processed
    entities_total: int              # Total entities to process
    current_entity_id: str | None    # Currently processing
    current_entity_type: str | None  # Type of current entity
    data_snapshot: dict              # Minimal state for resume
    metadata: dict                   # Additional info
```

### 6.4 Creating Checkpoints

**Usage Example**:
```python
from src.utils.checkpoint_manager import CheckpointManager

manager = CheckpointManager()

# Create checkpoint
checkpoint_id = manager.create_checkpoint(
    migration_record_id="migration-123",
    step_name="extract_work_packages",
    step_description="Fetching work packages from Jira",
    entities_processed=0,
    entities_total=1000,
    metadata={"jira_project": "MYPROJECT"}
)

# Start checkpoint
manager.start_checkpoint(checkpoint_id)

# Process entities...
for i, entity in enumerate(entities):
    process(entity)
    if i % 100 == 0:
        manager.update_progress(
            migration_record_id="migration-123",
            current_step_progress=(i / 1000) * 100
        )

# Complete checkpoint
manager.complete_checkpoint(
    checkpoint_id=checkpoint_id,
    entities_processed=1000
)
```

### 6.5 Resume from Checkpoint

**Finding Resume Point**:
```python
# Check if migration can resume
can_resume = manager.can_resume_migration("migration-123")

if can_resume:
    resume_point = manager.get_resume_point("migration-123")

    # Resume from last successful checkpoint
    start_from_entity = resume_point["entities_processed"]
    snapshot_data = resume_point["data_snapshot"]
```

### 6.6 Recovery Actions

```python
class RecoveryAction(Enum):
    RETRY_FROM_CHECKPOINT = "retry_from_checkpoint"
    ROLLBACK_TO_CHECKPOINT = "rollback_to_checkpoint"
    SKIP_AND_CONTINUE = "skip_and_continue"
    ABORT_MIGRATION = "abort_migration"
    MANUAL_INTERVENTION = "manual_intervention"
```

**Automatic Recovery**:
```python
# Create recovery plan
plan_id = manager.create_recovery_plan(
    checkpoint_id=checkpoint_id,
    failure_type="network_error",
    error_message="Connection timeout to Jira API"
)

# System determines recommended action:
# - network_error → RETRY_FROM_CHECKPOINT
# - validation_error → SKIP_AND_CONTINUE or MANUAL_INTERVENTION
# - auth_error → MANUAL_INTERVENTION
# - system_error → ABORT_MIGRATION

# Execute recovery plan
success = manager.execute_recovery_plan(plan_id)
```

### 6.7 Progress Tracking

**Real-time Progress**:
```python
# Start tracking
manager.start_progress_tracking(
    migration_record_id="migration-123",
    total_steps=5,
    current_step="Extract users"
)

# Update progress
manager.update_progress(
    migration_record_id="migration-123",
    current_step="Map users",
    completed_steps=1,
    current_step_progress=50.0  # 50% through current step
)

# Get status
status = manager.get_progress_status("migration-123")
# Returns:
# {
#     "migration_record_id": "migration-123",
#     "total_steps": 5,
#     "completed_steps": 1,
#     "current_step": "Map users",
#     "current_step_progress": 50.0,
#     "overall_progress": 30.0,  # (1 + 0.5) / 5 * 100
#     "estimated_time_remaining": "15.3 minutes",
#     "throughput_per_minute": 0.2,
#     "status": "running"
# }
```

### 6.8 Cleanup

```python
# After successful migration
manager.cleanup_completed_migration("migration-123")

# Effect:
# - Removes from active progress tracking
# - Moves active checkpoints to completed directory
# - Preserves completed checkpoints for audit
```

---

## 7. Change Detection System

The change detection system enables incremental, idempotent migrations by tracking what has changed since the last run.

### 7.1 Architecture

**Location**: `src/utils/change_detector.py`

**Storage**: `var/snapshots/`

**Integration**: `src/migrations/base_migration.py` provides `run_with_change_detection()` method

### 7.2 Snapshot Structure

```python
class EntitySnapshot(TypedDict):
    entity_id: str               # Unique entity identifier
    entity_type: str             # E.g., "users", "projects", "issues"
    last_modified: str | None    # Last modified timestamp from Jira
    checksum: str                # SHA256 of entity data
    data: dict                   # Full entity data
    snapshot_timestamp: str      # When snapshot was created
```

**Snapshot File Format**:
```json
{
  "timestamp": "2025-10-17T14:30:00.123456+00:00",
  "entity_type": "work_packages",
  "migration_component": "WorkPackageMigration",
  "entity_count": 1234,
  "snapshots": [
    {
      "entity_id": "MYPROJECT-123",
      "entity_type": "work_packages",
      "last_modified": "2025-10-17T10:00:00Z",
      "checksum": "abc123...",
      "data": { ... },
      "snapshot_timestamp": "2025-10-17T14:30:00.123456+00:00"
    }
  ]
}
```

### 7.3 Change Detection Flow

```
1. Fetch current entities from Jira
   ↓
2. Load baseline snapshot (if exists)
   ↓
3. Compare current vs baseline:
   - Calculate checksums
   - Identify new entities (created)
   - Identify modified entities (updated)
   - Identify missing entities (deleted)
   ↓
4. Generate ChangeReport
   ↓
5. If changes detected:
   - Run migration
   - Create new snapshot
6. If no changes:
   - Skip migration
   - Return success
```

### 7.4 Change Report

```python
class ChangeReport(TypedDict):
    detection_timestamp: str              # When detection ran
    baseline_snapshot_timestamp: str | None  # Baseline timestamp
    total_changes: int                    # Count of all changes
    changes_by_type: dict[str, int]       # {"created": 10, "updated": 5}
    changes: list[EntityChange]           # Detailed change list
    summary: dict                         # Summary statistics
```

**Change Types**:
```python
class EntityChange(TypedDict):
    entity_id: str                  # Entity identifier
    entity_type: str                # Entity type
    change_type: str                # "created", "updated", "deleted"
    old_data: dict | None           # Previous state (None for created)
    new_data: dict | None           # Current state (None for deleted)
    priority: int                   # 1-10, higher = more important
```

### 7.5 Checksum Calculation

**Fields Ignored** (volatile fields that don't indicate real changes):
- `self` - API URL
- `lastViewed` - User viewing timestamp
- `expand` - API expansion flags
- `renderedFields` - Rendered HTML (changes with every fetch)
- `transitions` - Available transitions (depends on current state)
- `operations` - Available operations (depends on permissions)
- `editmeta` - Edit metadata (API-specific)

**Implementation**:
```python
def _calculate_entity_checksum(self, entity_data: dict[str, Any]) -> str:
    # Remove volatile fields
    normalized_data = entity_data.copy()
    for field in fields_to_ignore:
        normalized_data.pop(field, None)

    # Deterministic JSON with sorted keys
    normalized_json = json.dumps(
        normalized_data,
        sort_keys=True,
        separators=(",", ":")
    )

    # SHA256 hash
    return hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()
```

### 7.6 Usage in Migrations

**Base Migration Class** (src/migrations/base_migration.py:563-702):

```python
def run_with_change_detection(self, entity_type: str | None = None) -> ComponentResult:
    """Run migration with change detection support.

    1. Check for changes before migration
    2. Run migration if changes detected
    3. Create snapshot after successful migration
    """

    # Check if migration should be skipped
    should_skip, change_report = self.should_skip_migration(entity_type)

    if should_skip:
        return ComponentResult(
            success=True,
            message=f"No changes detected for {entity_type}, migration skipped",
            details={"change_report": change_report}
        )

    # Run migration
    result = self.run()

    # Create snapshot if successful
    if result.success:
        current_entities = get_cached_entities(entity_type)
        snapshot_path = self.create_snapshot(current_entities, entity_type)
        result.details["snapshot_created"] = str(snapshot_path)

    return result
```

**Example Migration**:
```python
@register_entity_types("users")
class UserMigration(BaseMigration):

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict]:
        """Fetch current users from Jira."""
        return self.jira_client.get_all_users()

    def run(self) -> ComponentResult:
        """Legacy run method - actual migration logic."""
        users = self.jira_client.get_all_users()
        # ... migration logic ...
        return ComponentResult(success=True)

# Usage:
migration = UserMigration()

# Run with change detection (recommended)
result = migration.run_with_change_detection(entity_type="users")
# → Only processes users that changed since last run

# Or legacy run (processes all users)
result = migration.run()
```

### 7.7 Cache Integration

Change detection integrates with **API call caching** to optimize performance:

**Caching Strategy**:
```python
# Thread-safe global cache
self._global_entity_cache: dict[str, list[dict]] = {}

# Cache stats tracking
self._cache_stats = {
    "hits": 0,
    "misses": 0,
    "evictions": 0,
    "memory_cleanups": 0,
    "total_size": 0
}
```

**Performance Gains**:
- **30-50% API call reduction** through caching
- **50-90% processing time reduction** through change detection
- **Memory-safe** with automatic cleanup at 80% threshold

**Example Performance**:
```
Without change detection:
- Fetch 10,000 work packages from Jira (60 seconds)
- Process all 10,000 work packages (120 seconds)
- Total: 180 seconds

With change detection (10 changed):
- Fetch 10,000 work packages from Jira (60 seconds) [cached]
- Compare 10,000 checksums (5 seconds)
- Process 10 changed work packages (1 second)
- Total: 6 seconds + cache hit

With change detection (no changes):
- Fetch from cache (instant)
- Compare checksums (5 seconds)
- Skip migration entirely
- Total: 5 seconds
```

### 7.8 Snapshot Cleanup

**Automatic Cleanup**:
```python
from src.utils.change_detector import ChangeDetector

detector = ChangeDetector()

# Clean snapshots older than 30 days
deleted_count = detector.cleanup_old_snapshots(keep_days=30)
# → Removes old snapshot files from var/snapshots/archive/
```

**Recommended Schedule**:
- Run cleanup weekly during maintenance window
- Keep last 30-60 days of snapshots for audit
- Archive critical snapshots to external storage before cleanup

---

## Appendix A: Command Reference

### Migration Commands

```bash
# Full migration
python -m src.main migrate

# Specific components
python -m src.main migrate --components users,projects,work_packages

# Force fresh data fetch
python -m src.main migrate --force

# Reset work package checkpoints
python -m src.main migrate --components work_packages --reset-wp-checkpoints

# Dry run (no writes to OpenProject)
python -m src.main migrate --dry-run

# Stop on first error
python -m src.main migrate --stop-on-error

# No confirmation prompts
python -m src.main migrate --no-confirm

# Filter to specific Jira projects
python -m src.main migrate --jira-project-filter MYPROJECT,OTHERPROJECT

# Use component profile
python -m src.main migrate --profile metadata_refresh
```

### Backup/Restore Commands

```bash
# Database backup
docker-compose exec postgres pg_dump -U openproject openproject > var/backups/backup-$(date +%Y%m%d-%H%M%S).sql

# Database restore
docker-compose exec postgres psql -U openproject -d openproject < var/backups/backup-20251017-143000.sql

# File backup
tar -czf var/backups/files-$(date +%Y%m%d-%H%M%S).tar.gz /var/openproject

# File restore
tar -xzf var/backups/files-20251017-143000.tar.gz -C /
```

### Diagnostic Commands

```bash
# Check checkpoint status
ls -lh var/state/checkpoints/active/
ls -lh var/state/checkpoints/completed/
ls -lh var/state/checkpoints/failed/

# Check snapshots
ls -lh var/snapshots/current/
ls -lh var/snapshots/archive/

# Check mappings
ls -lh var/data/mappings/

# Verify OpenProject entity counts
docker-compose exec openproject rails console
> User.count
> Project.count
> WorkPackage.count
```

### Cleanup Commands

```bash
# Remove checkpoint data
rm -rf var/state/checkpoints/active/*
rm -rf var/state/checkpoints/completed/*
rm -rf var/state/checkpoints/failed/*

# Clear mapping files
rm var/data/mappings/*.json

# Clean old snapshots
python -c "from src.utils.change_detector import ChangeDetector; ChangeDetector().cleanup_old_snapshots(keep_days=30)"
```

---

## Appendix B: Troubleshooting Guide

### Issue: "Another migration instance is running"

**Cause**: Singleton lock file exists from previous run

**Solutions**:
```bash
# Option 1: Remove stale lock
rm var/run/j2o_migrate.pid

# Option 2: Override lock (not recommended)
export J2O_DISABLE_LOCK=1
python -m src.main migrate
```

### Issue: "Checkpoint not found"

**Cause**: Checkpoint files corrupted or deleted

**Solution**:
```bash
# Reset checkpoints and restart
python -m src.main migrate --components work_packages --reset-wp-checkpoints
```

### Issue: "No changes detected but entities are different"

**Cause**: Snapshot checksum calculation ignoring relevant fields

**Solution**:
1. Review change_detector.py `_calculate_entity_checksum()` method
2. Ensure critical fields are NOT in `fields_to_ignore` list
3. Force fresh snapshot:
```bash
rm var/snapshots/current/work_packages.json
python -m src.main migrate --components work_packages --force
```

### Issue: "Duplicate entities created"

**Cause**: Mapping files cleared but OpenProject not restored

**Solution**:
```bash
# Option 1: Restore OpenProject from backup
docker-compose exec postgres psql -U openproject -d openproject < var/backups/pre-migration.sql

# Option 2: Manual cleanup (if few duplicates)
# Use OpenProject UI or rails console to delete duplicates
```

### Issue: "Foreign key constraint violation"

**Cause**: Attempting to delete entity with relationships

**Solution**:
- **DO NOT** attempt delete-based cleanup
- Use fix-forward pattern instead
- If necessary, restore from backup

---

## Appendix C: Best Practices Summary

### DO ✅

1. **Always snapshot before first migration**
   - Database snapshot
   - File system snapshot
   - Validate snapshot integrity

2. **Use fix-forward for data issues**
   - Update transformation code
   - Re-run affected component
   - Preserve entity IDs and relationships

3. **Test with small project subset**
   - Use `--jira-project-filter`
   - Validate before full migration
   - Iterate on configuration

4. **Monitor progress and logs**
   - Watch checkpoint creation
   - Review change detection reports
   - Track API call performance

5. **Keep snapshots for audit**
   - Archive critical snapshots
   - Maintain 30-60 days history
   - Document snapshot metadata

### DON'T ❌

1. **Don't use delete-based rollback**
   - No way to guarantee consistency
   - Loses user modifications
   - Breaks external references
   - Risk of cascading deletes

2. **Don't clear mappings without clean OpenProject**
   - Will create duplicate entities
   - Breaks relationships
   - Causes ID mismatches

3. **Don't skip validation after restore**
   - Always verify entity counts
   - Check relationships intact
   - Confirm API accessibility

4. **Don't disable safety mechanisms in production**
   - Keep singleton lock enabled
   - Don't skip change detection
   - Don't disable validation gates

5. **Don't ignore checkpoint failures**
   - Investigate root cause
   - Create recovery plan
   - Document lessons learned

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-10-17 | j2o-7 | Initial comprehensive documentation |

---

## Related Documentation

- [AGENTS.tasks.md](../AGENTS.tasks.md) - Workflow and task management
- [README.md](../README.md) - System overview
- [src/utils/checkpoint_manager.py](../src/utils/checkpoint_manager.py) - Checkpoint implementation
- [src/utils/change_detector.py](../src/utils/change_detector.py) - Change detection implementation
- [src/migrations/base_migration.py](../src/migrations/base_migration.py) - Base migration with idempotent support

---

**END OF DOCUMENT**
