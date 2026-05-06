"""Unit tests for ``tools.audit_migrated_project._classify``.

The classifier turns the Ruby-side metrics hash (returned by the OP
Rails console) into the human-readable failures + warnings lists. This
file pins the heuristic rules so future audit-tool changes don't
silently weaken post-migration validation.

Each test seeds the smallest realistic metrics dict required to trip
(or not trip) one rule.
"""

from __future__ import annotations

from typing import Any

from tools.audit_migrated_project import _classify


def _baseline_metrics(**overrides: Any) -> dict[str, Any]:
    """Return a healthy 100-WP baseline; tests override only the field they exercise."""
    base: dict[str, Any] = {
        "project_id": 1,
        "project_identifier": "test",
        "wp_total": 100,
        "wp_with_subject": 100,
        "wp_with_description": 100,
        "wp_with_assignee": 80,
        "wp_with_author": 100,
        "wp_with_due_date": 0,
        "wp_with_start_date": 0,
        "wp_with_type": 100,
        "wp_with_status": 100,
        "wp_with_priority": 100,
        "wp_created_in_last_24h": 0,
        "wp_provenance_cfs": {
            "J2O Origin Key": {"exists": True, "populated": 100},
            "J2O Origin ID": {"exists": True, "populated": 100},
            "J2O Origin System": {"exists": True, "populated": 100},
            "J2O Origin URL": {"exists": True, "populated": 100},
            "J2O Project Key": {"exists": True, "populated": 100},
            "J2O Project ID": {"exists": True, "populated": 100},
            "J2O First Migration Date": {"exists": True, "populated": 100},
            "J2O Last Update Date": {"exists": True, "populated": 100},
        },
        "user_provenance_cfs": {
            "J2O Origin System": True,
            "J2O User ID": True,
            "J2O User Key": True,
            "J2O External URL": True,
        },
        "te_provenance_cfs": {
            "J2O Origin Worklog Key": True,
            "J2O Origin Issue ID": True,
            "J2O Origin Issue Key": True,
            "J2O Origin System": True,
            "J2O First Migration Date": True,
            "J2O Last Update Date": True,
        },
        "wp_journal_total": 100,
        "wp_attachment_total": 0,
        "wp_watcher_total": 50,
        "te_total": 10,
        "te_with_worklog_key": 10,
        "te_hours_sum": 12.5,
        "te_distinct_hours_count": 5,
        "te_min_hours": 0.25,
        "te_max_hours": 4.0,
        "relation_total": 30,
    }
    base.update(overrides)
    return base


def test_baseline_metrics_pass() -> None:
    """The healthy baseline must produce zero failures and zero warnings."""
    failures, warnings = _classify(_baseline_metrics())
    assert failures == []
    assert warnings == []


# --- Bug F: Type/Status/Priority NULL on WPs ----------------------------------


def test_wps_missing_type_is_failure() -> None:
    """A NULL ``type_id`` on any WP is a hard failure (mapping broke silently)."""
    failures, _warnings = _classify(_baseline_metrics(wp_with_type=99))
    assert any("type" in f.lower() for f in failures), failures


def test_wps_missing_status_is_failure() -> None:
    failures, _warnings = _classify(_baseline_metrics(wp_with_status=98))
    assert any("status" in f.lower() for f in failures), failures


def test_wps_missing_priority_is_failure() -> None:
    failures, _warnings = _classify(_baseline_metrics(wp_with_priority=0))
    assert any("priority" in f.lower() for f in failures), failures


def test_missing_type_field_treated_as_zero() -> None:
    """If the Ruby side hasn't been updated, missing key still produces a failure."""
    metrics = _baseline_metrics()
    del metrics["wp_with_type"]
    failures, _warnings = _classify(metrics)
    assert any("type" in f.lower() for f in failures), failures


# --- Bug I: Journal count below WP count --------------------------------------


def test_journal_count_below_wp_count_is_failure() -> None:
    """Rails auto-creates a journal on every WP creation; <wp_total means broken."""
    failures, _warnings = _classify(_baseline_metrics(wp_journal_total=50))
    assert any("journal" in f.lower() for f in failures), failures


def test_journal_count_equal_to_wp_count_passes() -> None:
    failures, _warnings = _classify(_baseline_metrics(wp_journal_total=100))
    assert not any("journal" in f.lower() for f in failures), failures


def test_journal_count_above_wp_count_passes() -> None:
    """Multiple journals per WP (edits, comments) is normal."""
    failures, _warnings = _classify(_baseline_metrics(wp_journal_total=500))
    assert not any("journal" in f.lower() for f in failures), failures


def test_missing_journal_field_treated_as_zero() -> None:
    """A missing ``wp_journal_total`` key must fail-loud (Ruby/Python skew guard).

    Same contract as ``test_missing_type_field_treated_as_zero``: the
    ``metrics.get(..., 0)`` default is intentional — if the audit hash
    is missing a key we expect, treat it as zero so the rule fires
    rather than silently passing.
    """
    metrics = _baseline_metrics()
    del metrics["wp_journal_total"]
    failures, _warnings = _classify(metrics)
    assert any("journal" in f.lower() for f in failures), failures


