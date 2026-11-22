# ADR-014: Tempo billable hours and approval status are time entry custom fields

## Status

Accepted (2026-11-22)

## Context

Tempo stores, per work log, the billable seconds and an approval status (`OPEN`, `WAITING_FOR_APPROVAL`, `APPROVED` and so on). `jira_tempo_service.py` read both, and `time_entry_transformer.py` kept the billable hours in the time entry's `_meta`, but the Rails writer wrote only hours, date, activity, comment and the provenance fields. Both values were lost on the target.

Three places could hold them: time entry custom fields, the sealed Tempo attribute archive, or a report. The operator decided on 2026-11-21 for time entry custom fields.

## Decision

1. The time entry writer keeps two `TimeEntryCustomField`s: `Tempo Billable Hours` (`float`) and `Tempo Approval Status` (`string`). It creates them on first use under an advisory lock, like the provenance fields.
2. Both fields are `admin_only`. Billing data can be confidential; an administrator can open a field to other roles in OpenProject.
3. The fields mirror Tempo. Every run writes the current value, also on a time entry that an earlier run created, so an approval granted later in Tempo reaches OpenProject. A value that already matches is not written again. Because only administrators can change these fields, the overwrite guard of ADR-013 does not apply to them.
4. Billable hours are the billable seconds divided by 3600, rounded to four decimals, so a single billable minute (0.0167 h) survives. A missing value writes nothing.
5. The approval status is Tempo's string; a dictionary shape is read from its `status` or `name`. An empty status writes nothing.

## Consequences

- The time entry result counts `tempo_fields_changed`, and the migration summary adds it up.
- A field of the same name with another format, or one that is not admin-only, makes the writer refuse the time entry.
- Project members without administrator rights do not see billable hours or the approval status until an administrator opens the fields.

## Verified

`tests/integration/test_time_entry_preservation.py` ran against the OpenProject 17.9.7 pilot in a rolled-back transaction: a new entry wrote both fields, an unchanged repeat wrote none, a repeat with a new approval status wrote one, both fields were admin-only, and the stored billable hours equalled 0.0047.
