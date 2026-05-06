"""Post-migration audit for a Jira project's WPs in OpenProject.

Runs against a live OP instance (via the same tmux Rails console
``j2o`` uses) and reports per-spec compliance for the migrated work
packages of a given Jira project key.

Usage:

    .venv/bin/python -m tools.audit_migrated_project NRS

Output: structured JSON to stdout + a one-line PASS/FAIL summary on
stderr. Non-zero exit code if any **must** rule fails.

Spec: see ``docs/MIGRATION_SPEC.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from src.infrastructure.openproject.openproject_client import OpenProjectClient

# --- spec checks ----------------------------------------------------------

_REQUIRED_WP_PROVENANCE_CFS: tuple[str, ...] = (
    "J2O Origin Key",
    "J2O Origin ID",
    "J2O Origin System",
    "J2O Origin URL",
    "J2O Project Key",
    "J2O Project ID",
    "J2O First Migration Date",
    "J2O Last Update Date",
)

# Ruby regex literals for each WP provenance CF — the migrator
# populates these with crisp formats and a regression that corrupts
# the value (truncation, missing prefix, wrong type) is invisible to
# the populated-count check. Patterns are kept conservative so a
# legitimate edge case cannot silently fail; if a CF name is absent
# from this map, the audit only checks population, not format.
_WP_CF_FORMAT_REGEXES: tuple[tuple[str, str], ...] = (
    ("J2O Origin Key", r"\A[A-Z][A-Z0-9_]+-\d+\z"),
    ("J2O Origin ID", r"\A\d+\z"),
    ("J2O Origin System", r"\AJira\z"),
    # Forward slashes escaped so the Ruby ``/.../`` regex literal
    # doesn't terminate at the protocol's ``://``.
    ("J2O Origin URL", r"\Ahttps?:\/\/[^\s]+\/browse\/[A-Z][A-Z0-9_]+-\d+\z"),
    ("J2O Project Key", r"\A[A-Z][A-Z0-9_]*\z"),
    ("J2O Project ID", r"\A\d+\z"),
    ("J2O First Migration Date", r"\A\d{4}-\d{2}-\d{2}\z"),
    ("J2O Last Update Date", r"\A\d{4}-\d{2}-\d{2}\z"),
)

_REQUIRED_USER_PROVENANCE_CFS: tuple[str, ...] = (
    "J2O Origin System",
    "J2O User ID",
    "J2O User Key",
    "J2O External URL",
)

# A project of this size or larger should typically have at least
# one relation and one watcher across its WPs; below this we don't
# warn (small/inactive projects legitimately have zero).
_HEURISTIC_SIZE_THRESHOLD = 50

_REQUIRED_TE_PROVENANCE_CFS: tuple[str, ...] = (
    "J2O Origin Worklog Key",
    "J2O Origin Issue ID",
    "J2O Origin Issue Key",
    "J2O Origin System",
    "J2O First Migration Date",
    "J2O Last Update Date",
)


def _build_audit_script(jira_project_key: str) -> str:
    """Build the Ruby audit expression for a Jira project.

    Returns a single Ruby expression that evaluates to a hash of audit
    metrics. Routed through ``execute_large_query_to_json_file`` so the
    result is read back via a container tempfile — bypassing the noisy
    tmux scrollback and any stale-marker collisions.
    """
    expected_wp_cfs = list(_REQUIRED_WP_PROVENANCE_CFS)
    expected_user_cfs = list(_REQUIRED_USER_PROVENANCE_CFS)
    expected_te_cfs = list(_REQUIRED_TE_PROVENANCE_CFS)
    # Build the Ruby hash literal mapping CF name -> Regexp literal.
    # Keep the regex sources verbatim — they're the same on both sides
    # (Ruby and Python both accept ``\A``, ``\z``, character classes).
    cf_format_pairs = ", ".join(f"{name!r} => /{pattern}/" for name, pattern in _WP_CF_FORMAT_REGEXES)
    return f"""