# --- Bug D2: Relation count zero heuristic (warning only) ---------------------


def test_relation_zero_with_many_wps_warns() -> None:
    """Big project + zero relations = suspicious but not fatal (small projects can be 0)."""
    _failures, warnings = _classify(_baseline_metrics(relation_total=0))
    assert any("relation" in w.lower() for w in warnings), warnings


def test_relation_zero_with_few_wps_does_not_warn() -> None:
    """Below threshold, zero relations is plausible."""
    _failures, warnings = _classify(
        _baseline_metrics(
            wp_total=10,
            wp_with_subject=10,
            wp_with_description=10,
            wp_with_assignee=10,
            wp_with_author=10,
            wp_with_type=10,
            wp_with_status=10,
            wp_with_priority=10,
            wp_journal_total=10,
            relation_total=0,
            wp_provenance_cfs={k: {"exists": True, "populated": 10} for k in _baseline_metrics()["wp_provenance_cfs"]},
        ),
    )
    assert not any("relation" in w.lower() for w in warnings), warnings


# --- Watcher zero heuristic (warning only) ------------------------------------


def test_watcher_zero_with_many_wps_warns() -> None:
    _failures, warnings = _classify(_baseline_metrics(wp_watcher_total=0))
    assert any("watcher" in w.lower() for w in warnings), warnings


def test_watcher_present_does_not_warn() -> None:
    _failures, warnings = _classify(_baseline_metrics(wp_watcher_total=5))
    assert not any("watcher" in w.lower() for w in warnings), warnings


# --- Orphan referential integrity (relations / watchers) ---------------------
# A "project relation" is one where either ``from_id`` OR ``to_id`` is in the
# project's WP IDs. A relation is *orphaned* when the *other* endpoint
# references a WP that no longer exists (typically because that WP was
# deleted in another project without its relations cascading). Watchers are
# orphaned when ``user_id`` references a deleted user.


def test_orphaned_relations_from_is_failure() -> None:
    """A non-zero ``orphaned_relations_from`` count must fail."""
    failures, _warnings = _classify(_baseline_metrics(orphaned_relations_from=2))
    assert any("orphan" in f.lower() and "relation" in f.lower() for f in failures), failures


def test_orphaned_relations_to_is_failure() -> None:
    failures, _warnings = _classify(_baseline_metrics(orphaned_relations_to=1))
    assert any("orphan" in f.lower() and "relation" in f.lower() for f in failures), failures


def test_orphaned_watchers_is_failure() -> None:
    failures, _warnings = _classify(_baseline_metrics(orphaned_watchers=1))
    assert any("orphan" in f.lower() and "watcher" in f.lower() for f in failures), failures


def test_zero_orphans_passes() -> None:
    """All orphan counts at zero produce no orphan-related failure."""
    failures, _warnings = _classify(
        _baseline_metrics(
            orphaned_relations_from=0,
            orphaned_relations_to=0,
            orphaned_watchers=0,
        ),
    )
    assert not any("orphan" in f.lower() for f in failures), failures


def test_missing_orphan_fields_treated_as_zero() -> None:
    """Missing orphan keys must NOT fire as failures.

    Unlike the type/journal contracts (where missing-key-as-zero is a
    *failure*), an orphan count of zero is the *healthy* baseline. The
    rule must therefore be silent when keys are absent — otherwise every
    legacy audit run would suddenly fail on this branch.
    """
    metrics = _baseline_metrics()
    failures, _warnings = _classify(metrics)
    assert not any("orphan" in f.lower() for f in failures), failures


# --- WP CF value-format validation -------------------------------------------
# Provenance CFs are populated by the migrator with specific shapes
# (e.g. ``J2O Origin Key`` is always a Jira issue key like ``NRS-123``).
# Existence + populated-count alone does not catch a regression that
# silently corrupts the value (wrong format, truncation, missing
# prefix). The Ruby side counts per-CF format violations; the
# classifier fails on any non-zero violation count.


def test_wp_cf_format_violation_is_failure() -> None:
    """One violation in any tracked WP CF must fail with the CF name."""
    failures, _warnings = _classify(
        _baseline_metrics(wp_cf_format_violations={"J2O Origin Key": 3}),
    )
    assert any("format" in f.lower() and "J2O Origin Key" in f for f in failures), failures


def test_wp_cf_format_zero_violations_passes() -> None:
    """An all-zero violations dict must produce no failures."""
    failures, _warnings = _classify(
        _baseline_metrics(
            wp_cf_format_violations={
                "J2O Origin Key": 0,
                "J2O Origin ID": 0,
                "J2O Origin URL": 0,
            },
        ),
    )
    assert not any("format" in f.lower() for f in failures), failures


