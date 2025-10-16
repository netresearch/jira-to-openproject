# j2o-50: Implementation Templates for Idempotent Workflow Caching

**Task**: j2o-52
**Date**: 2025-10-15
**Purpose**: Provide code templates for implementing `_get_current_entities_for_type()` across all migration tiers

## Overview

This document provides copy-paste templates for implementing the `_get_current_entities_for_type()` method that enables idempotent workflow caching. All templates follow the patterns established in successfully completed migrations:
- user_migration.py
- custom_field_migration.py
- project_migration.py
- work_package_migration.py

## Template Selection Guide

| Your Migration | API Calls | Complexity | Use Template |
|----------------|-----------|------------|--------------|
| Single entity type, 1 API call | 1 | Simple | **Tier 1A** |
| Transformation-only (no direct API) | 0 | Simple | **Tier 1B** |
| Multiple API calls, data aggregation | 2-4 | Medium | **Tier 2** |
| Special cases, custom logic | Varies | Complex | **Tier 3** |

---

## Tier 1A Template: Single Entity Type

**Use When**: Migration fetches one entity type with a single API call

**Example Migrations**:
- status_migration.py → `get_statuses()`
- priority_migration.py → `get_priorities()`
- issue_type_migration.py → `get_issue_types()`

### Template

```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Get current entities from Jira for a specific type.

    This method enables idempotent workflow caching by providing a standard
    interface for entity retrieval. Called by run_idempotent() to fetch data
    with automatic thread-safe caching.

    Args:
        entity_type: The type of entities to retrieve (e.g., "priorities")

    Returns:
        List of entity dictionaries from Jira API

    Raises:
        ValueError: If entity_type is not supported by this migration
    """
    # Check if this is the entity type we handle
    if entity_type == "priorities":  # CHANGE THIS to match your entity type
        return self.jira_client.get_priorities()  # CHANGE THIS to your API method

    # Raise error for unsupported types
    msg = (
        f"PriorityMigration does not support entity type: {entity_type}. "  # CHANGE CLASS NAME
        f"Supported types: ['priorities']"  # CHANGE TO YOUR TYPES
    )
    raise ValueError(msg)
```

### Customization Checklist

1. **Line 18**: Change `"priorities"` to your entity type (e.g., `"statuses"`, `"issue_types"`)
2. **Line 19**: Change `get_priorities()` to your API method (e.g., `get_statuses()`, `get_issue_types()`)
3. **Line 23**: Change `PriorityMigration` to your class name
4. **Line 24**: Change `['priorities']` to list your supported types

### Multi-Type Variation

If your migration handles multiple entity types (e.g., `users` and `user_accounts`):

```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Get current entities from Jira for a specific type."""
    # Support both entity type names
    if entity_type in ("users", "user_accounts"):
        return self.jira_client.get_users()

    msg = (
        f"UserMigration does not support entity type: {entity_type}. "
        f"Supported types: ['users', 'user_accounts']"
    )
    raise ValueError(msg)
```

---

## Tier 1B Template: Transformation-Only Migrations

**Use When**: Migration performs data transformation without direct Jira API calls

**Example Migrations**:
- affects_versions_migration.py
- inline_refs_migration.py
- story_points_migration.py

### Template (Stub Implementation)

```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Get current entities for transformation.

    This migration performs data transformation on already-fetched entities
    rather than fetching directly from Jira. It does not benefit from
    idempotent workflow caching.

    Args:
        entity_type: The type of entities requested

    Returns:
        Empty list (this migration doesn't fetch from Jira)

    Raises:
        ValueError: Always, as this migration doesn't support idempotent workflow
    """
    msg = (
        f"StoryPointsMigration is a transformation-only migration and does not "  # CHANGE CLASS NAME
        f"support idempotent workflow. It operates on data from other migrations."
    )
    raise ValueError(msg)
```

**Alternative Approach**: Return empty list if you want to allow idempotent workflow (will skip on cache miss):

```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Transformation-only migration - no direct entity fetching."""
    # Return empty list to indicate no entities to fetch from Jira
    # Migration will proceed with transformation logic in run()
    return []
```

---

## Tier 2 Template: Multiple API Calls with Aggregation

**Use When**: Migration fetches from multiple endpoints or aggregates data