(lambda do
  proj_key = {jira_project_key!r}.downcase
  proj = Project.find_by(identifier: proj_key)
  next {{ error: "OP project '#{{proj_key}}' not found" }} unless proj

  wps = WorkPackage.where(project_id: proj.id)
  wp_ids = wps.pluck(:id)

  wp_provenance = {{}}
  {expected_wp_cfs!r}.each do |cf_name|
    cf = CustomField.find_by(type: 'WorkPackageCustomField', name: cf_name)
    populated = cf ? CustomValue.where(custom_field_id: cf.id, customized_type: 'WorkPackage').
      where(customized_id: wp_ids).where.not(value: [nil, '']).count : 0
    wp_provenance[cf_name] = {{ 'exists' => !cf.nil?, 'populated' => populated }}
  end

  # Per-CF format-violation count. For each WP provenance CF that has
  # a regex spec, count populated values that don't match. Pluck-then-
  # filter (rather than DB-side regex) keeps the query DB-portable
  # across Postgres/MySQL/SQLite and lets us reuse the same regex
  # source on both sides.
  wp_cf_format_violations = {{}}
  {{ {cf_format_pairs} }}.each do |cf_name, regex|
    cf = CustomField.find_by(type: 'WorkPackageCustomField', name: cf_name)
    next unless cf
    bad = CustomValue.where(custom_field_id: cf.id, customized_type: 'WorkPackage').
      where(customized_id: wp_ids).where.not(value: [nil, '']).
      pluck(:value).reject {{ |v| v =~ regex }}.count
    wp_cf_format_violations[cf_name] = bad
  end

  user_provenance = {expected_user_cfs!r}.map {{ |n|
    [n, !CustomField.find_by(type: 'UserCustomField', name: n).nil?]
  }}.to_h

  te_provenance = {expected_te_cfs!r}.map {{ |n|
    [n, !CustomField.find_by(type: 'TimeEntryCustomField', name: n).nil?]
  }}.to_h

  te = TimeEntry.where(entity_type: 'WorkPackage', entity_id: wp_ids)

  # Relations involving this project on either endpoint. Reused below
  # for the total count and the two orphan-detection queries.
  project_relations = Relation.where('from_id IN (?) OR to_id IN (?)', wp_ids, wp_ids)

  {{
    'project_id' => proj.id,
    'project_identifier' => proj.identifier,
    'wp_total' => wps.count,
    'wp_with_subject' => wps.where.not(subject: [nil, '']).count,
    'wp_with_description' => wps.where.not(description: [nil, '']).count,
    'wp_with_assignee' => wps.where.not(assigned_to_id: nil).count,
    'wp_with_author' => wps.where.not(author_id: nil).count,
    'wp_with_due_date' => wps.where.not(due_date: nil).count,
    'wp_with_start_date' => wps.where.not(start_date: nil).count,
    'wp_with_type' => wps.where.not(type_id: nil).count,
    'wp_with_status' => wps.where.not(status_id: nil).count,
    'wp_with_priority' => wps.where.not(priority_id: nil).count,
    'wp_created_in_last_24h' => wps.where("created_at > ?", Time.now - 86400).count,
    'wp_provenance_cfs' => wp_provenance,
    'wp_cf_format_violations' => wp_cf_format_violations,
    'user_provenance_cfs' => user_provenance,
    'te_provenance_cfs' => te_provenance,
    'wp_journal_total' => Journal.where(journable_type: 'WorkPackage', journable_id: wp_ids).count,
    'wp_attachment_total' => Attachment.where(container_type: 'WorkPackage', container_id: wp_ids).count,
    'wp_watcher_total' => Watcher.where(watchable_type: 'WorkPackage', watchable_id: wp_ids).count,
    'te_total' => te.count,
    'te_hours_sum' => te.sum(:hours).to_f,
    'te_distinct_hours_count' => te.distinct.count(:hours),
    'te_min_hours' => (te.minimum(:hours) || 0).to_f,
    'te_max_hours' => (te.maximum(:hours) || 0).to_f,
    # Single OR-query so a relation with both ends inside ``wp_ids``
    # (the common intra-project case) is counted exactly once instead
    # of twice. The downstream zero-threshold heuristic still works:
    # zero stays zero, but non-zero numbers reflect reality.
    'relation_total' => project_relations.count,
    # Orphan detection. A *project relation* with ``from_id`` not in
    # ``work_packages`` can only mean ``to_id IN wp_ids`` AND the from-end
    # is a deleted WP elsewhere — i.e. the "from" endpoint is dangling.
    # Symmetric for the to-end. Watcher orphans fire when a watching
    # user has been deleted without cascade.
    #
    # ``NOT EXISTS`` instead of ``NOT IN (SELECT ...)`` so a NULL in the
    # subquery cannot collapse the entire predicate to ``UNKNOWN`` (the
    # three-valued-logic trap). PKs are ``NOT NULL`` today so behavior
    # is identical, but ``NOT EXISTS`` stays correct if that ever
    # changes and is typically faster.
    'orphaned_relations_from' => project_relations.
      where('NOT EXISTS (SELECT 1 FROM work_packages wp WHERE wp.id = relations.from_id)').count,
    'orphaned_relations_to' => project_relations.
      where('NOT EXISTS (SELECT 1 FROM work_packages wp WHERE wp.id = relations.to_id)').count,
    'orphaned_watchers' => Watcher.where(watchable_type: 'WorkPackage', watchable_id: wp_ids).
      where('NOT EXISTS (SELECT 1 FROM users u WHERE u.id = watchers.user_id)').count,
  }}
end).call
"""


def _classify(metrics: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (failures, warnings) per the migration spec."""
    failures: list[str] = []
    warnings: list[str] = []

    # Surface the Ruby-side error first — the audit script returns
    # ``{"error": "OP project '<key>' not found"}`` when the project
    # identifier doesn't resolve, and downstream rules would otherwise
    # generate misleading "no work packages" output.
    error_msg = metrics.get("error")
    if error_msg:
        failures.append(f"Audit aborted: {error_msg}")
        return failures, warnings

    wp_total = int(metrics.get("wp_total", 0))
    if wp_total == 0:
        failures.append("No work packages found for project (migration likely never ran for it)")
        return failures, warnings

    # All WPs must have author + subject
    if metrics.get("wp_with_author", 0) < wp_total:
        failures.append(f"WPs missing author_id: {wp_total - metrics['wp_with_author']}/{wp_total}")
    if metrics.get("wp_with_subject", 0) < wp_total:
        failures.append(f"WPs missing subject: {wp_total - metrics['wp_with_subject']}/{wp_total}")

    # Assignee — coverage signal (was the bug at <1% before fix)
    assignee_pct = (metrics.get("wp_with_assignee", 0) / wp_total) * 100
    if assignee_pct < 5:
        failures.append(
            f"Suspiciously low assignee coverage: {metrics['wp_with_assignee']}/{wp_total} = {assignee_pct:.1f}%"
            " (Bug A indicator)"
        )

    # created_at preservation — if >50% of WPs were created in the last 24h,
    # update_columns isn't sticking (Bug E indicator)
    created_recent_pct = (metrics.get("wp_created_in_last_24h", 0) / wp_total) * 100
    if created_recent_pct > 50:
        failures.append(
            f"{metrics['wp_created_in_last_24h']}/{wp_total} ({created_recent_pct:.0f}%) WPs have"
            " created_at within last 24h — original Jira timestamps not preserved (Bug E indicator)"
        )

    # Provenance CFs
    wp_provenance = metrics.get("wp_provenance_cfs", {}) or {}
    missing_cfs = [name for name, info in wp_provenance.items() if not info.get("exists")]
    if missing_cfs:
        failures.append(f"WP provenance CFs missing: {missing_cfs} (Bug D indicator)")
    user_provenance = metrics.get("user_provenance_cfs", {}) or {}
    missing_user_cfs = [n for n, exists in user_provenance.items() if not exists]
    if missing_user_cfs:
        failures.append(f"User provenance CFs missing: {missing_user_cfs} (Bug D indicator)")
    te_provenance = metrics.get("te_provenance_cfs", {}) or {}
    missing_te_cfs = [n for n, exists in te_provenance.items() if not exists]
    if missing_te_cfs:
        failures.append(f"TimeEntry provenance CFs missing: {missing_te_cfs} (Bug D indicator)")

    # Time entry hours — Bug B indicator
    te_total = metrics.get("te_total", 0)
    if te_total > 0:
        distinct = metrics.get("te_distinct_hours_count", 0)
        if distinct == 1 and metrics.get("te_min_hours") == metrics.get("te_max_hours"):
            failures.append(
                f"All {te_total} TimeEntries have hours = {metrics['te_min_hours']} — units"
                " are not being preserved (Bug B indicator)"
            )

    # Type / Status / Priority — mapping integrity. Any WP with NULL on
    # one of these is a silent mapping failure (the Jira→OP map for the
    # corresponding domain didn't resolve and the WP got persisted
    # anyway).
    for label, key in (
        ("type", "wp_with_type"),
        ("status", "wp_with_status"),
        ("priority", "wp_with_priority"),
    ):
        populated = int(metrics.get(key, 0))
        if populated < wp_total:
            failures.append(
                f"WPs missing {label}_id: {wp_total - populated}/{wp_total}"
                f" — {label} mapping likely failed for those issues",
            )

    # Journal count — Rails auto-emits a Journal on WP create/update.
    # If wp_journal_total < wp_total, journaling is broken (or WPs were
    # bulk-inserted in a way that bypassed the Journal hooks).
    journal_total = int(metrics.get("wp_journal_total", 0))
    if journal_total < wp_total:
        failures.append(
            f"Journal count {journal_total} < wp_total {wp_total} — every WP"
            " creation should emit at least one Journal record",
        )

    # Relations / Watchers — size-gated heuristic warnings. A small
    # project legitimately may have neither, but on a project of
    # ``_HEURISTIC_SIZE_THRESHOLD`` WPs or more, zero is suspicious.
    if wp_total >= _HEURISTIC_SIZE_THRESHOLD:
        if int(metrics.get("relation_total", 0)) == 0:
            warnings.append(
                f"No relations found across {wp_total} WPs — relation"
                " migration may have silently skipped (Bug D2 indicator)",
            )
        if int(metrics.get("wp_watcher_total", 0)) == 0:
            warnings.append(
                f"No watchers found across {wp_total} WPs — watcher migration may have silently skipped",
            )

    # WP CF format validation. The Ruby side counts populated values
    # that don't match the expected regex per CF. Missing key = legacy
    # audit run before this branch — silently skip (zero is healthy).
    # ``int(count or 0)`` so a future Ruby schema that emits ``null``
    # (or a partial-result blob with a missing CF) doesn't crash
    # ``_classify`` with ``TypeError``; a ``None`` collapses to zero.
    wp_cf_violations = metrics.get("wp_cf_format_violations", {}) or {}
    for cf_name, count in wp_cf_violations.items():
        if int(count or 0) > 0:
            failures.append(
                f"{count} populated values of WP CF '{cf_name}' do not match the expected format",
            )

    # Orphan referential integrity. Unlike the type/journal contracts
    # (where missing-key-as-zero is *also* a failure to flag a stale
    # Ruby script), a missing orphan key must stay silent — zero is the
    # healthy baseline and legacy audit runs without these keys would
    # otherwise wrongly fail. Only fire when we get a positive count.
    orphan_rel_from = int(metrics.get("orphaned_relations_from", 0))
    orphan_rel_to = int(metrics.get("orphaned_relations_to", 0))
    orphan_rel_total = orphan_rel_from + orphan_rel_to
    if orphan_rel_total > 0:
        failures.append(
            f"{orphan_rel_total} orphaned relations"
            f" (from-side dangling: {orphan_rel_from},"
            f" to-side dangling: {orphan_rel_to})"
            " — relations reference deleted WPs",
        )
    orphan_watchers = int(metrics.get("orphaned_watchers", 0))
    if orphan_watchers > 0:
        failures.append(
            f"{orphan_watchers} orphaned watchers — user_id references a deleted user",
        )

    # Description coverage (warning only)
    desc_pct = (metrics.get("wp_with_description", 0) / wp_total) * 100
    if desc_pct < 50:
        warnings.append(f"Only {desc_pct:.0f}% of WPs have a description")

    return failures, warnings