def test_wp_cf_format_multiple_violations_each_reported() -> None:
    """Each violating CF gets its own line so operators can pinpoint the bad field."""
    failures, _warnings = _classify(
        _baseline_metrics(
            wp_cf_format_violations={
                "J2O Origin Key": 2,
                "J2O Origin URL": 1,
                "J2O Project Key": 0,
            },
        ),
    )
    failed_cfs = [f for f in failures if "format" in f.lower()]
    assert len(failed_cfs) == 2, failed_cfs
    assert any("J2O Origin Key" in f for f in failed_cfs)
    assert any("J2O Origin URL" in f for f in failed_cfs)


def test_wp_cf_format_null_count_does_not_crash() -> None:
    """A ``None`` count for a CF must collapse to zero, not raise.

    Defends against a Ruby schema change or partial-result blob where
    the violation count comes back as JSON ``null``. ``int(None)`` would
    crash ``_classify`` with ``TypeError`` and turn a data-quality
    signal into a hard tool failure with no actionable message.
    """
    failures, _warnings = _classify(
        _baseline_metrics(
            wp_cf_format_violations={"J2O Origin Key": None, "J2O Origin ID": 0},
        ),
    )
    assert not any("format" in f.lower() for f in failures), failures


def test_wp_cf_format_missing_field_treated_as_silent() -> None:
    """A missing ``wp_cf_format_violations`` key must NOT fail.

    Same contract as the orphan rule: a missing key means a legacy
    audit run from before this branch. Zero is the healthy baseline;
    silently skipping the check on absent metric is correct.
    """
    metrics = _baseline_metrics()
    failures, _warnings = _classify(metrics)
    assert not any("format" in f.lower() for f in failures), failures


# --- TE CF value-format validation ------------------------------------------
# Parallel to the WP CF format check. The dedup-critical
# ``J2O Origin Worklog Key`` is built from one of two formulas
# (``<JIRA_KEY>:<id>`` or ``tempo:<id>``) — a regression that corrupts
# the value silently breaks dedup on re-run.


def test_te_cf_format_violation_is_failure() -> None:
    """One violation must fail with the CF name and the dedup hint."""
    failures, _warnings = _classify(
        _baseline_metrics(te_cf_format_violations={"J2O Origin Worklog Key": 2}),
    )
    assert any("format" in f.lower() and "J2O Origin Worklog Key" in f and "dedup" in f.lower() for f in failures), (
        failures
    )


def test_te_cf_format_zero_violations_passes() -> None:
    failures, _warnings = _classify(
        _baseline_metrics(te_cf_format_violations={"J2O Origin Worklog Key": 0}),
    )
    assert not any("format" in f.lower() and "TimeEntry" in f for f in failures), failures


def test_te_cf_format_missing_field_treated_as_silent() -> None:
    """Missing key = legacy audit run, must NOT fail (zero is healthy)."""
    metrics = _baseline_metrics()
    failures, _warnings = _classify(metrics)
    assert not any("format" in f.lower() and "TimeEntry" in f for f in failures), failures


def test_te_cf_format_null_count_does_not_crash() -> None:
    """A ``None`` count for a CF must collapse to zero, not raise.

    Same contract as ``test_wp_cf_format_null_count_does_not_crash`` —
    defends against a Ruby schema change emitting JSON ``null``.
    """
    failures, _warnings = _classify(
        _baseline_metrics(te_cf_format_violations={"J2O Origin Worklog Key": None}),
    )
    assert not any("format" in f.lower() and "TimeEntry" in f for f in failures), failures


# --- User CF value-format validation -----------------------------------------
# Parallel to WP/TE CF format checks. Scoped to the two User CFs whose
# value formats are deterministic — ``J2O Origin System`` (always
# ``Jira[ + variant]``) and ``J2O External URL``
# (``ViewProfile.jspa`` link). User ID/Key are deliberately skipped
# because their values are user-input-derived (display names, mixed-case
# keys, non-ASCII), and any conservative pattern would false-positive.


def test_user_cf_format_violation_is_failure() -> None:
    failures, _warnings = _classify(
        _baseline_metrics(user_cf_format_violations={"J2O Origin System": 3}),
    )
    assert any("format" in f.lower() and "User CF" in f and "J2O Origin System" in f for f in failures), failures


def test_user_cf_format_zero_violations_passes() -> None:
    failures, _warnings = _classify(
        _baseline_metrics(
            user_cf_format_violations={"J2O Origin System": 0, "J2O External URL": 0},
        ),
    )
    assert not any("format" in f.lower() and "User CF" in f for f in failures), failures


def test_user_cf_format_multiple_violations_each_reported() -> None:
    failures, _warnings = _classify(
        _baseline_metrics(
            user_cf_format_violations={"J2O Origin System": 1, "J2O External URL": 2},
        ),
    )
    failed_user_cfs = [f for f in failures if "User CF" in f and "format" in f.lower()]
    assert len(failed_user_cfs) == 2, failed_user_cfs


def test_user_cf_format_missing_field_treated_as_silent() -> None:
    """Legacy audit-run contract: missing key is healthy (zero), not a failure."""
    metrics = _baseline_metrics()
    failures, _warnings = _classify(metrics)
    assert not any("format" in f.lower() and "User CF" in f for f in failures), failures


