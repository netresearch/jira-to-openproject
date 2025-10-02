# ADR: Work Package Start Date Fallback via Jira Status History

**Status:** Accepted

## Context

Many Jira projects (e.g., SRVAC) do not populate the canonical start-date custom
fields (`customfield_18690` / `customfield_12590` / `customfield_11490` / `customfield_15082`).
The migration therefore lands OpenProject work packages without `start_date`. Operators
requested we copy Jira’s first “In Progress” transition date instead, so progress
reports reflect when implementation actually began.

## Decision

1. Extend `WorkPackageMigration` to scan Jira changelog histories and extract the first
   status transition whose status category is *In Progress* (`statusCategory.id == 4`
   or key `indeterminate`).
2. Reuse `EnhancedTimestampMigrator._normalize_timestamp` to canonicalise timestamps
   and record the resulting date as the OpenProject `start_date` when no custom-field
   value exists.
3. Cache Jira status metadata (`jira_statuses.json`) to map status IDs/names to
   categories so history lookups stay fast and deterministic.
4. Document the fallback so QA warnings (“0% start-date coverage”) clearly indicate
   missing transitions rather than missing field mappings.

## Consequences

- Start dates now populate automatically for issues that ever entered an
  In-Progress category, improving plan/baseline reporting in OpenProject.
- Projects without such transitions still surface as warnings in
  `scripts/data_qa.py`; that is expected and signals genuinely missing lifecycle data.
- Any Jira instance with customised category IDs/keys is still recognised because
  we match on category id, key, and name.

## Follow-up

- Consider persisting a provenance note (e.g., `J2O Start Date Source`) so analysts
  can distinguish custom-field vs. history-derived values.
- Add a targeted integration test once we have rehearsal fixtures with populated
  changelog histories.
