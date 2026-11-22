# ADR-013: A re-run writes a value again only while it holds what J2O wrote last

## Status

Accepted (2026-11-22)

## Context

`work_packages_content` wrote every non-empty Jira description and every mapped custom field value to its work package on each run. It never looked at the target first. Once people work in OpenProject, a repeated run (a delta run before cutover, a run after a fix) replaced their edits without a trace.

Three components write the description of a work package:

- `work_packages_content` writes the converted Jira description.
- `simpletasks` replaces the checklist section inside it (`tasklist_origins.py`).
- `inline_refs` rewrites inline attachment links in it.

So a guard in `work_packages_content` alone would take the rewrites of the two other components for edits made by a person.

OpenProject stores what it receives in a form of its own: a list option as its option ID, a boolean as `t`, a multi-value field as several rows. A comparison of the value J2O sends with the value OpenProject stores is therefore not reliable. A comparison of the stored value with itself is reliable.

## Decision

1. J2O keeps, for each work package and attribute (`description`, `customField<id>`), the SHA-256 of the JSON of the stored value it last wrote or found unchanged. The store is the SQLite file `written_values.sqlite` in the data directory, next to the mappings (`src/application/components/written_values.py`).
2. Rails computes each digest from the stored value: the description text, or the sorted non-empty `custom_values` of the field (`WRITTEN_GUARD` in `src/infrastructure/openproject/written_guard.py`).
3. A guarded row of `batch_update_work_packages` carries `_j2o_guard` (`attr`, `expect`, `overwrite`) and writes exactly that one attribute. Inside the row lock Rails decides:
   - `unchanged`: the target already holds the wanted value. Nothing is written and no journal is added; the digest is recorded.
   - `updated`: the target holds the value with the recorded digest. Without a record, the target is empty or holds the field default. The digest of the newly stored value is returned and recorded.
   - `target_changed`: anything else. The row is not written. The record keeps its old digest, so the row stays refused on the next run too.
4. `work_packages_content` reports refused rows as `target_changed` (`work package:attribute`) and counts them per kind. They are not failures, so the run stays successful.
5. `simpletasks` and `inline_refs` keep their rewrite: it changes only their own section or a link, and it keeps the text around it. They update the record only when the description they rewrote held the recorded digest (or was empty, for a checklist without a record). A description that a person changed keeps its old record, and `work_packages_content` keeps refusing it.
6. `migration.overwrite_edited_targets: true` (only the boolean) writes every row and records the new digests. It is the operator's decision for a run, never a default.
7. Rows without `_j2o_guard` behave as before. Other callers of `batch_update_work_packages` are unchanged.

## Consequences

- A repeated run leaves a description or a custom field value that someone changed in OpenProject as it is and names it in the log and in the `target_changed` result detail.
- A repeated run with unchanged Jira content writes nothing and adds no journal entries.
- The store is migration state like the mappings. Without it (a new data directory, a lost file), a target counts as changed by a person when it holds a value other than the wanted one, other than empty and other than the field default. The operator records such targets with one run with `overwrite_edited_targets: true`. Back up the data directory with the mappings.
- Work packages migrated before this change have no record. Their first content run records every target that already holds the wanted value (`unchanged`) and writes every empty one or one at its field default; it refuses the others. For an instance where nobody has worked yet, one run with `overwrite_edited_targets: true` records those too.
- A crash between the Rails write and the record leaves the old digest. The next run of the same content finds the wanted value and records it as `unchanged`. If the Jira content changed in between, the row is refused and needs the operator flag.
- The legacy single-phase `work_packages` component writes descriptions at creation without a record. A content run after it records those that still match the converted Jira text and refuses the others unless the operator allows overwriting.

## Not covered yet

`estimates`, `priorities`, `project_named_records` and the other custom field writers (`openproject_work_package_custom_field_service.py`, `BaseMigration._write_work_package_custom_value`) still write without the guard. They follow in the same issue.

## Verified

On the OpenProject 17.9.7 pilot, in a transaction that was rolled back:

- description: a non-empty target without a record was refused; the operator flag wrote it; a repeated run with the same text was `unchanged` and added no journal (CRLF stored unchanged); new Jira text was written; a description changed by another writer was refused and kept.
- custom fields of the formats string, text, date, float, int, list, multi-value list and bool: first write, then `unchanged` on a repeated run; an int and a bool changed in the database were refused; a list field holding its default option counted as untouched.
- `inline_refs` returned the digest of the rewritten description.