def test_user_cf_format_null_count_does_not_crash() -> None:
    """``None`` count collapses to zero, not raise — Ruby schema-skew guard."""
    failures, _warnings = _classify(
        _baseline_metrics(user_cf_format_violations={"J2O Origin System": None}),
    )
    assert not any("format" in f.lower() and "User CF" in f for f in failures), failures


def test_user_origin_system_regex_rejects_typo_corruption() -> None:
    """The ``Origin System`` regex must not pass typo-style corruption.

    Pins the mandatory whitespace separator: ``"Jiraz"``,
    ``"Jirabad"``, ``"Jira_corrupted"`` (no whitespace after ``Jira``)
    must all be rejected. Without the whitespace anchor, the regex
    would silently accept arbitrary suffixes attached to ``"Jira"``.
    """
    import re

    from tools.audit_migrated_project import _USER_CF_FORMAT_REGEXES

    pattern = dict(_USER_CF_FORMAT_REGEXES)["J2O Origin System"]
    rx = re.compile(pattern)
    for bad in ("Jiraz", "Jirabad", "Jira_corrupted", "JiraX"):
        assert rx.match(bad) is None, f"regex accepted corrupted value {bad!r}"


def test_user_origin_system_regex_accepts_operator_override_punctuation() -> None:
    """The ``Origin System`` regex must accept operator-typed deployment labels.

    ``user_migration._get_origin_system_label`` falls back to
    ``config.jira_config["deployment"]`` — a free-form string that may
    legitimately contain parens, slashes, and hyphens (e.g.
    ``"Jira (Server)"``, ``"Jira Data-Center"``, ``"Jira Cloud/v9"``).
    A regex that's too tight here would false-positive on real values.
    """
    import re

    from tools.audit_migrated_project import _USER_CF_FORMAT_REGEXES

    pattern = dict(_USER_CF_FORMAT_REGEXES)["J2O Origin System"]
    rx = re.compile(pattern)
    for good in (
        "Jira",
        "Jira Cloud",
        "Jira Server v9.1",
        "Jira (Server)",
        "Jira Data-Center",
        "Jira Cloud/v9",
        "Jira Data Center 9.1.2",
    ):
        assert rx.match(good) is not None, f"regex rejected legit value {good!r}"


# --- TimeEntry Origin Worklog Key population --------------------------------
# Per spec, ``J2O Origin Worklog Key`` MUST be populated on every migrated
# TimeEntry — it's the dedup key on re-runs. Existence-of-the-CF alone is
# not enough; missing values let duplicate worklogs slip through silently.


def test_te_worklog_key_population_below_te_total_is_failure() -> None:
    """A populated count below ``te_total`` must fail with a dedup hint."""
    failures, _warnings = _classify(
        _baseline_metrics(te_total=10, te_with_worklog_key=7),
    )
    assert any("worklog" in f.lower() and ("dedup" in f.lower() or "re-run" in f.lower()) for f in failures), failures


def test_te_worklog_key_population_equals_te_total_passes() -> None:
    """100% population is the healthy state."""
    failures, _warnings = _classify(
        _baseline_metrics(te_total=10, te_with_worklog_key=10),
    )
    assert not any("worklog" in f.lower() for f in failures), failures


def test_te_worklog_key_zero_te_total_is_silent() -> None:
    """If there are no TimeEntries to begin with, the rule must not fire."""
    failures, _warnings = _classify(_baseline_metrics(te_total=0, te_with_worklog_key=0))
    assert not any("worklog" in f.lower() for f in failures), failures


def test_te_worklog_key_missing_field_treated_as_zero() -> None:
    """Missing key fails loud (same contract as type/journal — Ruby/Python skew)."""
    metrics = _baseline_metrics(te_total=5)
    del metrics["te_with_worklog_key"]
    failures, _warnings = _classify(metrics)
    assert any("worklog" in f.lower() for f in failures), failures


def test_classify_does_not_crash_when_wp_total_is_null() -> None:
    """``wp_total`` itself goes through the helper — None must not crash.

    Sibling to the all-other-metrics-null smoke. Setting ``wp_total=None``
    short-circuits via the ``wp_total == 0`` early-return, so this only
    exercises the *first* helper call. Without it, a future regression
    that removes ``_metric_int`` from the wp_total site would slip past
    the other smoke (which keeps wp_total=100 to exercise downstream).
    """
    failures, _warnings = _classify(_baseline_metrics(wp_total=None))
    assert isinstance(failures, list)
    assert any("no work packages" in f.lower() for f in failures), failures


def test_metric_int_preserves_zero_with_non_zero_default() -> None:
    """``_metric_int(default=N)`` must return 0 when the value is 0, not N.

    Pins the explicit ``is None`` branch in the helper. A naive
    ``metrics.get(key, default) or default`` would return ``default``
    on a legitimate 0 (because ``0 or N == N``), silently masking a
    "zero is the actual count" signal whenever a caller passes a
    non-zero default. All current callers use ``default=0``, but the
    signature advertises non-zero defaults — this test makes sure the
    body honors that.
    """
    from tools.audit_migrated_project import _metric_int

    assert _metric_int({"k": 0}, "k", default=5) == 0
    assert _metric_int({"k": None}, "k", default=5) == 5
    assert _metric_int({}, "k", default=5) == 5
    assert _metric_int({"k": 7}, "k", default=5) == 7


