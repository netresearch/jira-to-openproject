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
        "wp_watcher_author_auto": 0,
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


def test_fetch_jira_attachment_count_skips_issues_without_fields_attr(monkeypatch) -> None:
    """An ``Issue`` without a populated ``.fields`` must skip cleanly, not raise.

    Caught by a live audit run on NRS — python-jira can return Issue
    objects whose ``.fields`` is missing entirely (partial / cached
    response, permission restriction, server quirk). ``getattr(issue.fields, ...)``
    then triggers the resource ``__getattr__`` which falls through to
    a subscript attempt and raises ``AttributeError`` — the broad
    ``except`` then collapses the whole audit to "source unavailable",
    losing the partial count we DID accumulate.

    Pin: a page mixing well-formed and ``.fields``-less issues must
    still sum the well-formed entries instead of raising.
    """
    import sys as _sys
    import types

    class _Fields:
        def __init__(self, attachments_per_issue: int) -> None:
            self.attachment = [object()] * attachments_per_issue

    class _GoodIssue:
        def __init__(self, attachments_per_issue: int) -> None:
            self.fields = _Fields(attachments_per_issue)

    class _BrokenIssue:
        """Mimics a python-jira Issue whose fields response was incomplete."""

        def __getattr__(self, name: str) -> Any:
            # Mimic resources.Resource.__getattr__'s eventual fall-through:
            # raise AttributeError with the trailing subscript hint.
            raise AttributeError(
                f"<class 'jira.resources.Issue'> object has no attribute {name!r}"
                f" ('Issue' object is not subscriptable)",
            )

    pages = [
        # 1 well-formed, 1 broken, 1 well-formed
        [_GoodIssue(2), _BrokenIssue(), _GoodIssue(3)],
        [],
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

    from tools.audit_migrated_project import _fetch_jira_attachment_count

    # Should sum the two good issues (2 + 3 = 5) and skip the broken one,
    # NOT raise AttributeError, NOT return None.
    assert _fetch_jira_attachment_count("NRS") == 5


def test_fetch_jira_watcher_count_skips_issues_without_fields_attr(monkeypatch) -> None:
    """Watcher helper has the same vulnerability — same hardening required.

    Hasn't been hit by a live audit yet because watcher responses tend
    to be more uniform, but the access pattern ``issue.fields.watches``
    is identical to the attachment helper. Pin the guard now so the
    same silent-failure can't slip through later.
    """
    import sys as _sys
    import types

    class _ObjWatches:
        watchCount = 4

    class _Fields:
        def __init__(self) -> None:
            self.watches = _ObjWatches()

    class _GoodIssue:
        def __init__(self) -> None:
            self.fields = _Fields()

    class _BrokenIssue:
        def __getattr__(self, name: str) -> Any:
            raise AttributeError(
                f"<class 'jira.resources.Issue'> object has no attribute {name!r}",
            )

    pages = [
        [_GoodIssue(), _BrokenIssue(), _GoodIssue()],
        [],
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

    # Two good issues, 4 watchers each = 8; broken one skipped.
    assert _fetch_jira_watcher_count("NRS") == 8


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


# --- Cross-project relation breakdown (added 2026-05-07) ---------------------
# The legacy ``raw // 2`` halved count over-counts on cross-project-heavy
# projects (NRS audit caught a 75% false positive). The new breakdown
# compares OP to intra-project unique pairs and surfaces cross-project
# count as informational. These tests pin both halves of that contract.


def test_jira_relation_breakdown_intra_within_tolerance_passes() -> None:
    """OP matches intra-project Jira links → no failure on relations."""
    # baseline OP=30; intra=29 (within 5%), cross=1000 (irrelevant).
    failures, _warnings = _classify(
        _baseline_metrics(
            jira_relation_breakdown={"intra_unique": 29, "cross": 1000, "raw": 2000},
        ),
    )
    assert not any("relation" in f.lower() and "jira" in f.lower() and "5" in f for f in failures), failures


def test_jira_relation_breakdown_intra_above_tolerance_is_failure() -> None:
    """OP missing intra-project links → failure (regardless of cross count)."""
    # baseline OP=30; intra=100 (way over) — fails.
    failures, _warnings = _classify(
        _baseline_metrics(
            jira_relation_breakdown={"intra_unique": 100, "cross": 0, "raw": 200},
        ),
    )
    assert any("intra-project" in f.lower() and "5" in f for f in failures), failures


def test_jira_relation_breakdown_cross_emits_informational_warning() -> None:
    """Cross-project links produce an informational warning, not a failure.

    Pin: even when OP intra matches Jira intra exactly, the audit
    surfaces the cross-project count so the operator knows how
    much was deliberately not migrated. Live NRS run: 3451 cross
    vs 1173 raw intra (= 586 unique pairs after dedup).
    """
    failures, warnings = _classify(
        _baseline_metrics(
            jira_relation_breakdown={"intra_unique": 30, "cross": 3451, "raw": 4624},
        ),
    )
    assert not any("relation" in f.lower() and "jira" in f.lower() and "5" in f for f in failures), failures
    assert any("cross-project" in w.lower() and "3451" in w for w in warnings), warnings


def test_jira_relation_breakdown_cross_zero_omits_warning() -> None:
    """No cross-project links → no informational warning."""
    _failures, warnings = _classify(
        _baseline_metrics(
            jira_relation_breakdown={"intra_unique": 30, "cross": 0, "raw": 60},
        ),
    )
    assert not any("cross-project" in w.lower() for w in warnings), warnings


def test_jira_relation_breakdown_takes_precedence_over_legacy_count() -> None:
    """When both ``jira_relation_breakdown`` and the legacy halved count are
    present, the breakdown wins.

    Mirrors what ``_execute_audit`` actually populates: both metrics
    are set in the same dict, but the breakdown's intra-only check
    is the authoritative comparison. Pin: a legacy halved count
    that would fail (large cross-project tail) is silently ignored
    in favour of the intra-only check.
    """
    failures, _warnings = _classify(
        _baseline_metrics(
            jira_relation_count=2350,  # legacy halved would FAIL: 30 vs 2350
            jira_relation_breakdown={"intra_unique": 30, "cross": 4640, "raw": 4700},
        ),
    )
    assert not any("relation" in f.lower() and "5" in f for f in failures), failures


def test_jira_relation_breakdown_zero_intra_requires_zero_op() -> None:
    """Both intra-counts at zero is healthy; OP non-zero would fail."""
    failures, _warnings = _classify(
        _baseline_metrics(
            jira_relation_breakdown={"intra_unique": 0, "cross": 5, "raw": 5},
            relation_total=0,
            wp_total=10,
        ),
    )
    assert not any("relation" in f.lower() and "5" in f for f in failures), failures
    failures2, _ = _classify(
        _baseline_metrics(
            jira_relation_breakdown={"intra_unique": 0, "cross": 5, "raw": 5},
            relation_total=3,
            wp_total=10,
        ),
    )
    # OP has 3 relations but Jira intra is 0 → mismatch.
    assert any("relation" in f.lower() and "5" in f for f in failures2), failures2


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


def test_jira_watcher_count_above_tolerance_is_warning() -> None:
    # OP=50 baseline, Jira=80 → 60% high → warning (not failure).
    # Watcher mismatch is a warning, not a failure: count comparison
    # has known noise sources (permission scope on ``watchCount``,
    # author/auto-subscription overlap, cross-project drift) that
    # prevent exact reconciliation. See ``_WATCHER_TOLERANCE`` docstring.
    failures, warnings = _classify(_baseline_metrics(jira_watcher_count=80))
    assert not any("watcher" in f.lower() for f in failures), failures
    assert any("watcher" in w.lower() and "jira" in w.lower() for w in warnings), warnings


def test_jira_watcher_count_below_tolerance_is_warning() -> None:
    # OP=50 baseline, Jira=10 → 80% low → warning (not failure).
    # Same rationale as the above-tolerance case.
    failures, warnings = _classify(_baseline_metrics(jira_watcher_count=10))
    assert not any("watcher" in f.lower() for f in failures), failures
    assert any("watcher" in w.lower() and "jira" in w.lower() for w in warnings), warnings


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


# --- Direct tests for _fetch_jira_relation_breakdown -------------------------
# Per PR #202 review: the breakdown function has non-trivial parsing
# logic (intra dedup, cross counting, dual-shape link handling). These
# tests pin the breakdown calculation directly without going through
# ``_classify``, so a regression in the parsing surfaces as a focused
# failure rather than a downstream classifier mismatch.


class _FakeJiraIssue:
    """Object-shape Jira issue for the breakdown tests."""

    def __init__(self, key: str, links: list[Any]) -> None:
        self.key = key
        self.fields = type("F", (), {"issuelinks": links})()


def _link_obj(*, outward_key: str | None = None, inward_key: str | None = None) -> Any:
    """Object-shape link with optional outward/inward target key."""
    link = type("L", (), {})()
    link.outwardIssue = type("O", (), {"key": outward_key})() if outward_key else None
    link.inwardIssue = type("I", (), {"key": inward_key})() if inward_key else None
    return link


def _link_dict(*, outward_key: str | None = None, inward_key: str | None = None) -> dict[str, Any]:
    """Dict-shape link, mirroring the cached / serialized shape that
    ``relation_migration._merge_batch_issues`` produces.
    """
    out: dict[str, Any] = {}
    if outward_key:
        out["outwardIssue"] = {"key": outward_key}
    if inward_key:
        out["inwardIssue"] = {"key": inward_key}
    return out


def _patch_jira_search(monkeypatch, pages: list[list[Any]]) -> None:
    """Patch the JiraClient lookup so the breakdown sees ``pages`` in order."""

    class _FakeUnderlying:
        def __init__(self, pages_: list[list[Any]]) -> None:
            self._pages = list(pages_)

        def search_issues(self, *_a, **_kw):
            return self._pages.pop(0) if self._pages else []

    class _FakeJira:
        def __init__(self) -> None:
            self.jira = _FakeUnderlying(pages)

    monkeypatch.setattr(
        "src.infrastructure.jira.jira_client.JiraClient",
        _FakeJira,
    )


def test_breakdown_dedups_intra_pairs(monkeypatch) -> None:
    """Each intra-project link appears twice (once per end) — must dedup to 1."""
    pages = [
        [
            _FakeJiraIssue("NRS-1", [_link_obj(outward_key="NRS-2")]),
            _FakeJiraIssue("NRS-2", [_link_obj(inward_key="NRS-1")]),
        ],
    ]
    _patch_jira_search(monkeypatch, pages)
    from tools import audit_migrated_project as audit_mod

    result = audit_mod._fetch_jira_relation_breakdown("NRS")
    assert result == {"intra_unique": 1, "cross": 0, "raw": 2}


def test_breakdown_counts_cross_separately(monkeypatch) -> None:
    """Cross-project link appears once (only the in-scope end carries it)."""
    pages = [
        [_FakeJiraIssue("NRS-1", [_link_obj(outward_key="OTHER-1")])],
    ]
    _patch_jira_search(monkeypatch, pages)
    from tools import audit_migrated_project as audit_mod

    result = audit_mod._fetch_jira_relation_breakdown("NRS")
    assert result == {"intra_unique": 0, "cross": 1, "raw": 1}


def test_breakdown_handles_dict_shaped_links(monkeypatch) -> None:
    """Dict-shape links (cached / serialized form) classify identically.

    Without dual-shape support, every dict link falls into the
    "malformed" path and gets dropped from both intra and cross —
    skewing the breakdown. Per PR #202 review.
    """
    pages = [
        [
            _FakeJiraIssue("NRS-1", [_link_dict(outward_key="NRS-2"), _link_dict(outward_key="OTHER-7")]),
        ],
    ]
    _patch_jira_search(monkeypatch, pages)
    from tools import audit_migrated_project as audit_mod

    result = audit_mod._fetch_jira_relation_breakdown("NRS")
    # 1 cross + 1 intra (NRS-1 → NRS-2 only, the reciprocal isn't fetched here)
    assert result == {"intra_unique": 1, "cross": 1, "raw": 2}


def test_breakdown_handles_dict_shaped_issues(monkeypatch) -> None:
    """Dict-shape issues + dict-shape links both work."""
    pages = [
        [
            {"key": "NRS-1", "fields": {"issuelinks": [_link_dict(outward_key="NRS-2")]}},
            {"key": "NRS-2", "fields": {"issuelinks": [_link_dict(inward_key="NRS-1")]}},
        ],
    ]
    _patch_jira_search(monkeypatch, pages)
    from tools import audit_migrated_project as audit_mod

    result = audit_mod._fetch_jira_relation_breakdown("NRS")
    assert result == {"intra_unique": 1, "cross": 0, "raw": 2}


def test_breakdown_skips_malformed_links_without_keys(monkeypatch) -> None:
    """Link with neither outwardIssue nor inwardIssue → counted as raw,
    dropped from both intra and cross to avoid double-counting.
    """
    pages = [
        [_FakeJiraIssue("NRS-1", [_link_obj()])],  # no outward, no inward
    ]
    _patch_jira_search(monkeypatch, pages)
    from tools import audit_migrated_project as audit_mod

    result = audit_mod._fetch_jira_relation_breakdown("NRS")
    assert result == {"intra_unique": 0, "cross": 0, "raw": 1}


def test_breakdown_invalid_project_key_returns_none(monkeypatch, capsys) -> None:
    """Same project-key validation contract as the legacy halved counter."""
    from tools import audit_migrated_project as audit_mod

    result = audit_mod._fetch_jira_relation_breakdown("nrs lower-case")
    assert result is None
    captured = capsys.readouterr()
    assert "invalid project key" in captured.err.lower(), captured.err


def test_breakdown_emits_odd_raw_diagnostic(monkeypatch, capsys) -> None:
    """Odd raw count → "[audit] odd" stderr warning (same hint as legacy)."""
    pages = [
        # 3 raw entries: 1 intra (NRS-1 → NRS-2), 1 cross, 1 cross.
        # Reciprocal end of the intra link is missing → odd raw.
        [
            _FakeJiraIssue(
                "NRS-1",
                [
                    _link_obj(outward_key="NRS-2"),
                    _link_obj(outward_key="OTHER-1"),
                    _link_obj(outward_key="OTHER-2"),
                ],
            ),
        ],
    ]
    _patch_jira_search(monkeypatch, pages)
    from tools import audit_migrated_project as audit_mod

    audit_mod._fetch_jira_relation_breakdown("NRS")
    captured = capsys.readouterr()
    assert "odd" in captured.err.lower() and "(3)" in captured.err, captured.err


def test_breakdown_failure_falls_back_to_legacy_count(monkeypatch) -> None:
    """When the breakdown raises mid-pagination, ``_execute_audit`` falls
    back to the legacy halved count so the audit still produces signal.

    Per PR #202 review: previously the breakdown failure path set
    ``jira_relation_count = None`` too, which collapsed the relation
    check to a "source unavailable" warning even when the legacy
    counter would have worked.
    """
    from tools import audit_migrated_project as audit_mod

    # Force the breakdown to fail.
    monkeypatch.setattr(audit_mod, "_fetch_jira_relation_breakdown", lambda _k: None)
    # Force the legacy halved counter to succeed.
    monkeypatch.setattr(audit_mod, "_fetch_jira_relation_count", lambda _k: 42)
    # Stub the other Jira-side comparisons + the OP-side script execution.
    monkeypatch.setattr(audit_mod, "_fetch_jira_issue_count", lambda _k: 100)
    monkeypatch.setattr(audit_mod, "_fetch_jira_attachment_count", lambda _k: 10)
    monkeypatch.setattr(audit_mod, "_fetch_jira_watcher_count", lambda _k: 50)

    class _StubOp:
        def execute_json_query(self, *_a, **_kw) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(audit_mod, "OpenProjectClient", _StubOp)

    metrics = audit_mod._execute_audit("NRS")
    assert metrics["jira_relation_breakdown"] is None
    # Crucial: legacy counter still populated, classifier uses it.
    assert metrics["jira_relation_count"] == 42


def test_generated_audit_script_parses_as_ruby() -> None:
    """The generated Ruby script must pass ``ruby -c`` (parse-only).

    Two distinct regex-slash bugs were caught only by live audit runs
    in this project — PR #178 (URL pattern) and PR #182/#188 (User
    Origin System pattern). Both shipped because the unit-test loop
    runs the patterns through Python's ``re``, which has no ``/.../``
    literal syntax and therefore never sees the parse-time failure.

    This test closes the loop: generate the full Ruby script
    (including all interpolated regex literals, hash literals, and
    block syntax) and pipe it to ``ruby -c`` for a parse-only check.
    Catches the entire class of "valid in Python, broken in Ruby"
    bugs before they reach production audits — at the cost of a
    Ruby interpreter on the test runner.

    Skipped if ``ruby`` isn't on PATH (CI environments without Ruby
    fall through silently rather than failing). The other regex
    guards in this file (``test_audit_regexes_have_no_unescaped_forward_slash_in_ruby_literal``)
    catch the slash subset without needing Ruby.
    """
    import shutil
    import subprocess

    ruby = shutil.which("ruby")
    if ruby is None:
        import pytest as _pytest

        _pytest.skip("ruby not on PATH — script-syntax check needs a Ruby interpreter")

    from tools.audit_migrated_project import _build_audit_script

    script = _build_audit_script("NRS")
    proc = subprocess.run(
        [ruby, "-c", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, (
        "Generated audit script failed Ruby parse-check.\n"
        f"stdout: {proc.stdout!r}\n"
        f"stderr: {proc.stderr!r}\n"
        "First 500 chars of script:\n"
        f"{script[:500]}"
    )
    assert "Syntax OK" in proc.stdout, proc.stdout


def test_wp_origin_key_under_populated_is_failure() -> None:
    """``J2O Origin Key`` is a hard-requirement (per spec line 27).

    Caught by the live TEST audit: TEST showed every WP CF with
    ``populated: 0`` because the migration ran before #175 wired
    them up. The audit's existing rule only checks ``exists``, not
    ``populated`` — so the gap was invisible. Without ``J2O Origin
    Key`` populated, dedup on re-run is broken AND the audit can't
    identify which Jira issue each WP came from.
    """
    metrics = _baseline_metrics()
    # Override Origin Key to populated=50 (with wp_total=100 baseline)
    metrics["wp_provenance_cfs"]["J2O Origin Key"] = {
        "exists": True,
        "populated": 50,
    }
    failures, _warnings = _classify(metrics)
    assert any("Origin Key" in f and ("populated" in f.lower() or "/100" in f) for f in failures), failures


def test_wp_other_cfs_under_populated_is_warning() -> None:
    """Other WP provenance CFs are 'should' per spec — warning, not failure."""
    metrics = _baseline_metrics()
    # ``J2O Origin URL`` is "should" per spec, not hard-required.
    metrics["wp_provenance_cfs"]["J2O Origin URL"] = {
        "exists": True,
        "populated": 70,
    }
    failures, warnings = _classify(metrics)
    # Not in failures
    assert not any("Origin URL" in f for f in failures), failures
    # But surfaced as a warning so operators see it
    assert any("Origin URL" in w for w in warnings), warnings


def test_wp_origin_key_fully_populated_passes() -> None:
    """Healthy state: every WP has Origin Key populated."""
    metrics = _baseline_metrics()
    # Baseline already has populated=100 for all CFs; explicit for clarity.
    metrics["wp_provenance_cfs"]["J2O Origin Key"] = {
        "exists": True,
        "populated": 100,
    }
    failures, _warnings = _classify(metrics)
    assert not any("Origin Key" in f for f in failures), failures


def test_wp_cf_population_silent_when_cf_missing_entirely() -> None:
    """If a CF doesn't exist at all, the existing missing-CF rule fires.

    The new under-populated rule must not double-fire — only the
    missing-CF rule should claim that CF, with a clear "missing"
    message rather than confusing population numbers.
    """
    metrics = _baseline_metrics()
    metrics["wp_provenance_cfs"]["J2O Origin Key"] = {
        "exists": False,
        "populated": 0,
    }
    failures, _warnings = _classify(metrics)
    # The missing-CF rule fires (Bug D indicator)
    assert any("CFs missing" in f and "Origin Key" in f for f in failures), failures
    # The new under-populated rule does NOT also fire — would be a
    # confusing double-message.
    assert not any("Origin Key" in f and "populated" in f.lower() and "CFs missing" not in f for f in failures), (
        failures
    )


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


# --- Source-aware assignee heuristic (added 2026-05-08) ---


def test_low_assignee_coverage_does_not_fail_when_jira_matches() -> None:
    """Low coverage that matches the Jira source = NOT a failure.

    Live 2026-05-08 NRS audit: Jira itself reports 12/4082 issues
    with an assignee. The migration faithfully copied all 12. The
    old hard 5% threshold flagged this as ``Bug A`` — false positive.
    """
    failures, warnings = _classify(
        _baseline_metrics(
            wp_total=4082,
            wp_with_assignee=12,
            wp_with_subject=4082,
            wp_with_description=4082,
            wp_with_author=4082,
            wp_with_type=4082,
            wp_with_status=4082,
            wp_with_priority=4082,
            wp_journal_total=4082,
            wp_provenance_cfs={
                k: {"exists": True, "populated": 4082} for k in _baseline_metrics()["wp_provenance_cfs"]
            },
            jira_assignee_count=12,
        ),
    )
    assert not any("assignee" in f.lower() for f in failures), failures
    assert not any("assignee" in w.lower() for w in warnings), warnings


def test_assignee_gap_beyond_tolerance_fails() -> None:
    """If OP undercount diverges from Jira by more than 5% +5
    issues, that's real loss.

    Pin: source-aware comparison — the threshold scales with the
    Jira-side count instead of being a fixed percentage.
    """
    failures, _warnings = _classify(
        _baseline_metrics(
            wp_total=1000,
            wp_with_assignee=400,  # OP has 400
            wp_with_subject=1000,
            wp_with_description=1000,
            wp_with_author=1000,
            wp_with_type=1000,
            wp_with_status=1000,
            wp_with_priority=1000,
            wp_journal_total=1000,
            wp_provenance_cfs={
                k: {"exists": True, "populated": 1000} for k in _baseline_metrics()["wp_provenance_cfs"]
            },
            jira_assignee_count=600,  # Jira has 600 — 200 missing in OP
        ),
    )
    assert any("assignee" in f.lower() and "missing" in f.lower() for f in failures), failures


def test_assignee_no_jira_count_low_coverage_warns_only() -> None:
    """Without a Jira-side count, very low coverage on a non-tiny
    project surfaces as a warning, never a failure — operators may
    legitimately run the audit without Jira creds.
    """
    failures, warnings = _classify(
        _baseline_metrics(
            wp_total=500,
            wp_with_assignee=2,
            wp_with_subject=500,
            wp_with_description=500,
            wp_with_author=500,
            wp_with_type=500,
            wp_with_status=500,
            wp_with_priority=500,
            wp_journal_total=500,
            wp_provenance_cfs={k: {"exists": True, "populated": 500} for k in _baseline_metrics()["wp_provenance_cfs"]},
            # jira_assignee_count omitted on purpose — simulates absent Jira creds.
        ),
    )
    assert not any("assignee" in f.lower() for f in failures), failures
    assert any("assignee" in w.lower() for w in warnings), warnings


# --- Watcher author auto-subscription correction (added 2026-05-08) ---


def test_watcher_count_subtracts_author_auto_subscriptions() -> None:
    """OP auto-subscribes a WP's author as a watcher. The audit must
    subtract these from ``wp_watcher_total`` before comparing to
    Jira's ``watchCount`` — otherwise the comparison conflates two
    different effects.

    Live 2026-05-08 NRS audit caught a +338 raw watcher delta that
    was actually 552 OP author auto-subscriptions masking a -214
    real undercount. With the correction:
        op_total=3233, author_auto=552, op_non_author=2681,
        jira=2895 → delta=-214 → still flags (genuine loss).
    Without it, the +338 surface delta misled the operator.
    """
    failures, warnings = _classify(
        _baseline_metrics(
            wp_total=4082,
            wp_with_subject=4082,
            wp_with_description=4082,
            wp_with_assignee=12,
            wp_with_author=4082,
            wp_with_type=4082,
            wp_with_status=4082,
            wp_with_priority=4082,
            wp_journal_total=4082,
            wp_provenance_cfs={
                k: {"exists": True, "populated": 4082} for k in _baseline_metrics()["wp_provenance_cfs"]
            },
            wp_watcher_total=3233,
            wp_watcher_author_auto=552,
            jira_assignee_count=12,
            jira_watcher_count=2895,
        ),
    )
    # Watcher mismatch surfaces as a warning, not a failure (count
    # comparison has known noise sources — see ``_WATCHER_TOLERANCE``).
    watcher_warnings = [w for w in warnings if "watcher" in w.lower()]
    assert watcher_warnings, warnings
    assert not any("watcher" in f.lower() for f in failures), failures
    # The reported numbers must reflect the corrected view (2681 non-author),
    # not the raw 3233 — so an operator can read the message and act on it.
    assert "2681" in watcher_warnings[0], watcher_warnings
    assert "552" in watcher_warnings[0], watcher_warnings


def test_watcher_count_clean_after_author_subtraction() -> None:
    """When OP's non-author watcher count matches Jira within
    tolerance, no failure should fire even if the raw total looks
    higher because of auto-subscriptions.
    """
    failures, _warnings = _classify(
        _baseline_metrics(
            wp_total=1000,
            wp_with_subject=1000,
            wp_with_description=1000,
            wp_with_assignee=1000,
            wp_with_author=1000,
            wp_with_type=1000,
            wp_with_status=1000,
            wp_with_priority=1000,
            wp_journal_total=1000,
            wp_provenance_cfs={
                k: {"exists": True, "populated": 1000} for k in _baseline_metrics()["wp_provenance_cfs"]
            },
            wp_watcher_total=600,
            wp_watcher_author_auto=200,  # 600 - 200 = 400 non-author
            jira_assignee_count=1000,
            jira_watcher_count=400,  # matches the non-author count
        ),
    )
    assert not any("watcher" in f.lower() for f in failures), failures
