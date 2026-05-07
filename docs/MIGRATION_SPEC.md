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

## Audit ruleset

`tools/audit_migrated_project.py` enforces the spec above as a set of
named rules. Each rule emits either a **failure** (non-zero exit code,
blocks merge gates) or a **warning** (informational). The table is the
authoritative list — add entries here when introducing new rules so
operators have a single reference for what the audit checks.

### OP-side rules (no Jira required)

| Rule | Severity | Trigger | Spec line | Added |
|---|---|---|---|---|
| Audit aborted | failure | Ruby returns `error` (project not found) | — | #175 |
| No work packages | failure | `wp_total == 0` | — | #175 |
| Missing `author_id` | failure | `wp_with_author < wp_total` | per-WP `author_id` | #175 |
| Missing `subject` | failure | `wp_with_subject < wp_total` | per-WP `subject` | #175 |
| Bug A (assignee coverage) | failure | `wp_with_assignee / wp_total < 5%` | per-WP `assigned_to_id` | #175 |
| Bug E (timestamps) | failure | `>50%` of WPs created in last 24h | per-WP `created_at` | #175 |
| Bug D (WP CFs missing) | failure | Any of the 8 `WorkPackageCustomField` provenance CFs absent | per-instance | #175 |
| Bug D (User CFs missing) | failure | Any of the 4 `UserCustomField` provenance CFs absent | per-instance | #175 |
| Bug D (TE CFs missing) | failure | Any of the 6 `TimeEntryCustomField` provenance CFs absent | per-instance | #175 |
| Bug B (TE hours uniform) | failure | All TE hours collapsed to one value | per-instance | #175 |
| TE Worklog Key population | failure | `te_with_worklog_key < te_total` | per-TE worklog key | #179 |
| WP type/status/priority NULL | failure | `wp_with_type/status/priority < wp_total` | per-WP `type_id`/`status_id`/`priority_id` | #176 |
| Journal count below WP count | failure | `wp_journal_total < wp_total` (Rails auto-emits ≥1 per create) | per-instance | #176 |
| WP CF format violation | failure | Any populated WP provenance CF value doesn't match its regex | per-WP provenance | #178 |
| `J2O Origin Key` under-populated | failure | `wp_provenance_cfs["J2O Origin Key"].populated < wp_total` | per-WP `J2O Origin Key` (hard-required, line 27) | #193 |
| Other WP CF under-populated | warning | Any other existing WP provenance CF has `populated < wp_total` | per-WP provenance (soft) | #193 |
| TE CF format violation | failure | `J2O Origin Worklog Key` value doesn't match `<KEY>:<id>` or `tempo:<id>` | per-TE worklog key | #181 |
| User CF format violation | failure | `J2O Origin System` or `J2O External URL` value malformed | per-User provenance | #182 |
| Orphan relations | failure | Any `Relation` references a deleted WP | per-instance | #177 |
| Orphan watchers | failure | Any `Watcher` references a deleted user | per-instance | #177 |
| Zero relations on big project | warning | `wp_total >= 50 ∧ relation_total == 0` | per-instance | #176 |
| Zero watchers on big project | warning | `wp_total >= 50 ∧ wp_watcher_total == 0` | per-instance | #176 |
| Description coverage low | warning | `<50%` of WPs have a description | per-WP `description` | #175 |

### Source-side rules (require Jira credentials)

Each compares an OP-side count to the Jira source. When Jira is
unreachable, every source-side rule degrades to a **warning** ("source
unavailable") rather than blocking — operators may legitimately run
the audit without Jira creds, and the OP-side rules still produce a
useful report.

| Rule | Severity | Trigger | Spec line | Added |
|---|---|---|---|---|
| Issue count mismatch | failure | `jira_issue_count != wp_total` (exact) | per-instance (issues exact) | #183 |
| Attachment count mismatch | failure | `jira_attachment_count != wp_attachment_total` (exact) | per-collection attachments | #184 |
| Relation count drift | failure | `&#124;Δ&#124; / jira_relation_count > 5%` | per-collection relations ±5% | #185 |
| Watcher count drift | failure | `&#124;Δ&#124; / jira_watcher_count > 5%` | per-collection watchers | #186 |
| Source unavailable (any of the 4) | warning | Jira fetch returned `None` | — | #183–#186 |

### Hardening contracts

- **Ruby/Python schema-skew guard.** Numeric metrics flow through
  `_metric_int(metrics, key)` which coerces both *missing key* and
  `None`-value to `0`. A future Ruby change emitting `null` doesn't
  crash `_classify`; a stale Ruby script omitting a key fires the
  rule loud rather than silently passing. (#176, #180)
- **Pagination safety.** Every Jira-side helper paginates by
  advancing `start_at += len(page)` (NOT by the requested
  `page_size` — Jira Server caps `maxResults` and the obvious
  heuristic silently truncates after page 1). A `for…else` cap at
  `_PAGINATION_MAX_PAGES` defends against a buggy upstream returning
  the same page repeatedly. (#184, #185)
- **JQL-injection guard.** Project keys are regex-validated against
  `\A[A-Z][A-Z0-9_]+\z` before being interpolated into JQL — a stray
  quote in argv would otherwise silently change the query scope. (#184)
- **Best-effort error contract.** All Jira-side helpers log
  `type(exc).__name__: str(exc)` plus `traceback.format_exc()` to
  stderr and return `None` on any failure. The classifier converts
  `None` to a "source unavailable" warning. The forensic trail lets
  operators distinguish "no creds" (expected) from "JiraClient
  broken" (a real bug). (#183)
