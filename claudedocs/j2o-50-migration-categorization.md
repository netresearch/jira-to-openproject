# j2o-50: Migration Categorization by Complexity Tier

**Task**: j2o-51
**Date**: 2025-10-15
**Purpose**: Categorize all 37 migrations by implementation complexity for Option B rollout

## Summary Statistics

- **Total Migrations**: 37
- **Already Complete** (have `_get_current_entities_for_type()`): 4
- **Tier 1 (Simple)**: 24 migrations
- **Tier 2 (Medium)**: 5 migrations
- **Tier 3 (Complex/Special)**: 4 migrations

## Methodology

Categorization based on:
1. **API Call Count**: Number of `jira_client.get_*()` calls
2. **Data Complexity**: Single vs multiple entity types
3. **Processing Logic**: ETL complexity, nested structures
4. **Existing Implementation**: Already has required method

## Already Complete ✅ (4 migrations)

These migrations already implement `_get_current_entities_for_type()`:

| Migration | Entity Types | API Calls | Notes |
|-----------|--------------|-----------|-------|
| custom_field_migration.py | `custom_fields` | 1 | ✅ Complete implementation |
| project_migration.py | `projects` | 1 unique | ✅ Complete, called 2x |
| user_migration.py | `users`, `user_accounts` | 2 unique | ✅ Complete, called 3x |
| work_package_migration.py | `work_packages`, `issues` | 3 unique | ✅ Complex but complete, called 4x |

**Status**: No action required, use as reference implementations

---

## Tier 1: Simple Migrations (24 migrations)

### Tier 1A: Single API Call (9 migrations)

Straightforward implementations: single entity type, one API call pattern

| Migration | Entity Type | API Method | Effort |
|-----------|-------------|------------|--------|
| account_migration.py | `accounts`, `tempo_accounts` | get_tempo_accounts() | 20 min |
| category_defaults_migration.py | `category_defaults` | get_project_components() | 20 min |
| company_migration.py | `companies`, `tempo_companies` | get_tempo_companies() | 20 min |
| issue_type_migration.py | `issue_types`, `work_package_types` | get_issue_types() | 20 min |
| link_type_migration.py | `link_types`, `relation_types` | get_link_types() | 20 min |
| priority_migration.py | `priorities` | get_priorities() | 20 min |
| simpletasks_migration.py | `simpletasks` | get_issues() | 20 min |
| status_migration.py | `statuses`, `status_types` | get_statuses() | 20 min |
| watcher_migration.py | `watchers` | get_watchers() | 20 min |

**Implementation Pattern**:
```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Get current entities from Jira for a specific type."""
    if entity_type == "priorities":
        return self.jira_client.get_priorities()
    msg = f"PriorityMigration does not support entity type: {entity_type}. Supported types: ['priorities']"
    raise ValueError(msg)
```

**Total Effort**: 3 hours (9 × 20 min)

### Tier 1B: No Direct API Calls (15 migrations)

Data transformation migrations that work with already-fetched data:

| Migration | Purpose | Notes |
|-----------|---------|-------|
| affects_versions_migration.py | Version associations | Works with existing version data |
| attachment_provenance_migration.py | Attachment metadata | Post-processing |
| attachments_migration.py | File attachments | Binary data handling |
| components_migration.py | Project components | Works with project data |
| customfields_generic_migration.py | Generic custom fields | Transformation layer |
| estimates_migration.py | Time estimates | Derived from issues |
| inline_refs_migration.py | Reference extraction | Text processing |
| labels_migration.py | Issue labels | Works with issue data |
| native_tags_migration.py | OpenProject tags | Mapping transformation |
| relation_migration.py | Issue relationships | Graph processing |
| remote_links_migration.py | External links | Link processing |
| resolution_migration.py | Resolution states | Configuration data |
| security_levels_migration.py | Security config | Security data |
| sprint_epic_migration.py | Sprint/epic data | Derived from agile boards |
| story_points_migration.py | Story points | Custom field processing |
| time_entry_migration.py | Work logs | Time tracking data |
| versions_migration.py | Version management | Project versions |
| votes_migration.py | Issue votes | Vote tracking |