def test_classify_does_not_crash_when_all_numeric_metrics_are_null() -> None:
    """Every numeric metric site must coerce ``None`` to 0 instead of raising.

    Defends against a future Ruby schema change emitting JSON ``null``
    on any metric (a conditional branch that returns ``nil`` instead of
    0, a partial-result blob, etc.). Without uniform ``or 0`` coercion,
    Python ``int(None)`` and arithmetic-with-None crash ``_classify``
    and turn a data-quality signal into a hard tool failure.

    Smoke test: blanks every numeric site simultaneously and verifies
    ``_classify`` returns normally. The actual outcome (lots of
    failures, since 0 < wp_total triggers most rules) is the secondary
    signal — the primary contract under test is "doesn't raise".
    """
    metrics = _baseline_metrics(
        wp_with_author=None,
        wp_with_subject=None,
        wp_with_assignee=None,
        wp_created_in_last_24h=None,
        wp_with_type=None,
        wp_with_status=None,
        wp_with_priority=None,
        wp_with_description=None,
        wp_journal_total=None,
        wp_watcher_total=None,
        relation_total=None,
        te_total=None,
        te_with_worklog_key=None,
        te_distinct_hours_count=None,
        orphaned_relations_from=None,
        orphaned_relations_to=None,
        orphaned_watchers=None,
    )
    failures, _warnings = _classify(metrics)
    assert isinstance(failures, list)


def test_te_worklog_key_null_value_does_not_crash() -> None:
    """A ``None`` value (e.g. future Ruby branch emits ``nil``) must not raise.

    Defends against a Ruby schema change where ``te_with_worklog_key``
    comes back as JSON ``null``. ``int(None)`` would raise ``TypeError``
    and turn a data-quality signal into a hard tool failure with no
    actionable message — same contract as PR #178's CF-format rule.
    """
    failures, _warnings = _classify(
        _baseline_metrics(te_total=5, te_with_worklog_key=None),
    )
    # None collapses to 0 → 0 < 5 → still fails loud, just doesn't crash.
    assert any("worklog" in f.lower() for f in failures), failures


# --- Jira source comparison: issue count ------------------------------------
# Without comparing to the Jira source, the audit can verify OP-side
# consistency but not "did everything actually migrate". A wholesale
# data loss (1000 Jira issues → 800 OP WPs) would currently pass every
# OP-side rule. This adds the first source-side check: exact issue count
# match (per spec: issues should be exact).


def test_jira_issue_count_match_passes() -> None:
    """``jira_issue_count == wp_total`` is the healthy state."""
    failures, _warnings = _classify(_baseline_metrics(jira_issue_count=100))
    assert not any("jira" in f.lower() for f in failures), failures


def test_jira_issue_count_mismatch_is_failure() -> None:
    """Any delta between Jira and OP issue counts is a hard failure."""
    failures, _warnings = _classify(_baseline_metrics(jira_issue_count=120))
    assert any("jira" in f.lower() and "100" in f and "120" in f for f in failures), failures


def test_jira_issue_count_lower_than_wp_total_is_failure() -> None:
    """Reverse direction (OP > Jira) also fails — both indicate inconsistency."""
    failures, _warnings = _classify(_baseline_metrics(jira_issue_count=80))
    assert any("jira" in f.lower() for f in failures), failures


def test_jira_issue_count_none_warns_source_unavailable() -> None:
    """If Jira couldn't be reached, emit a warning, not a failure.

    Audit shouldn't fail closed on Jira-side issues — operators may
    legitimately run the audit without Jira creds. ``None`` is the
    sentinel for "source comparison unavailable".
    """
    _failures, warnings = _classify(_baseline_metrics(jira_issue_count=None))
    assert any("jira" in w.lower() and ("source" in w.lower() or "unavailable" in w.lower()) for w in warnings), (
        warnings
    )


def test_jira_issue_count_missing_field_treated_as_silent() -> None:
    """Missing key = legacy audit run, must NOT fail or warn (silent contract)."""
    metrics = _baseline_metrics()
    failures, warnings = _classify(metrics)
    assert not any("jira" in f.lower() for f in failures), failures
    assert not any("jira" in w.lower() for w in warnings), warnings


# --- Jira source comparison: attachment count -------------------------------
# Per spec: attachment count must EXACTLY equal Jira's. Any silent
# attachment loss (file-too-large, OP backend rejected, transformer bug)
# would currently slip through every other rule.


def test_jira_attachment_count_match_passes() -> None:
    failures, _warnings = _classify(
        _baseline_metrics(jira_attachment_count=0, wp_attachment_total=0),
    )
    assert not any("attachment" in f.lower() for f in failures), failures


def test_jira_attachment_count_mismatch_is_failure() -> None:
    """Any non-zero delta is a hard failure — exact match required by spec."""
    failures, _warnings = _classify(
        _baseline_metrics(jira_attachment_count=42, wp_attachment_total=40),
    )
    assert any("attachment" in f.lower() and "42" in f and "40" in f for f in failures), failures


