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
