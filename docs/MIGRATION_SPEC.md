# Migrated Work Package Spec

Hard contract a migrated WP must satisfy. Used by `tools/audit_migrated_project.py`
to verify a real OP instance after a migration run. Where "must" is used,
the audit reports a hard failure when the field is empty or wrong; "should"
reports a warning.

## Per-WP required fields

| OP field | Source | Rule |
|---|---|---|
| `subject` | `jira.fields.summary` | **must** be non-empty (truncated to 255 chars OK) |
| `type_id` | `issue_type` mapping | **must** resolve to an active OP `Type` |
| `status_id` | `status_types` mapping | **must** resolve to an active OP `Status` |
| `priority_id` | `priorities` mapping | **must** resolve to an active OP `Priority` (default fallback OK) |
| `project_id` | `projects` mapping | **must** be an OP `Project` whose identifier maps from the Jira project key |
| `author_id` | `_map_user(jira.fields.reporter)` with default-author fallback | **must** be set; default fallback acceptable when reporter is unmapped |
| `assigned_to_id` | `_map_user(jira.fields.assignee)` | **must** be set whenever Jira's issue had an assignee. Empty IS acceptable iff Jira had no assignee. |
| `description` | `jira.fields.description` (markdown-converted) | **should** be non-empty when Jira had one |
| `created_at` | `jira.fields.created` (preserved via `update_columns`) | **must** equal Jira's `created` timestamp, NOT the migration timestamp. Tolerance: ±1s. |
| `updated_at` | `jira.fields.updated` | **should** equal Jira's `updated` (off by content-phase update by ±a few minutes is acceptable) |
| `due_date` | `jira.fields.duedate` | **should** be set when Jira had it |
| `start_date` | `jira.fields.customfield_*` (start) | **should** when Jira had it |

## Per-WP provenance custom fields (must, all WPs)

`J2O Origin Key` ← Jira issue key (e.g. `NRS-4388`) — hard requirement
`J2O Origin ID` ← Jira issue id (numeric) — should
`J2O Origin URL` ← `<jira-base>/browse/<key>` — should
`J2O Project Key` ← Jira project key — should
`J2O First Migration Date` ← first run timestamp — should

## Per-WP related collections

| Collection | Source | Rule |
|---|---|---|
| Journal entries (comments + activity) | `jira.fields.comment.comments[]` + history | **count must equal** Jira's comment count + history change count, ±10% tolerance |
| Attachments | `jira.fields.attachment[]` | **count must equal** Jira's attachment count, exact match required |
| Relations (`Relation` model) | `jira.fields.issuelinks[]` | **count must equal** Jira's link count, ±5% tolerance |
| Watchers | `jira.fields.watches.watchers[]` | **should** equal Jira's watcher count |
| Time entries | `jira.worklogs[]` | **count must equal** worklog count; **sum of hours** must be within ±5% of Jira's total |

## Per-instance global expectations

* All 8 `WorkPackageCustomField` provenance CFs exist (Bug D fix).
* All 4 `UserCustomField` provenance CFs exist.
* All 6 `TimeEntryCustomField` provenance CFs exist.
* User mapping has `openproject_id` populated for every Jira user that exists in OP (Bug A fix).
* Time entry hours are NOT all clamped to the floor (Bug B fix).
* Re-running the migration does NOT duplicate time entries (Bug C fix).

## Per-time-entry rules

| OP field | Source | Rule |
|---|---|---|
| `hours` | `jira.worklog.timeSpentSeconds / 3600` | **must** be that ratio, rounded to 0.01. NOT all entries should have the floor value. |
| `user_id` | mapped author | **must** resolve via user mapping |
| `entity_id` + `entity_type='WorkPackage'` | the WP's id | **must** point to a real WP |
| `spent_on` | `jira.worklog.started` parsed to a Date | **must** match the original date (UTC tolerance) |
| `comments` | `jira.worklog.comment` | **should** match (truncated to 1000 chars) |
| Provenance CF `J2O Origin Worklog Key` | `<issue_key>:<worklog_id>` | **must** be populated for dedup on re-run |