def test_jira_attachment_count_zero_jira_with_op_attachments_is_failure() -> None:
    """OP > Jira = phantom attachments somehow appeared in OP. Real bug."""
    failures, _warnings = _classify(
        _baseline_metrics(jira_attachment_count=0, wp_attachment_total=5),
    )
    assert any("attachment" in f.lower() for f in failures), failures


def test_jira_attachment_count_none_warns_source_unavailable() -> None:
    _failures, warnings = _classify(_baseline_metrics(jira_attachment_count=None))
    assert any("attachment" in w.lower() and ("source" in w.lower() or "unavailable" in w.lower()) for w in warnings), (
        warnings
    )


def test_jira_attachment_count_missing_field_treated_as_silent() -> None:
    """Missing key = legacy audit run; rule must not fire."""
    metrics = _baseline_metrics()
    failures, warnings = _classify(metrics)
    assert not any("attachment" in f.lower() and "jira" in f.lower() for f in failures), failures
    assert not any("attachment" in w.lower() and "jira" in w.lower() for w in warnings), warnings


def test_fetch_jira_attachment_count_paginates_when_server_caps_maxresults(monkeypatch) -> None:
    """The helper must page through all results when Jira caps maxResults.

    Jira Server / Data Center enforces ``jira.search.views.default.max``
    (commonly 50). Requesting ``maxResults=100`` then receiving 50
    must still mean "page complete, more may follow". An earlier draft
    used ``if len(page) < page_size: break`` which silently truncated
    the count to page 1 — the exact silent-failure class this tool
    exists to catch.

    The fake returns 50 items per page (capping the helper's request
    of 100) across two pages, then an empty terminator. With correct
    pagination the helper sums attachments across BOTH pages.
    """
    import sys as _sys
    import types

    class _FakeFields:
        def __init__(self, attachments_per_issue: int) -> None:
            self.attachment = [object()] * attachments_per_issue

    class _FakeIssue:
        def __init__(self, attachments_per_issue: int) -> None:
            self.fields = _FakeFields(attachments_per_issue)

    pages = [
        [_FakeIssue(1) for _ in range(50)],  # page 1: 50 issues × 1 attachment
        [_FakeIssue(2) for _ in range(50)],  # page 2: 50 issues × 2 attachments
        [],  # page 3: empty terminator
    ]
    page_iter = iter(pages)

    class _FakeUnderlying:
        def search_issues(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return next(page_iter)

    class _FakeJiraClient:
        def __init__(self) -> None:
            self.jira = _FakeUnderlying()

    fake_module = types.ModuleType("src.infrastructure.jira.jira_client")
    fake_module.JiraClient = _FakeJiraClient  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "src.infrastructure.jira.jira_client", fake_module)

    from tools.audit_migrated_project import _fetch_jira_attachment_count

    result = _fetch_jira_attachment_count("NRS")
    # Correct pagination: 50×1 + 50×2 = 150. Buggy pagination would
    # have stopped after page 1 and returned 50.
    assert result == 150


# --- Jira source comparison: relation count ---------------------------------
# Per spec, relations should be within ±5% of the Jira link count. Phase 1
# (#176) added a "zero relations on a >=50 WP project" heuristic warning;
# this Phase 3 rule replaces the heuristic with an exact source comparison
# (with tolerance) when Jira data is available.

_RELATION_TOLERANCE = 0.05


def test_jira_relation_count_within_tolerance_passes() -> None:
    """Within ±5% of Jira's link count is acceptable per spec."""
    # OP=30 baseline, Jira=31 (within 5% of 30)
    failures, _warnings = _classify(_baseline_metrics(jira_relation_count=31))
    assert not any("relation" in f.lower() and "jira" in f.lower() for f in failures), failures


def test_jira_relation_count_above_tolerance_is_failure() -> None:
    """Beyond ±5% means relation migration silently dropped or duplicated links."""
    # OP=30 baseline, Jira=50 → ~67% high → out of tolerance
    failures, _warnings = _classify(_baseline_metrics(jira_relation_count=50))
    assert any("relation" in f.lower() and "jira" in f.lower() and "5" in f for f in failures), failures


def test_jira_relation_count_below_tolerance_is_failure() -> None:
    """Reverse direction (OP > Jira) also fails."""
    # OP=30 baseline, Jira=10 → 67% low → out of tolerance
    failures, _warnings = _classify(_baseline_metrics(jira_relation_count=10))
    assert any("relation" in f.lower() and "jira" in f.lower() for f in failures), failures


def test_jira_relation_count_zero_jira_zero_op_passes() -> None:
    """Both ends zero is healthy (no relations expected, none found)."""
    failures, _warnings = _classify(
        _baseline_metrics(jira_relation_count=0, relation_total=0, wp_total=10),
    )
    assert not any("relation" in f.lower() and "jira" in f.lower() for f in failures), failures