**Example Migrations**:
- admin_scheme_migration.py (2 calls per project)
- reporting_migration.py (3 calls: filters, dashboards, details)
- workflow_migration.py (4 calls: workflows, schemes, statuses, transitions)

### Template

```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Get current entities from Jira with multiple API calls.

    This migration requires multiple API calls to gather complete data.
    The method aggregates results into a unified structure.

    Args:
        entity_type: The type of entities to retrieve

    Returns:
        Aggregated data from multiple API calls

    Raises:
        ValueError: If entity_type is not supported
    """
    if entity_type == "reporting":  # CHANGE TO YOUR TYPE
        # Fetch data from multiple endpoints
        filters = self.jira_client.get_filters()
        dashboards = self.jira_client.get_dashboards()

        # Fetch dependent data (dashboard details for each dashboard)
        dashboard_details = []
        for dashboard in dashboards:
            dash_id = dashboard.get("id")
            if dash_id:
                try:
                    detail = self.jira_client.get_dashboard_details(int(dash_id))
                    dashboard_details.append(detail)
                except Exception:  # noqa: BLE001
                    # If details fetch fails, use basic dashboard data
                    dashboard_details.append(dashboard)

        # Return aggregated structure
        # Note: This returns a dict, not list, for complex aggregations
        return {
            "filters": filters,
            "dashboards": dashboard_details
        }

    msg = (
        f"ReportingMigration does not support entity type: {entity_type}. "  # CHANGE CLASS
        f"Supported types: ['reporting']"  # CHANGE TYPES
    )
    raise ValueError(msg)
```

### Per-Project Iteration Pattern

For migrations that iterate over projects (e.g., admin_scheme, agile_board):

```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Get current entities with per-project iteration."""
    if entity_type == "admin_schemes":  # CHANGE TO YOUR TYPE
        projects = []

        # Get project mapping
        project_mapping = self.mappings.get_mapping("project") or {}

        # Iterate over mapped projects
        for project_key, entry in project_mapping.items():
            op_project_id = int(entry.get("openproject_id", 0) or 0) if isinstance(entry, dict) else 0
            if op_project_id <= 0:
                continue

            try:
                # Fetch project-specific data
                roles = self.jira_client.get_project_roles(project_key)
                scheme = self.jira_client.get_project_permission_scheme(project_key)

                # Aggregate per-project data
                projects.append({
                    "project_key": project_key,
                    "openproject_id": op_project_id,
                    "roles": roles,
                    "permission_scheme": scheme,
                })
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    f"Failed to fetch admin scheme for project {project_key}: {exc}"
                )
                continue

        return projects

    msg = f"AdminSchemeMigration does not support entity type: {entity_type}"
    raise ValueError(msg)
```

---

## Tier 3 Template: Custom Run() with Partial Caching

**Use When**: Migration has complex logic that doesn't fit standard pattern

**Approach**: Keep custom `run()` but add `_get_current_entities_for_type()` for specific cacheable parts

### Template (Hybrid Approach)

```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Get cacheable entities where possible.

    This migration uses custom run() logic but provides caching for
    specific entity types that benefit from it.
    """
    # Implement only for cacheable parts
    if entity_type == "workflow_statuses":
        return self.jira_client.get_statuses()

    if entity_type == "workflow_transitions":
        return self.jira_client.get_transitions()

    # For uncacheable parts, raise error
    msg = (
        f"WorkflowMigration only supports partial caching for specific entity types. "
        f"Supported types: ['workflow_statuses', 'workflow_transitions']. "
        f"Full workflow migration requires custom run() method."
    )
    raise ValueError(msg)

def run(self) -> ComponentResult:
    """Custom run implementation with selective caching.

    Uses cached entities where beneficial but maintains custom logic
    for complex workflow processing.
    """
    # Use cached data for statuses and transitions
    try:
        statuses = self._get_cached_entities_threadsafe(
            entity_type="workflow_statuses",
            cache_invalidated=set(),
            entity_cache={}
        )
        transitions = self._get_cached_entities_threadsafe(
            entity_type="workflow_transitions",
            cache_invalidated=set(),
            entity_cache={}
        )
    except Exception:  # noqa: BLE001
        # Fallback to direct API if caching fails
        statuses = self.jira_client.get_statuses()
        transitions = self.jira_client.get_transitions()

    # Continue with custom workflow processing logic
    workflows = self.jira_client.get_workflows()  # Not cached
    # ... rest of custom logic
```