**Implementation Approach**:
These migrations likely need to either:
1. Return empty list (if they don't fetch from Jira directly)
2. Return data from dependencies (e.g., issues, projects)
3. Implement stub that explains they don't support idempotent workflow

**Action Required**: Case-by-case evaluation during implementation

**Total Effort**: 5 hours (15 × 20 min)

---

## Tier 2: Medium Complexity (5 migrations)

Multiple API calls or complex data structures:

| Migration | API Calls | Complexity | Effort |
|-----------|-----------|------------|--------|
| admin_scheme_migration.py | 2 unique | get_project_roles(), get_project_permission_scheme() per project | 1 hour |
| agile_board_migration.py | 3 unique | get_boards(), get_sprints(), get_backlog() | 1 hour |
| group_migration.py | 3 unique | get_groups(), get_group_members(), get_group_projects() | 1 hour |
| reporting_migration.py | 3 unique | get_filters(), get_dashboards(), get_dashboard_details() | 1 hour |
| workflow_migration.py | 4 unique | get_workflows(), get_workflow_scheme(), get_statuses(), get_transitions() | 1 hour |

**Implementation Pattern**:
```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Get current entities from Jira for a specific type."""
    if entity_type == "reporting":
        # Combine multiple API calls
        filters = self.jira_client.get_filters()
        dashboards = self.jira_client.get_dashboards()
        dashboard_details = []
        for dashboard in dashboards:
            dash_id = dashboard.get("id")
            if dash_id:
                try:
                    detail = self.jira_client.get_dashboard_details(int(dash_id))
                    dashboard_details.append(detail)
                except Exception:
                    dashboard_details.append(dashboard)

        return {
            "filters": filters,
            "dashboards": dashboard_details
        }

    msg = f"ReportingMigration does not support entity type: {entity_type}"
    raise ValueError(msg)
```

**Challenges**:
- Multiple dependent API calls
- Data aggregation logic
- Error handling for partial failures
- Per-project iteration (admin_scheme, agile_board)

**Total Effort**: 5 hours (5 × 1 hour)

---

## Tier 3: Special Cases (4 migrations)

Require evaluation before implementation:

### 1. work_package_migration.py ✅
**Status**: Already complete
**Complexity**: 4 API calls, 3 unique methods
**Note**: Despite complexity, already implements `_get_current_entities_for_type()`

### 2. tempo_account_migration.py ❌
**Status**: Legacy code
**Issue**: Does NOT inherit from BaseMigration
**Class Found**: None (standalone implementation)
**Recommendation**:
- Evaluate if still used vs superseded by AccountMigration
- May need modernization or deprecation
- Document as legacy

**Effort**: 1.5 hours (evaluation + documentation)

### 3. Migrations with Complex Dependencies

**Candidates for evaluation**:
- **attachments_migration.py**: Binary file handling, streaming considerations
- **relation_migration.py**: Graph relationships, bidirectional links
- **workflow_migration.py**: Complex state machine, already in Tier 2

**Effort**: 1 hour each for evaluation

---

## Implementation Priority

### Phase 1: Foundation (1 day)
- j2o-51: ✅ This document
- j2o-52: Create implementation templates
- j2o-53: Design testing strategy
- j2o-54: Add feature flag to migration.py

### Phase 2: Tier 1 Quick Wins (2.5 days)
Start with Tier 1A (9 migrations with single API calls):
1. priority_migration.py (j2o-56)
2. status_migration.py (j2o-55)
3. issue_type_migration.py (j2o-57)
4. link_type_migration.py (j2o-62)
5. category_defaults_migration.py (j2o-70 - moved from Tier 2)
6. account_migration.py (j2o-79 - split from Company)
7. company_migration.py (j2o-79)
8. simpletasks_migration.py (new)
9. watcher_migration.py (new)

Then tackle Tier 1B (15 no-API-call migrations):
- Evaluate each for appropriate implementation
- May return empty lists or reference dependencies

### Phase 3: Tier 2 Medium (2.5 days)
Implement 5 migrations with multiple API calls:
1. admin_scheme_migration.py (j2o-72)
2. reporting_migration.py (j2o-71)
3. agile_board_migration.py (j2o-74)
4. group_migration.py (j2o-58 - moved to Tier 2)
5. workflow_migration.py (j2o-73)

### Phase 4: Tier 3 Evaluation (1.5 days)
Evaluate special cases and document decisions:
1. tempo_account_migration.py - Legacy evaluation
2. Complex dependency migrations
3. Document exceptions and rationale

---

## Tier Definitions

**Tier 1 - Simple**:
- ✅ Single entity type
- ✅ 0-1 API calls
- ✅ Straightforward ETL
- ✅ No complex dependencies
- **Pattern**: 5-10 lines of code
- **Time**: 20 minutes each

**Tier 2 - Medium**:
- ⚠️ 2-4 API calls
- ⚠️ Multiple entity types
- ⚠️ Per-project iteration
- ⚠️ Data aggregation logic
- **Pattern**: 20-40 lines of code
- **Time**: 1 hour each

**Tier 3 - Complex**:
- 🔴 Special architectural considerations
- 🔴 Legacy code requiring modernization
- 🔴 Complex dependencies or state
- 🔴 May not fit idempotent pattern
- **Pattern**: Case-by-case evaluation
- **Time**: 1.5 hours evaluation + variable implementation

---

## Risk Assessment

### Low Risk (Tier 1A - 9 migrations)
- Simple, well-defined patterns
- Reference implementations exist
- Quick to implement and validate

### Medium Risk (Tier 1B - 15 migrations)
- Unclear if idempotent workflow applies
- May need architectural guidance
- Testing complexity unknown

### Medium-High Risk (Tier 2 - 5 migrations)
- Multiple failure points (API calls)
- Complex data aggregation
- Existing patterns in admin_scheme, reporting, workflow

### High Risk (Tier 3 - 4 migrations)
- Legacy code (tempo_account)
- May require refactoring beyond scope
- Could expose architectural limitations

---

## Success Metrics

**Phase 2 Success**:
- 9 Tier 1A migrations implemented
- All tests passing
- 30-40% API call reduction
- Cache hit rate >50%

**Phase 3 Success**:
- All Tier 2 migrations implemented
- 60-70% API call reduction
- Cache hit rate >65%

**Phase 4 Success**:
- All migrations evaluated
- Exceptions documented
- 80-95% API call reduction (overall)
- Cache hit rate >70%

---

## Next Steps

1. ✅ **Complete**: This categorization document
2. **Next**: j2o-52 - Create implementation templates for each tier
3. **Then**: j2o-53 - Design testing strategy with tier-specific validation
4. **Finally**: j2o-54 - Add USE_IDEMPOTENT_WORKFLOWS feature flag

---

**Analysis Performed By**: Claude (SuperClaude Framework)
**Date**: 2025-10-15
**Migrations Analyzed**: 37
**Categorization**: 4 Complete + 24 Tier 1 + 5 Tier 2 + 4 Tier 3
**Total Effort Estimate**: 8.5 days