def test_jira_relation_count_none_warns_source_unavailable() -> None:
    _failures, warnings = _classify(_baseline_metrics(jira_relation_count=None))
    assert any(
        "relation" in w.lower() and "jira" in w.lower() and ("source" in w.lower() or "unavailable" in w.lower())
        for w in warnings
    ), warnings


def test_jira_relation_count_missing_field_treated_as_silent() -> None:
    metrics = _baseline_metrics()
    failures, warnings = _classify(metrics)
    assert not any("relation" in f.lower() and "jira" in f.lower() for f in failures), failures
    # Missing key: existing zero-heuristic warning may still fire from the
    # ``relation_total < 50`` rule; here we just check the new Jira rule
    # didn't add its own warning.
    assert not any("relation" in w.lower() and "jira" in w.lower() for w in warnings), warnings


# --- Jira source comparison: watcher count -----------------------------------
# Per spec, watchers "should" equal Jira's count (softer than the
# relation "must" wording). ±5% tolerance lets the audit catch real
# regressions while tolerating locked/disabled users whose watches
# can't migrate.


def test_jira_watcher_count_within_tolerance_passes() -> None:
    # OP=50 baseline, Jira=51 → within 5%
    failures, _warnings = _classify(_baseline_metrics(jira_watcher_count=51))
    assert not any("watcher" in f.lower() and "jira" in f.lower() for f in failures), failures


def test_jira_watcher_count_above_tolerance_is_failure() -> None:
    # OP=50 baseline, Jira=80 → 60% high → fails
    failures, _warnings = _classify(_baseline_metrics(jira_watcher_count=80))
    assert any("watcher" in f.lower() and "jira" in f.lower() for f in failures), failures


def test_jira_watcher_count_below_tolerance_is_failure() -> None:
    # OP=50 baseline, Jira=10 → 80% low → fails
    failures, _warnings = _classify(_baseline_metrics(jira_watcher_count=10))
    assert any("watcher" in f.lower() and "jira" in f.lower() for f in failures), failures


def test_jira_watcher_count_zero_jira_zero_op_passes() -> None:
    failures, _warnings = _classify(
        _baseline_metrics(jira_watcher_count=0, wp_watcher_total=0, wp_total=10),
    )
    assert not any("watcher" in f.lower() and "jira" in f.lower() for f in failures), failures


def test_jira_watcher_count_none_warns_source_unavailable() -> None:
    _failures, warnings = _classify(_baseline_metrics(jira_watcher_count=None))
    assert any(
        "watcher" in w.lower() and "jira" in w.lower() and ("source" in w.lower() or "unavailable" in w.lower())
        for w in warnings
    ), warnings


def test_jira_watcher_count_missing_field_treated_as_silent() -> None:
    metrics = _baseline_metrics()
    failures, warnings = _classify(metrics)
    # Existing zero-watchers heuristic warning may fire (it's gated on
    # ``wp_total>=50``, not on ``jira_watcher_count``); only assert the
    # NEW Jira-watcher rule didn't add its own failure or warning.
    assert not any("jira" in f.lower() and "watcher" in f.lower() for f in failures), failures
    assert not any("jira" in w.lower() and "watcher" in w.lower() for w in warnings), warnings


def test_fetch_jira_watcher_count_handles_dict_and_object_watches(monkeypatch) -> None:
    """``watches`` may be either a python-jira resource OR a raw dict.

    Defends against the silent-failure path where ``getattr(dict, "watchCount", 0)``
    returns ``0``: a dict-shaped response would silently zero the
    count and the classifier would (wrongly) blame the migration.
    """
    import sys as _sys
    import types

    class _ObjWatches:
        watchCount = 7

    class _Fields:
        def __init__(self, watches: Any) -> None:
            self.watches = watches

    class _Issue:
        def __init__(self, watches: Any) -> None:
            self.fields = _Fields(watches)

    pages = [
        # 2 issues with object-shaped watches (7 each)
        [_Issue(_ObjWatches()), _Issue(_ObjWatches())],
        # 2 issues with dict-shaped watches (3 + 5)
        [_Issue({"watchCount": 3}), _Issue({"watchCount": 5})],
        # 1 issue with no watches attribute (None)
        [_Issue(None)],
        [],  # terminator
    ]
    page_iter = iter(pages)

    class _FakeUnderlying:
        def search_issues(self, *_a: Any, **_kw: Any) -> list[Any]:
            return next(page_iter)

    class _FakeJiraClient:
        def __init__(self) -> None:
            self.jira = _FakeUnderlying()

    fake_module = types.ModuleType("src.infrastructure.jira.jira_client")
    fake_module.JiraClient = _FakeJiraClient  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "src.infrastructure.jira.jira_client", fake_module)

    from tools.audit_migrated_project import _fetch_jira_watcher_count

    # Object pages: 7+7=14, dict pages: 3+5=8, None: 0 → total 22
    assert _fetch_jira_watcher_count("NRS") == 22


