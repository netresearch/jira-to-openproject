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
import re
import sys
import traceback
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

# Regex specs for User provenance CF *values*. Per the migrator
# (``src/application/components/user_migration.py``,
# ``_build_user_origin_metadata`` and ``create_missing_users``):
#
#   - ``J2O Origin System``: ``"Jira"`` + optional deployment label
#     ("Cloud", "Server", "Data Center") + optional version.
#   - ``J2O External URL``: ``<base>/secure/ViewProfile.jspa?<param>=<val>``.
#
# ``J2O User ID`` and ``J2O User Key`` are *deliberately not* validated
# here. Their values are user-input-derived (account IDs, usernames,
# display names, emails) and the realistic shape varies wildly across
# Jira deployments — any conservative pattern would false-positive on
# legitimate edge cases (display names with spaces, mixed-case keys
# from older Server installs, non-ASCII names). The audit checks they
# *exist* via the population path; format validation is left to the
# migration code's own normalization.
_USER_CF_FORMAT_REGEXES: tuple[tuple[str, str], ...] = (
    # ``Jira`` followed optionally by a whitespace separator and a
    # mix of label/version/punctuation chars. The mandatory whitespace
    # after ``Jira`` (when the optional group is present) blocks
    # typo-style corruptions like ``"Jiraz"`` from passing as valid.
    # The expanded charset (``()-/``) accepts operator-overridden
    # deployment labels — ``user_migration._get_origin_system_label``
    # falls back to ``config.jira_config["deployment"]`` which is a
    # free-form string that may contain parens, slashes, or hyphens
    # (e.g. ``"Jira (Server)"``, ``"Jira Data-Center"``).
    # Forward slash inside the character class MUST be escaped — the
    # Ruby ``/.../`` regex literal terminates at any unescaped ``/``,
    # even one inside ``[...]``. Without ``\/`` the audit's generated
    # Ruby script ends up with a syntax error at load time. Python's
    # ``re`` engine accepts the unescaped form (no ``/.../`` literal
    # syntax), so the regression is invisible to the unit-test loop.
    ("J2O Origin System", r"\AJira(?:\s[\w\s.()\-\/]*)?\z"),
    # ``ViewProfile.jspa?<param>=<value>`` — forward slashes escaped so
    # the Ruby ``/.../`` regex literal doesn't terminate at ``://``.
    ("J2O External URL", r"\Ahttps?:\/\/[^\s]+\/secure\/ViewProfile\.jspa\?[^\s]*\z"),
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

# Regex specs for TimeEntry provenance CF *values*. Per the migrator
# (``src/utils/time_entry_transformer.py``), the dedup-critical
# ``J2O Origin Worklog Key`` is built from one of two unconditional
# formulas:
#
#   - ``f"{issue_key}:{worklog_id}"``  for Jira worklogs
#   - ``f"tempo:{tempo_id}"``          for Tempo worklogs
#
# A regression that corrupts either form breaks dedup on re-run
# silently — the existing populated-count check (PR #179) cannot
# detect a populated-but-malformed value.
#
# Note on the ``tempo:\d+`` arm: Tempo Cloud worklog IDs are documented
# integers and ``time_entry_transformer.py`` reads them as such. If a
# future Tempo API version (or a custom extractor) emits non-numeric
# IDs (UUIDs, strings), this audit would flag every Tempo row as a
# format violation — fail-loud, not silent, but a hint to the future
# maintainer that the ``\d+`` Tempo arm needs updating alongside the
# transformer.
_TE_CF_FORMAT_REGEXES: tuple[tuple[str, str], ...] = (
    ("J2O Origin Worklog Key", r"\A([A-Z][A-Z0-9_]+-\d+:\d+|tempo:\d+)\z"),
)

# Validated against argv before being interpolated into a JQL string —
# a stray quote in a malformed key would otherwise silently change the
# query scope (``NRS" OR project = "PROD`` → both projects).
_JIRA_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\z")

# Acceptable drift between Jira's link count and OP's relation count.
# Per ``MIGRATION_SPEC.md``: "count must equal Jira's link count, ±5%
# tolerance". The tolerance accommodates link-type dedup, cross-project
# links that don't migrate, and rounding from per-issue counting.
_RELATION_TOLERANCE = 0.05

# Acceptable drift between Jira's watcher count and OP's. The spec
# says watchers "should" match (line 40, weaker than the relation
# rule's "must equal"); ±5% lets the audit catch real migration
# regressions while tolerating the occasional locked/disabled user
# whose Jira watch couldn't carry over.
_WATCHER_TOLERANCE = 0.05

# Hard cap for the attachment pagination loop. Defends against a buggy
# proxy / Jira returning the same page repeatedly (so neither the
# empty-page break nor the actual end-of-results triggers) — without
# this cap, ``start_at`` would grow unboundedly and the audit would
# pound the rate-limiter forever. 1000 pages × 100 issues/page =
# 100k issues, well above any real project.
_PAGINATION_MAX_PAGES = 1000


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
    te_cf_format_pairs = ", ".join(f"{name!r} => /{pattern}/" for name, pattern in _TE_CF_FORMAT_REGEXES)
    user_cf_format_pairs = ", ".join(f"{name!r} => /{pattern}/" for name, pattern in _USER_CF_FORMAT_REGEXES)
    return f"""
(lambda do
  proj_key = {jira_project_key!r}.downcase
  proj = Project.find_by(identifier: proj_key)
  next {{ error: "OP project '#{{proj_key}}' not found" }} unless proj

  wps = WorkPackage.where(project_id: proj.id)
  # Reuse the relation as a subquery (``wps.select(:id)``) instead of
  # plucking ids into an IN(?) array. ``pluck`` materialises every WP id
  # in Ruby memory and ships them back to the DB on every CustomValue /
  # Journal / Attachment / Watcher / Relation query in this audit; on
  # large projects (>32k WPs on Postgres, >65k on MySQL) that hits the
  # bind-parameter limit and aborts the audit. ``select(:id)`` lets the
  # DB plan a subquery and keeps the row count off the wire entirely.
  wp_id_scope = wps.select(:id)

  wp_provenance = {{}}
  {expected_wp_cfs!r}.each do |cf_name|
    cf = CustomField.find_by(type: 'WorkPackageCustomField', name: cf_name)
    populated = cf ? CustomValue.where(custom_field_id: cf.id, customized_type: 'WorkPackage').
      where(customized_id: wp_id_scope).where.not(value: [nil, '']).count : 0
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
    # ``pluck.count {{ block }}`` streams the comparison instead of
    # building an intermediate ``reject``-array — same semantics, no
    # per-value allocation overhead on large projects.
    bad = CustomValue.where(custom_field_id: cf.id, customized_type: 'WorkPackage').
      where(customized_id: wp_id_scope).where.not(value: [nil, '']).
      pluck(:value).count {{ |v| v !~ regex }}
    wp_cf_format_violations[cf_name] = bad
  end

  user_provenance = {expected_user_cfs!r}.map {{ |n|
    [n, !CustomField.find_by(type: 'UserCustomField', name: n).nil?]
  }}.to_h

  # Per-CF format-violation count for User provenance CFs. Same shape
  # as the WP/TE versions (``pluck.count {{ block }}`` streams the
  # comparison without an intermediate array). Scoped to *all*
  # populated values across ALL users — User CFs are global and the
  # audit project doesn't restrict the user set.
  user_cf_format_violations = {{}}
  {{ {user_cf_format_pairs} }}.each do |cf_name, regex|
    cf = CustomField.find_by(type: 'UserCustomField', name: cf_name)
    next unless cf
    bad = CustomValue.where(custom_field_id: cf.id, customized_type: 'User').
      where.not(value: [nil, '']).
      pluck(:value).count {{ |v| v !~ regex }}
    user_cf_format_violations[cf_name] = bad
  end

  te_provenance = {expected_te_cfs!r}.map {{ |n|
    [n, !CustomField.find_by(type: 'TimeEntryCustomField', name: n).nil?]
  }}.to_h

  te = TimeEntry.where(entity_type: 'WorkPackage', entity_id: wp_id_scope)

  # Per-TE population of ``J2O Origin Worklog Key``. The spec mandates
  # this CF on every migrated TimeEntry — it's the dedup key on re-run.
  # Below ``te_total`` means duplicate worklogs would slip through on
  # the next migration pass. Passing ``te`` (an ``ActiveRecord::Relation``)
  # straight into ``customized_id:`` generates a subquery instead of
  # materializing every TE id in Ruby — keeps the query fast on large
  # projects and avoids any DB parameter/packet limits.
  worklog_key_cf = CustomField.find_by(type: 'TimeEntryCustomField', name: 'J2O Origin Worklog Key')
  te_with_worklog_key = worklog_key_cf ?
    CustomValue.where(custom_field_id: worklog_key_cf.id, customized_type: 'TimeEntry').
      where(customized_id: te.select(:id)).where.not(value: [nil, '']).count : 0

  # Per-CF format-violation count for TE provenance CFs. Same shape as
  # the WP version above (pluck → ``count {{ block }}`` streams the
  # comparison without an intermediate array). The dedup-critical
  # ``J2O Origin Worklog Key`` is the only TE CF the migrator
  # populates per-row; corrupting its format silently breaks
  # idempotent re-runs.
  te_cf_format_violations = {{}}
  {{ {te_cf_format_pairs} }}.each do |cf_name, regex|
    cf = CustomField.find_by(type: 'TimeEntryCustomField', name: cf_name)
    next unless cf
    bad = CustomValue.where(custom_field_id: cf.id, customized_type: 'TimeEntry').
      where(customized_id: te.select(:id)).where.not(value: [nil, '']).
      pluck(:value).count {{ |v| v !~ regex }}
    te_cf_format_violations[cf_name] = bad
  end

  # Relations involving this project on either endpoint. Reused below
  # for the total count and the two orphan-detection queries. Use
  # ``.or`` with two scoped relations rather than a raw IN-array string
  # — Rails turns each side into a subquery against ``wp_id_scope``,
  # avoiding the bind-parameter blowup on large projects.
  project_relations = Relation.where(from_id: wp_id_scope).or(Relation.where(to_id: wp_id_scope))

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
    'te_cf_format_violations' => te_cf_format_violations,
    'user_cf_format_violations' => user_cf_format_violations,
    'user_provenance_cfs' => user_provenance,
    'te_provenance_cfs' => te_provenance,
    'wp_journal_total' => Journal.where(journable_type: 'WorkPackage', journable_id: wp_id_scope).count,
    'wp_attachment_total' => Attachment.where(container_type: 'WorkPackage', container_id: wp_id_scope).count,
    'wp_watcher_total' => Watcher.where(watchable_type: 'WorkPackage', watchable_id: wp_id_scope).count,
    'te_total' => te.count,
    'te_with_worklog_key' => te_with_worklog_key,
    'te_hours_sum' => te.sum(:hours).to_f,
    'te_distinct_hours_count' => te.distinct.count(:hours),
    'te_min_hours' => (te.minimum(:hours) || 0).to_f,
    'te_max_hours' => (te.maximum(:hours) || 0).to_f,
    # Single ``.or`` query so a relation with both ends inside the
    # project (the common intra-project case) is counted exactly once
    # instead of twice. The downstream zero-threshold heuristic still
    # works: zero stays zero, but non-zero numbers reflect reality.
    'relation_total' => project_relations.count,
    # Orphan detection. A *project relation* with ``from_id`` not in
    # ``work_packages`` can only mean the to-end is in the project
    # AND the from-end is a deleted WP elsewhere — i.e. the "from"
    # endpoint is dangling.
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
    'orphaned_watchers' => Watcher.where(watchable_type: 'WorkPackage', watchable_id: wp_id_scope).
      where('NOT EXISTS (SELECT 1 FROM users u WHERE u.id = watchers.user_id)').count,
  }}
end).call
"""


def _metric_int(metrics: dict[str, Any], key: str, default: int = 0) -> int:
    """Read a numeric metric, coercing missing key OR JSON ``null`` to default.

    The audit's Ruby script returns a JSON dict whose values become the
    Python ``metrics`` dict here. A future Ruby schema can fail this
    contract two ways: omit a key (Python sees missing) or emit
    ``nil`` / ``null`` (Python sees ``None``). Plain ``int(metrics.get(key, 0))``
    only handles the first — ``int(None)`` raises ``TypeError`` and
    turns a data-quality signal into a hard tool failure with no
    actionable message. This helper handles both.

    Note the explicit ``is None`` check rather than the simpler
    ``metrics.get(key, default) or default``: the latter would collapse
    a legitimate ``0`` to ``default`` when ``default != 0`` (because
    ``0 or 5 == 5``). All current callers pass ``default=0`` so the
    distinction is dormant, but the contract advertised by the
    signature must hold for any future caller that passes a non-zero
    default.
    """
    value = metrics.get(key)
    return default if value is None else int(value)


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

    wp_total = _metric_int(metrics, "wp_total")
    if wp_total == 0:
        failures.append("No work packages found for project (migration likely never ran for it)")
        return failures, warnings

    # All WPs must have author + subject
    wp_with_author = _metric_int(metrics, "wp_with_author")
    if wp_with_author < wp_total:
        failures.append(f"WPs missing author_id: {wp_total - wp_with_author}/{wp_total}")
    wp_with_subject = _metric_int(metrics, "wp_with_subject")
    if wp_with_subject < wp_total:
        failures.append(f"WPs missing subject: {wp_total - wp_with_subject}/{wp_total}")

    # Assignee — coverage signal (was the bug at <1% before fix)
    wp_with_assignee = _metric_int(metrics, "wp_with_assignee")
    assignee_pct = (wp_with_assignee / wp_total) * 100
    if assignee_pct < 5:
        failures.append(
            f"Suspiciously low assignee coverage: {wp_with_assignee}/{wp_total} = {assignee_pct:.1f}% (Bug A indicator)"
        )

    # created_at preservation — if >50% of WPs were created in the last 24h,
    # update_columns isn't sticking (Bug E indicator)
    wp_created_recent = _metric_int(metrics, "wp_created_in_last_24h")
    created_recent_pct = (wp_created_recent / wp_total) * 100
    if created_recent_pct > 50:
        failures.append(
            f"{wp_created_recent}/{wp_total} ({created_recent_pct:.0f}%) WPs have"
            " created_at within last 24h — original Jira timestamps not preserved (Bug E indicator)"
        )

    # Provenance CFs
    wp_provenance = metrics.get("wp_provenance_cfs", {}) or {}
    missing_cfs = [name for name, info in wp_provenance.items() if not info.get("exists")]
    if missing_cfs:
        failures.append(f"WP provenance CFs missing: {missing_cfs} (Bug D indicator)")

    # Per-CF *population* check — distinct from the existence check
    # above. A CF can ``exists=True, populated=0`` when the migration
    # created the CustomField record but never wrote any CustomValue
    # rows (e.g. migration ran before the populating fix landed).
    # Caught by the live TEST audit: every WP CF showed populated=0
    # because the migration was pre-#175. Per spec, ``J2O Origin
    # Key`` is hard-required ("must" line 27); the others are "should"
    # — fail vs warn accordingly. Skip CFs that didn't exist (the
    # missing-CF rule above already claims them with a clearer
    # message).
    for cf_name, info in wp_provenance.items():
        if not info.get("exists"):
            continue
        # Use ``_metric_int`` — same null-coercion contract as every
        # other numeric read in this function. The previous ``int(... or 0)``
        # form was inconsistent with the sibling metric reads and would
        # collapse a legitimate ``0`` differently from a missing key.
        populated = _metric_int(info, "populated")
        if populated >= wp_total:
            continue
        if cf_name == "J2O Origin Key":
            failures.append(
                f"WP CF '{cf_name}' under-populated: {populated}/{wp_total}"
                " — hard-required per spec (dedup + provenance broken)",
            )
        else:
            warnings.append(
                f"WP CF '{cf_name}' under-populated: {populated}/{wp_total}",
            )

    user_provenance = metrics.get("user_provenance_cfs", {}) or {}
    missing_user_cfs = [n for n, exists in user_provenance.items() if not exists]
    if missing_user_cfs:
        failures.append(f"User provenance CFs missing: {missing_user_cfs} (Bug D indicator)")
    te_provenance = metrics.get("te_provenance_cfs", {}) or {}
    missing_te_cfs = [n for n, exists in te_provenance.items() if not exists]
    if missing_te_cfs:
        failures.append(f"TimeEntry provenance CFs missing: {missing_te_cfs} (Bug D indicator)")

    # Time entry hours — Bug B indicator
    te_total = _metric_int(metrics, "te_total")
    if te_total > 0:
        distinct = _metric_int(metrics, "te_distinct_hours_count")
        te_min_hours = metrics.get("te_min_hours")
        te_max_hours = metrics.get("te_max_hours")
        # Explicit not-None guard: ``None == None`` is True in Python,
        # which would otherwise let an all-keys-missing metrics dict fire
        # the rule with a meaningless ``hours = None`` message.
        if distinct == 1 and te_min_hours is not None and te_min_hours == te_max_hours:
            failures.append(
                f"All {te_total} TimeEntries have hours = {te_min_hours} — units"
                " are not being preserved (Bug B indicator)"
            )
        # ``J2O Origin Worklog Key`` MUST be populated on every migrated
        # TE. Below ``te_total`` means dedup on re-run will silently fail
        # for the unmarked entries (each rerun would create duplicates).
        # Missing key in metrics → 0 → fail loud (Ruby/Python skew guard).
        te_with_wlk = _metric_int(metrics, "te_with_worklog_key")
        if te_with_wlk < te_total:
            failures.append(
                f"TimeEntry 'J2O Origin Worklog Key' population:"
                f" {te_with_wlk}/{te_total} — dedup on re-run will silently"
                " fail for entries missing the key",
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
        populated = _metric_int(metrics, key)
        if populated < wp_total:
            failures.append(
                f"WPs missing {label}_id: {wp_total - populated}/{wp_total}"
                f" — {label} mapping likely failed for those issues",
            )

    # Journal count — Rails auto-emits a Journal on WP create/update.
    # If wp_journal_total < wp_total, journaling is broken (or WPs were
    # bulk-inserted in a way that bypassed the Journal hooks).
    journal_total = _metric_int(metrics, "wp_journal_total")
    if journal_total < wp_total:
        failures.append(
            f"Journal count {journal_total} < wp_total {wp_total} — every WP"
            " creation should emit at least one Journal record",
        )

    # Relations / Watchers — size-gated heuristic warnings. A small
    # project legitimately may have neither, but on a project of
    # ``_HEURISTIC_SIZE_THRESHOLD`` WPs or more, zero is suspicious.
    if wp_total >= _HEURISTIC_SIZE_THRESHOLD:
        if _metric_int(metrics, "relation_total") == 0:
            warnings.append(
                f"No relations found across {wp_total} WPs — relation"
                " migration may have silently skipped (Bug D2 indicator)",
            )
        if _metric_int(metrics, "wp_watcher_total") == 0:
            warnings.append(
                f"No watchers found across {wp_total} WPs — watcher migration may have silently skipped",
            )

    # Jira source comparison: issue count. The audit is otherwise
    # OP-side only — without this check, a wholesale loss (e.g. 1000
    # Jira issues → 800 OP WPs) passes every other rule. Spec mandates
    # exact match; any delta is a failure. ``None`` = the audit
    # couldn't reach Jira (no creds / network) — warn so operators see
    # the gap, but don't block the OP-side report. Missing key = legacy
    # audit run before this branch — silent (zero false positives on
    # cached metrics blobs).
    if "jira_issue_count" in metrics:
        jira_count = metrics["jira_issue_count"]
        if jira_count is None:
            warnings.append(
                "Jira source comparison unavailable — issue-count check skipped",
            )
        elif int(jira_count) != wp_total:
            failures.append(
                f"Jira→OP issue count mismatch: Jira reports {jira_count}, OP has"
                f" {wp_total} ({int(jira_count) - wp_total:+d}) — wholesale data loss",
            )

    # Jira source comparison: attachment count. Per spec, attachments
    # must match exactly — any non-zero delta means silent attachment
    # loss (file-too-large, backend rejected, transformer bug) or a
    # phantom OP-side artifact. ``None`` warns ("source unavailable")
    # without blocking the OP-side report. Missing key = legacy run.
    if "jira_attachment_count" in metrics:
        jira_att = metrics["jira_attachment_count"]
        if jira_att is None:
            warnings.append(
                "Jira attachment source comparison unavailable — check skipped",
            )
        else:
            op_att = _metric_int(metrics, "wp_attachment_total")
            if int(jira_att) != op_att:
                failures.append(
                    f"Jira→OP attachment count mismatch: Jira reports {jira_att},"
                    f" OP has {op_att} ({int(jira_att) - op_att:+d}) — silent"
                    " attachment loss or phantom OP-side artifacts",
                )

    # Jira source comparison: watcher count, ±5% tolerance. The spec
    # uses "should equal" rather than "must equal" for watchers
    # (locked users / disabled accounts won't migrate their watches),
    # so this is a softer signal than the relation rule but still
    # catches wholesale watcher loss.
    if "jira_watcher_count" in metrics:
        jira_watch = metrics["jira_watcher_count"]
        if jira_watch is None:
            warnings.append(
                "Jira watcher source comparison unavailable — check skipped",
            )
        else:
            op_watch = _metric_int(metrics, "wp_watcher_total")
            jira_watch_int = int(jira_watch)
            if jira_watch_int == 0:
                tolerance_ok = op_watch == 0
            else:
                delta_pct = abs(op_watch - jira_watch_int) / jira_watch_int
                tolerance_ok = delta_pct <= _WATCHER_TOLERANCE
            if not tolerance_ok:
                failures.append(
                    f"Jira→OP watcher count mismatch beyond ±5%:"
                    f" Jira reports {jira_watch_int}, OP has {op_watch}"
                    f" ({op_watch - jira_watch_int:+d}) — watch records dropped"
                    " or duplicated during migration",
                )

    # Jira source comparison: relation (issue-link) count, ±5%
    # tolerance per spec. Replaces the size-gated "zero-relations
    # warning" heuristic from #176 with an exact source comparison
    # whenever Jira data is available. Both directions out-of-band
    # are failures: a low OP count means relations were dropped on
    # migration; a high OP count means duplicates leaked through.
    # Prefer the cross/intra breakdown (added 2026-05-07): the legacy
    # ``raw // 2`` halved count over-counts on projects whose issues
    # link heavily to other projects (NRS: 75% audit false-positive).
    # Only intra-project links migrate, so we compare OP to the intra
    # count and surface the cross count as informational.
    breakdown = metrics.get("jira_relation_breakdown")
    if isinstance(breakdown, dict):
        op_rel = _metric_int(metrics, "relation_total")
        intra_unique = int(breakdown.get("intra_unique") or 0)
        cross = int(breakdown.get("cross") or 0)
        if intra_unique == 0:
            tolerance_ok = op_rel == 0
        else:
            delta_pct = abs(op_rel - intra_unique) / intra_unique
            tolerance_ok = delta_pct <= _RELATION_TOLERANCE
        if not tolerance_ok:
            failures.append(
                f"Jira→OP intra-project relation count mismatch beyond ±5%:"
                f" Jira intra={intra_unique}, OP has {op_rel}"
                f" ({op_rel - intra_unique:+d}) — relations dropped or"
                " duplicated during migration",
            )
        if cross:
            warnings.append(
                f"Jira→OP cross-project relations not migrated: {cross}"
                " — informational, not loss (cross-project links are"
                " out of scope per MIGRATION_SPEC.md; the matched"
                " project is single-scope)",
            )
    elif "jira_relation_count" in metrics and metrics["jira_relation_count"] is None:
        warnings.append(
            "Jira relation source comparison unavailable — check skipped",
        )
    elif "jira_relation_count" in metrics:
        # Pre-breakdown legacy fallback. Still needed for fixture-based
        # classifier tests that bypass ``_execute_audit`` and only set
        # ``jira_relation_count``. Same halving caveat as before.
        jira_rel_int = int(metrics["jira_relation_count"])
        op_rel = _metric_int(metrics, "relation_total")
        if jira_rel_int == 0:
            tolerance_ok = op_rel == 0
        else:
            delta_pct = abs(op_rel - jira_rel_int) / jira_rel_int
            tolerance_ok = delta_pct <= _RELATION_TOLERANCE
        if not tolerance_ok:
            failures.append(
                f"Jira→OP relation count mismatch beyond ±5%:"
                f" Jira reports {jira_rel_int}, OP has {op_rel}"
                f" ({op_rel - jira_rel_int:+d}) — relations dropped or"
                " duplicated during migration",
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

    # TE CF format validation (parallel to WP). Same missing-key
    # contract: silent on absent metric (legacy audit run); fails
    # loud only when a populated value doesn't match its regex.
    te_cf_violations = metrics.get("te_cf_format_violations", {}) or {}
    for cf_name, count in te_cf_violations.items():
        if int(count or 0) > 0:
            failures.append(
                f"{count} populated values of TimeEntry CF '{cf_name}' do not match the expected format"
                " — dedup on re-run depends on this format",
            )

    # User CF format validation (parallel to WP/TE). Scoped only to the
    # two CFs whose value formats are deterministic — user IDs and
    # keys are intentionally not validated (see _USER_CF_FORMAT_REGEXES
    # docstring).
    user_cf_violations = metrics.get("user_cf_format_violations", {}) or {}
    for cf_name, count in user_cf_violations.items():
        if int(count or 0) > 0:
            failures.append(
                f"{count} populated values of User CF '{cf_name}' do not match the expected format",
            )

    # Orphan referential integrity. Unlike the type/journal contracts
    # (where missing-key-as-zero is *also* a failure to flag a stale
    # Ruby script), a missing orphan key must stay silent — zero is the
    # healthy baseline and legacy audit runs without these keys would
    # otherwise wrongly fail. Only fire when we get a positive count.
    orphan_rel_from = _metric_int(metrics, "orphaned_relations_from")
    orphan_rel_to = _metric_int(metrics, "orphaned_relations_to")
    orphan_rel_total = orphan_rel_from + orphan_rel_to
    if orphan_rel_total > 0:
        failures.append(
            f"{orphan_rel_total} orphaned relations"
            f" (from-side dangling: {orphan_rel_from},"
            f" to-side dangling: {orphan_rel_to})"
            " — relations reference deleted WPs",
        )
    orphan_watchers = _metric_int(metrics, "orphaned_watchers")
    if orphan_watchers > 0:
        failures.append(
            f"{orphan_watchers} orphaned watchers — user_id references a deleted user",
        )

    # Description coverage (warning only)
    desc_pct = (_metric_int(metrics, "wp_with_description") / wp_total) * 100
    if desc_pct < 50:
        warnings.append(f"Only {desc_pct:.0f}% of WPs have a description")

    return failures, warnings


def _fetch_jira_issue_count(jira_project_key: str) -> int | None:
    """Best-effort: ask Jira how many issues live in this project.

    Returns ``None`` if Jira can't be reached for any reason (no
    creds, network down, project not found, auth error). The audit
    must not fail closed on Jira-side issues — operators may
    legitimately run it without Jira creds, and the OP-side checks
    still produce useful output. A ``None`` flows through ``_classify``
    as a warning ("source comparison unavailable"), not a failure.

    Imported lazily so the module imports cleanly even when Jira
    config is absent (e.g. in unit tests that exercise the
    classifier directly).
    """
    try:
        from src.infrastructure.jira.jira_client import JiraClient
    except ImportError as exc:
        sys.stderr.write(
            f"[audit] Jira source comparison skipped — could not import JiraClient: {exc}\n",
        )
        return None
    try:
        jira = JiraClient()
        return int(jira.get_issue_count(jira_project_key))
    except Exception as exc:
        # Surface the underlying error on stderr so operators can tell
        # "no creds" (expected) from "JiraClient is broken" (a real
        # bug). The audit's stdout JSON stays clean; the warning
        # emitted by ``_classify`` keeps the high-level signal, while
        # the trace below preserves a forensic trail. ``ValueError``
        # from ``int(...)`` (malformed upstream response) and network
        # errors land here together — the trace tells them apart.
        sys.stderr.write(
            f"[audit] Jira source comparison skipped — {type(exc).__name__}: {exc}\n",
        )
        sys.stderr.write(traceback.format_exc())
        return None


def _paginated_per_issue_field_count(
    jira_project_key: str,
    *,
    jira_field: str,
    attr_name: str,
    label: str,
) -> int | None:
    """Best-effort: paginate ``search_issues`` summing a per-issue list field.

    Used by both :func:`_fetch_jira_attachment_count` and
    :func:`_fetch_jira_relation_count` — they differ only in *which*
    Jira field to fetch (``attachment`` vs. ``issuelinks``) and which
    Issue attribute to count (same names). ``label`` is the human-
    facing word that goes into the stderr-trace messages
    (``"attachment"`` / ``"relation"``).

    Returns ``None`` on any failure (no creds, network down, project
    not found, mid-pagination error, auth, CAPTCHA, malformed
    response, hit-the-page-cap). Stderr is written with full
    traceback to preserve a forensic trail; stdout JSON stays clean.

    **Pagination correctness.** Two subtle bugs an earlier draft of
    the attachment-counter had:

    1. ``start_at`` MUST advance by ``len(page)``, not by the requested
       ``page_size``. Jira Server / Data Center caps ``maxResults`` via
       ``jira.search.views.default.max`` (commonly 50 or 100,
       configurable). Requesting 100 and receiving 50 must still mean
       "page complete, more may follow". The obvious heuristic
       ``if len(page) < page_size: break`` silently truncates the
       entire project past page 1 — the exact silent-failure class
       this audit tool exists to catch.
    2. The loop needs a hard cap. A buggy proxy that returns the same
       page repeatedly (``len(page) == page_size`` forever) would
       otherwise spin until the rate-limiter melts. ``for...else``
       fires ``return None`` if the cap is hit so a buggy upstream
       presents as "source unavailable" rather than a silent count.

    Project key is regex-validated before the JQL is built — a stray
    quote in argv would otherwise silently change the query scope
    (``NRS" OR project = "PROD`` would query both).

    The lazy import + broad ``except`` mirror the issue-count helper
    so the audit module imports cleanly without Jira config — the
    classifier unit tests rely on this.
    """
    if not _JIRA_PROJECT_KEY_RE.match(jira_project_key):
        sys.stderr.write(
            f"[audit] Jira {label} comparison skipped — invalid project key"
            f" {jira_project_key!r} (expected uppercase Jira key like 'NRS')\n",
        )
        return None
    try:
        from src.infrastructure.jira.jira_client import JiraClient
    except ImportError as exc:
        sys.stderr.write(
            f"[audit] Jira {label} comparison skipped — could not import JiraClient: {exc}\n",
        )
        return None
    try:
        jira = JiraClient()
        # ``jira.jira`` is the underlying ``python-jira`` JIRA instance
        # used elsewhere in src/infrastructure/jira/ for paginated
        # search calls (see jira_search_service.py:64-72).
        underlying = jira.jira
        page_size = 100
        start_at = 0
        total = 0
        jql = f'project = "{jira_project_key}"'
        for _ in range(_PAGINATION_MAX_PAGES):
            page = underlying.search_issues(
                jql,
                startAt=start_at,
                maxResults=page_size,
                fields=jira_field,
                expand="",
            )
            if not page:
                break
            for issue in page:
                # python-jira's ``Issue.__getattr__`` raises
                # ``AttributeError`` when ``.fields`` is missing on a
                # partial / cached / permission-restricted response.
                # The broad ``except`` below would otherwise collapse
                # the WHOLE audit to "source unavailable" because of
                # one bad issue, losing the partial count we already
                # accumulated. Guard ``.fields`` first; skip the
                # broken issue and keep summing.
                fields_obj = getattr(issue, "fields", None)
                if fields_obj is None:
                    continue
                items = getattr(fields_obj, attr_name, None) or []
                total += len(items)
            # Advance by what the server actually returned, not by what
            # we asked for. See the docstring for why.
            start_at += len(page)
        else:
            sys.stderr.write(
                f"[audit] Jira {label} pagination hit the {_PAGINATION_MAX_PAGES}"
                f"-page safety cap for project {jira_project_key!r} — likely a buggy"
                " upstream returning the same page repeatedly\n",
            )
            return None
    except Exception as exc:
        sys.stderr.write(
            f"[audit] Jira {label} comparison skipped — {type(exc).__name__}: {exc}\n",
        )
        sys.stderr.write(traceback.format_exc())
        return None
    return total


def _fetch_jira_attachment_count(jira_project_key: str) -> int | None:
    """Best-effort: count total attachments across all issues in the project."""
    return _paginated_per_issue_field_count(
        jira_project_key,
        jira_field="attachment",
        attr_name="attachment",
        label="attachment",
    )


def _fetch_jira_watcher_count(jira_project_key: str) -> int | None:
    """Best-effort: sum ``watchCount`` across all issues in the project.

    Each ``issue.fields.watches.watchCount`` is the number of distinct
    users watching that issue. Summing across project issues gives the
    total ``(user, issue)`` watch pairs — the same shape OP stores in
    its ``Watcher`` table (one row per pair).

    **Permission scope caveat.** ``watchCount`` is filtered by the
    calling user's *view voters and watchers* project permission. If
    the audit token has less scope than the migration admin token did,
    this helper will systematically *under*-report and the classifier
    will then incorrectly flag ``OP > Jira`` as "duplicates leaked
    through" — when the real cause is missing audit-token permission.
    Run the audit with the same scope as the migration ran with.

    Implementation parallels :func:`_paginated_per_issue_field_count`
    but reads a per-issue *scalar* (``watches.watchCount``) instead of
    a list length. The whole shared-helper-via-``label`` approach
    would only make the parameterization noisier here, so this is a
    near-copy with the value-extraction line as the only divergence.
    All the pagination correctness invariants from #184 still apply
    (advance by ``len(page)``, hard cap via ``for...else``, regex-
    validate the project key).
    """
    if not _JIRA_PROJECT_KEY_RE.match(jira_project_key):
        sys.stderr.write(
            f"[audit] Jira watcher comparison skipped — invalid project key"
            f" {jira_project_key!r} (expected uppercase Jira key like 'NRS')\n",
        )
        return None
    try:
        from src.infrastructure.jira.jira_client import JiraClient
    except ImportError as exc:
        sys.stderr.write(
            f"[audit] Jira watcher comparison skipped — could not import JiraClient: {exc}\n",
        )
        return None
    try:
        jira = JiraClient()
        underlying = jira.jira
        page_size = 100
        start_at = 0
        total = 0
        jql = f'project = "{jira_project_key}"'
        for _ in range(_PAGINATION_MAX_PAGES):
            page = underlying.search_issues(
                jql,
                startAt=start_at,
                maxResults=page_size,
                fields="watches",
                expand="",
            )
            if not page:
                break
            for issue in page:
                # Same guard as ``_paginated_per_issue_field_count`` —
                # ``issue.fields`` may be missing on partial / cached
                # responses. Without this check, one bad issue
                # collapses the whole audit to "source unavailable".
                fields_obj = getattr(issue, "fields", None)
                if fields_obj is None:
                    continue
                watches = getattr(fields_obj, "watches", None)
                if watches is None:
                    continue
                # ``python-jira`` usually returns ``Watcher`` resource
                # objects but some code paths (raw cache, partial
                # responses) yield a dict. ``getattr`` on a dict
                # returns the default and would silently zero the
                # count — explicitly branch on shape.
                if isinstance(watches, dict):
                    raw_count = watches.get("watchCount", 0)
                else:
                    raw_count = getattr(watches, "watchCount", 0)
                total += int(raw_count or 0)
            start_at += len(page)
        else:
            sys.stderr.write(
                f"[audit] Jira watcher pagination hit the {_PAGINATION_MAX_PAGES}"
                f"-page safety cap for project {jira_project_key!r} — likely a buggy"
                " upstream returning the same page repeatedly\n",
            )
            return None
    except Exception as exc:
        sys.stderr.write(
            f"[audit] Jira watcher comparison skipped — {type(exc).__name__}: {exc}\n",
        )
        sys.stderr.write(traceback.format_exc())
        return None
    return total


def _fetch_jira_relation_breakdown(jira_project_key: str) -> dict[str, int] | None:
    """Best-effort: classify Jira issuelinks as intra-project vs cross-project.

    The previous halving model (``raw // 2``) assumed intra-project
    links dominate, which holds for small projects but breaks badly
    on real-world projects whose issues link heavily to *other*
    projects. Live 2026-05-07 NRS audit: 4624 raw issuelinks split
    as 1173 intra + 3451 cross — halving reported 2312, OP had 591
    (which roughly matches the 586 unique intra pairs) → audit
    failed by -1759 even though the migration was lossless on
    intra-project relations (cross-project links don't migrate by
    design, per ``MIGRATION_SPEC.md``).

    Returns a dict with three counts, or ``None`` on Jira failure:

    * ``intra_unique`` — distinct unordered pairs ``{a, b}`` where
      both ends are in this project. Each appears twice in the raw
      stream (once per end); deduped via ``frozenset``. This is the
      number OP's ``relation_total`` should match.
    * ``cross`` — links where exactly one end is in this project.
      Counted once each (only the in-project end carries them in
      the per-issue iteration). Informational; cross-project links
      do not migrate.
    * ``raw`` — total issuelink entries summed across all issues.
      Kept for back-compat with the legacy ``jira_relation_count``
      metric (= ``raw // 2``) and to surface the "odd raw"
      diagnostic if present.

    Pagination + project-key validation reuse the same hardening as
    :func:`_paginated_per_issue_field_count`.
    """
    if not _JIRA_PROJECT_KEY_RE.match(jira_project_key):
        sys.stderr.write(
            f"[audit] Jira relation breakdown skipped — invalid project key"
            f" {jira_project_key!r} (expected uppercase Jira key like 'NRS')\n",
        )
        return None
    try:
        from src.infrastructure.jira.jira_client import JiraClient
    except ImportError as exc:
        sys.stderr.write(
            f"[audit] Jira relation breakdown skipped — could not import JiraClient: {exc}\n",
        )
        return None

    project_prefix = f"{jira_project_key}-"
    intra_pairs: set[frozenset[str]] = set()
    cross_count = 0
    raw_count = 0

    try:
        jira = JiraClient()
        underlying = jira.jira
        page_size = 100
        start_at = 0
        jql = f'project = "{jira_project_key}"'
        for _ in range(_PAGINATION_MAX_PAGES):
            page = underlying.search_issues(
                jql,
                startAt=start_at,
                maxResults=page_size,
                fields="issuelinks",
                expand="",
            )
            if not page:
                break
            for issue in page:
                fields_obj = getattr(issue, "fields", None)
                if fields_obj is None:
                    continue
                source_key = getattr(issue, "key", None)
                if not source_key:
                    continue
                links = getattr(fields_obj, "issuelinks", None) or []
                for link in links:
                    raw_count += 1
                    target_key: str | None = None
                    outward = getattr(link, "outwardIssue", None)
                    inward = getattr(link, "inwardIssue", None)
                    if outward is not None:
                        target_key = getattr(outward, "key", None)
                    elif inward is not None:
                        target_key = getattr(inward, "key", None)
                    if not target_key:
                        # Malformed link — neither outward nor inward
                        # carries a key. Skip from both buckets so we
                        # don't double-count.
                        continue
                    if target_key.startswith(project_prefix):
                        # Intra-project: dedup by sorted pair so each
                        # link contributes exactly once.
                        intra_pairs.add(frozenset((source_key, target_key)))
                    else:
                        cross_count += 1
            start_at += len(page)
        else:
            sys.stderr.write(
                f"[audit] Jira relation pagination hit the {_PAGINATION_MAX_PAGES}"
                f"-page safety cap for project {jira_project_key!r} — likely a buggy"
                " upstream returning the same page repeatedly\n",
            )
            return None
    except Exception as exc:
        sys.stderr.write(
            f"[audit] Jira relation breakdown skipped — {type(exc).__name__}: {exc}\n",
        )
        sys.stderr.write(traceback.format_exc())
        return None

    return {
        "intra_unique": len(intra_pairs),
        "cross": cross_count,
        "raw": raw_count,
    }


def _fetch_jira_relation_count(jira_project_key: str) -> int | None:
    """Back-compat shim: returns the legacy ``raw // 2`` halved count.

    The classifier prefers ``jira_relation_breakdown`` when present
    (set by :func:`_execute_audit`); this function exists so older
    callers / fixtures that read ``metrics["jira_relation_count"]``
    keep working. The halving model is documented as broken for
    cross-project-heavy projects in
    :func:`_fetch_jira_relation_breakdown`.
    """
    raw = _paginated_per_issue_field_count(
        jira_project_key,
        jira_field="issuelinks",
        attr_name="issuelinks",
        label="relation",
    )
    if raw is None:
        return None
    if raw % 2 != 0:
        # Odd raw count means at least one cross-project link, which
        # the floor-division below silently rounds down. Surface the
        # asymmetry on stderr — preserved here so legacy callers /
        # tests that exercise this shim still see the same diagnostic.
        sys.stderr.write(
            f"[audit] Jira raw issuelinks count for {jira_project_key!r}"
            f" is odd ({raw}) — at least one cross-project link is"
            " present; the floor-divided count below may be 0.5 low,"
            " which on small projects can push past ±5% tolerance\n",
        )
    return raw // 2


def _execute_audit(jira_project_key: str) -> dict[str, Any]:
    """Run the audit Ruby expression via the OpenProject client and return parsed metrics."""
    op_client = OpenProjectClient()
    script = _build_audit_script(jira_project_key)
    # File-based path: writes the JSON to a container tempfile and reads
    # it back, avoiding tmux scrollback parsing.
    metrics: dict[str, Any] = op_client.execute_json_query(script, timeout=120)
    # Best-effort source comparison. Any Jira-side error collapses to
    # ``None`` and surfaces as a warning in the classifier; the OP-side
    # report is still valid.
    metrics["jira_issue_count"] = _fetch_jira_issue_count(jira_project_key)
    metrics["jira_attachment_count"] = _fetch_jira_attachment_count(jira_project_key)
    # Prefer the breakdown over the legacy halved count — the latter
    # over-counts on cross-project-heavy projects (NRS audit caught
    # this: -1759 false positive, see _fetch_jira_relation_breakdown).
    breakdown = _fetch_jira_relation_breakdown(jira_project_key)
    if breakdown is not None:
        metrics["jira_relation_breakdown"] = breakdown
        # Keep the legacy ``jira_relation_count`` populated for any
        # downstream tooling that still reads it; derive from the same
        # raw count so the two stay consistent.
        metrics["jira_relation_count"] = breakdown["raw"] // 2
    else:
        metrics["jira_relation_breakdown"] = None
        metrics["jira_relation_count"] = None
    metrics["jira_watcher_count"] = _fetch_jira_watcher_count(jira_project_key)
    return metrics


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