def _execute_audit(jira_project_key: str) -> dict[str, Any]:
    """Run the audit Ruby expression via the OpenProject client and return parsed metrics."""
    op_client = OpenProjectClient()
    script = _build_audit_script(jira_project_key)
    # File-based path: writes the JSON to a container tempfile and reads
    # it back, avoiding tmux scrollback parsing.
    return op_client.execute_json_query(script, timeout=120)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: audit one Jira project's migrated WPs in OP."""
    parser = argparse.ArgumentParser(description="Audit a migrated Jira project in OpenProject")
    parser.add_argument("jira_key", help="Jira project key (e.g. NRS)")
    args = parser.parse_args(argv)

    try:
        metrics = _execute_audit(args.jira_key)
    except Exception as exc:
        sys.stderr.write(f"AUDIT_ERROR: {exc}\n")
        return 2

    failures, warnings = _classify(metrics)
    output = {
        "project": args.jira_key,
        "metrics": metrics,
        "failures": failures,
        "warnings": warnings,
        "passed": len(failures) == 0,
    }
    sys.stdout.write(json.dumps(output, indent=2, default=str))
    sys.stdout.write("\n")

    status = "PASS" if not failures else "FAIL"
    sys.stderr.write(
        f"\n{status}: {args.jira_key}: {len(failures)} failures, {len(warnings)} warnings\n",
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