def test_fetch_jira_relation_count_halves_raw_to_match_op_semantics(monkeypatch) -> None:
    """``_fetch_jira_relation_count`` must halve the raw issuelinks count.

    Each Jira link contributes 2 entries to ``issuelinks`` summed across
    a project (one inward, one outward); OP's ``relation_total`` counts
    each Relation row once. Removing the ``// 2`` would produce a 2x
    over-count and silently fail the tolerance check on every project
    with any links. This test pins the halving so a future
    "simplification" doesn't break the semantics.

    Even raw → exact halving. The next test pins the odd-raw
    rounding-down behavior + the stderr warning.
    """
    from tools import audit_migrated_project as audit_mod

    # Patch the shared paginator to return a known raw count, bypassing
    # the lazy JiraClient import + the actual pagination loop.
    monkeypatch.setattr(
        audit_mod,
        "_paginated_per_issue_field_count",
        lambda *_a, **_kw: 60,
    )
    assert audit_mod._fetch_jira_relation_count("NRS") == 30


def test_fetch_jira_relation_count_odd_raw_rounds_down_and_warns(
    monkeypatch,
    capsys,
) -> None:
    """Odd raw count = cross-project link present.

    Halving rounds down (silent under-count of half a link) but the
    asymmetry must be surfaced on stderr so an operator investigating
    a tolerance failure can tell "real migration defect" from
    "cross-project asymmetry".
    """
    from tools import audit_migrated_project as audit_mod

    monkeypatch.setattr(
        audit_mod,
        "_paginated_per_issue_field_count",
        lambda *_a, **_kw: 7,
    )
    result = audit_mod._fetch_jira_relation_count("NRS")
    assert result == 3  # 7 // 2
    captured = capsys.readouterr()
    assert "odd" in captured.err.lower(), captured.err
    assert "cross-project" in captured.err.lower(), captured.err


def test_fetch_jira_relation_count_propagates_none_from_paginator(monkeypatch) -> None:
    """``None`` from the paginator (Jira unreachable) must propagate."""
    from tools import audit_migrated_project as audit_mod

    monkeypatch.setattr(
        audit_mod,
        "_paginated_per_issue_field_count",
        lambda *_a, **_kw: None,
    )
    assert audit_mod._fetch_jira_relation_count("NRS") is None


def test_audit_regexes_have_no_unescaped_forward_slash_in_ruby_literal() -> None:
    """Every audit regex must be safe to embed in a Ruby ``/.../`` literal.

    Ruby's regex literal terminates at any unescaped ``/`` — even one
    inside a character class. Python's ``re`` engine has no equivalent
    rule (no literal syntax), so the unit-test loop happily accepts a
    pattern that crashes on the live audit. Pin all three regex maps
    here so the next slash-in-charclass slip fails CI instead of a
    real audit run.

    Caught a real bug shipped in #182: the Origin System pattern
    had an unescaped forward slash inside its character class which
    broke the Ruby parser when the script was loaded in the OP
    rails console.
    """
    import re as _re

    from tools.audit_migrated_project import (
        _TE_CF_FORMAT_REGEXES,
        _USER_CF_FORMAT_REGEXES,
        _WP_CF_FORMAT_REGEXES,
    )

    # Match an unescaped ``/`` — i.e. one not preceded by a backslash.
    # Negative lookbehind handles the simple case; doubled backslash
    # before slash (escape of escape) isn't a pattern any of these
    # specs use, so the simple lookbehind is correct here.
    unescaped_slash = _re.compile(r"(?<!\\)/")
    for label, regexes in (
        ("WP", _WP_CF_FORMAT_REGEXES),
        ("User", _USER_CF_FORMAT_REGEXES),
        ("TE", _TE_CF_FORMAT_REGEXES),
    ):
        for cf_name, pattern in regexes:
            assert not unescaped_slash.search(pattern), (
                f"{label} CF regex {cf_name!r} contains an unescaped '/'"
                f" — this WILL break the Ruby /.../ literal at audit run time:\n"
                f"  {pattern}"
            )


def test_fetch_jira_attachment_count_rejects_invalid_project_key() -> None:
    """Malformed project keys must not be interpolated into JQL.

    Defends against a stray quote in argv silently changing the query
    scope (e.g. ``NRS" OR project = "PROD`` would query both projects).
    """
    from tools.audit_migrated_project import _fetch_jira_attachment_count

    # Each malformed key short-circuits before any Jira call would be
    # attempted — no monkeypatch needed.
    for bad in ('NRS"', "nrs", "NRS PROD", "", "1NRS", "NR-S"):
        assert _fetch_jira_attachment_count(bad) is None


# --- Pre-existing rules still hold (regression guard) -------------------------


def test_error_short_circuit_still_works() -> None:
    failures, warnings = _classify({"error": "OP project 'NRS' not found"})
    assert failures == ["Audit aborted: OP project 'NRS' not found"]
    assert warnings == []


def test_zero_wps_short_circuits_before_new_checks() -> None:
    """If wp_total=0, new heuristic checks must not run (no division, no false positives)."""
    failures, _warnings = _classify(_baseline_metrics(wp_total=0))
    # Single failure about no WPs; not a cascade of NULL-field complaints.
    assert len(failures) == 1
    assert "no work packages" in failures[0].lower()
