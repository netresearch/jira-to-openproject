"""Time entry idempotency on re-run.

Bug C: running the migration twice produced 10,606 TimeEntries (= 2x the
5,303 successful per-run). Three sub-bugs:

1. The probe-existing loop in ``time_entry_migrator.py:520,523`` looks for
   custom-field name ``"Jira Worklog Key"``, but the bulk-create script
   writes the canonical ``"J2O Origin Worklog Key"``. Lookup never hits
   → ``existing_keys`` is always empty.
2. The batch path (``use_batch = True``, the default) doesn't apply
   ``existing_keys`` at all — only the per-entry fallback does.
3. The provenance CF wasn't even created at migration startup
   (Bug D — fixed).

This test covers the dedup helper. The CF-name fix is a one-line change
verified by static inspection.
"""

from __future__ import annotations

from src.utils.time_entry_migrator import _filter_already_migrated_entries


def test_filters_entries_whose_worklog_key_is_already_in_op() -> None:
    entries = [
        {"hours": 1.0, "_meta": {"jira_worklog_key": "TEST-1:101"}},
        {"hours": 0.5, "_meta": {"jira_worklog_key": "TEST-1:102"}},
        {"hours": 2.0, "_meta": {"jira_worklog_key": "TEST-2:201"}},
    ]
    existing_keys = {"TEST-1:101", "TEST-2:201"}

    kept, skipped = _filter_already_migrated_entries(entries, existing_keys)

    assert len(kept) == 1
    assert kept[0]["_meta"]["jira_worklog_key"] == "TEST-1:102"
    assert skipped == 2


def test_keeps_all_when_existing_set_is_empty() -> None:
    entries = [
        {"_meta": {"jira_worklog_key": "TEST-1:1"}},
        {"_meta": {"jira_worklog_key": "TEST-1:2"}},
    ]
    kept, skipped = _filter_already_migrated_entries(entries, set())
    assert len(kept) == 2
    assert skipped == 0


def test_keeps_entries_without_worklog_key() -> None:
    """Entries lacking a meta worklog key go through (we can't dedup them
    anyway). The migration warns about these elsewhere.
    """
    entries = [
        {"_meta": {}},
        {"_meta": {"jira_worklog_key": ""}},
        {"_meta": {"jira_worklog_key": None}},
        {"_meta": {"jira_worklog_key": "TEST-1:1"}},
    ]
    kept, skipped = _filter_already_migrated_entries(entries, set())
    assert len(kept) == 4
    assert skipped == 0


def test_handles_missing_meta() -> None:
    entries = [
        {"hours": 1.0},  # no _meta
        {"_meta": {"jira_worklog_key": "TEST-1:1"}},
    ]
    kept, skipped = _filter_already_migrated_entries(entries, {"TEST-1:1"})
    assert len(kept) == 1
    assert kept[0] == {"hours": 1.0}
    assert skipped == 1