---

## Common Patterns and Best Practices

### Error Handling

Always use broad exception handling for robustness:

```python
try:
    return self.jira_client.get_priorities()
except Exception as exc:  # noqa: BLE001
    self.logger.error(f"Failed to fetch priorities: {exc}")
    return []  # Return empty list on failure
```

### Logging

Add logging for debugging cache behavior:

```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    self.logger.debug(f"Fetching entities for type: {entity_type}")

    if entity_type == "priorities":
        priorities = self.jira_client.get_priorities()
        self.logger.debug(f"Fetched {len(priorities)} priorities from Jira")
        return priorities

    raise ValueError(f"Unsupported entity type: {entity_type}")
```

### Type Annotations

Always include type annotations for clarity:

```python
from typing import Any

def _get_current_entities_for_type(
    self,
    entity_type: str
) -> list[dict[str, Any]]:
    """Type annotations improve IDE support and documentation."""
    ...
```

### Docstring Standards

Follow the established pattern from successful migrations:

```python
def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
    """Get current entities from Jira for a specific type.

    This method is called by the idempotent workflow to fetch entities
    with automatic thread-safe caching. Implementations should:
    - Return entity data directly from Jira API
    - Not perform transformations (done in _map())
    - Handle errors gracefully
    - Log appropriately for debugging

    Args:
        entity_type: The type of entities to retrieve (e.g., "priorities", "statuses")

    Returns:
        List of entity dictionaries from Jira API, or aggregated structure for complex types

    Raises:
        ValueError: If entity_type is not supported by this migration
    """
```

---

## Testing Your Implementation

### 1. Unit Test Template

```python
def test_get_current_entities_for_type_success(self):
    """Test successful entity retrieval."""
    # Arrange
    mock_jira_client = Mock()
    mock_jira_client.get_priorities.return_value = [
        {"id": "1", "name": "High"},
        {"id": "2", "name": "Low"},
    ]

    migration = PriorityMigration(
        jira_client=mock_jira_client,
        op_client=Mock()
    )

    # Act
    result = migration._get_current_entities_for_type("priorities")

    # Assert
    assert len(result) == 2
    assert result[0]["name"] == "High"
    mock_jira_client.get_priorities.assert_called_once()

def test_get_current_entities_for_type_unsupported(self):
    """Test error for unsupported entity type."""
    migration = PriorityMigration(
        jira_client=Mock(),
        op_client=Mock()
    )

    with pytest.raises(ValueError, match="does not support entity type"):
        migration._get_current_entities_for_type("invalid_type")
```

### 2. Integration Test

```python
def test_idempotent_workflow_with_caching(self):
    """Test that idempotent workflow uses caching."""
    # Enable feature flag
    config.migration_config["use_idempotent_workflows"] = True

    # Run migration
    result = migration.run_idempotent()

    # Verify caching was used (API called only once)
    assert mock_jira_client.get_priorities.call_count == 1

    # Run again - should use cache
    result2 = migration.run_idempotent()

    # API should still have been called only once
    assert mock_jira_client.get_priorities.call_count == 1
```

---

## Migration Checklist

When implementing `_get_current_entities_for_type()` for your migration:

- [ ] Choose correct tier template (1A, 1B, 2, or 3)
- [ ] Copy template to your migration file
- [ ] Customize entity type strings
- [ ] Customize API method calls
- [ ] Update class name in error messages
- [ ] Add appropriate logging
- [ ] Handle errors gracefully
- [ ] Write unit tests
- [ ] Test with feature flag enabled
- [ ] Verify caching behavior
- [ ] Document any deviations from template

---

## Quick Reference

| Tier | Template | Lines of Code | Effort | Risk |
|------|----------|---------------|--------|------|
| 1A | Single API call | 10-15 | 20 min | Low |
| 1B | Transformation stub | 5-10 | 10 min | None |
| 2 | Multiple calls | 30-50 | 1 hour | Medium |
| 3 | Custom hybrid | 50+ | 1.5 hours | High |

---

**Document Created**: 2025-10-15
**Task**: j2o-52
**Author**: Claude (SuperClaude Framework)
**Referenced Implementations**: user_migration.py, custom_field_migration.py, project_migration.py
**Total Templates**: 4 tiers with variations
