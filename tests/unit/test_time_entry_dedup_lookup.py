"""Bug C2: extracting worklog keys from OP-side TimeEntry payloads.

The first attempt at the dedup filter looked up CF values by *name*
(``item.get("name") == "J2O Origin Worklog Key"``). But OP's
``custom_field_values`` JSON shape doesn't include ``name`` — it's
``[{"custom_field_id": 205, "value": "TEST-1:101", ...}]``. So the
match always failed, ``existing_keys`` was always empty, and re-runs
still duplicated every TimeEntry (TEST migration: 5 → 10 on second run).

Fix: resolve the worklog CF id once, then match entries by
``custom_field_id`` (with backward-compat for older payload shapes).
"""

from __future__ import annotations

from src.utils.time_entry_migrator import _extract_worklog_keys_from_op_entries


def test_matches_by_custom_field_id_in_list_shape() -> None:
    """Real OP ``custom_field_values`` JSON shape — list of dicts with
    ``custom_field_id`` (no ``name`` key).
    """
    entries = [
        {
            "id": 100,
            "custom_fields": [
                {"id": 4001, "custom_field_id": 205, "value": "TEST-1:1001"},
                {"id": 4002, "custom_field_id": 999, "value": "ignored"},
            ],
        },
        {
            "id": 101,
            "custom_fields": [
                {"id": 4003, "custom_field_id": 205, "value": "TEST-2:2002"},
            ],
        },
    ]
    keys = _extract_worklog_keys_from_op_entries(entries, worklog_cf_id=205)
    assert keys == {"TEST-1:1001", "TEST-2:2002"}


def test_falls_back_to_meta_jira_worklog_key() -> None:
    """If the response includes a flat ``jira_worklog_key`` (some adapter
    shapes do), use it directly.
    """
    entries = [
        {"id": 100, "jira_worklog_key": "TEST-1:1001"},
        {"id": 101, "jira_worklog_key": "TEST-2:2002"},
    ]
    keys = _extract_worklog_keys_from_op_entries(entries, worklog_cf_id=205)
    assert keys == {"TEST-1:1001", "TEST-2:2002"}


def test_handles_mixed_shapes() -> None:
    """Some entries have list-shape CFs, others have meta passthrough,
    others have nothing.
    """
    entries = [
        {"custom_fields": [{"custom_field_id": 205, "value": "A:1"}]},
        {"jira_worklog_key": "B:2"},
        {"custom_fields": []},
        {},
    ]
    keys = _extract_worklog_keys_from_op_entries(entries, worklog_cf_id=205)
    assert keys == {"A:1", "B:2"}


def test_skips_other_cf_ids() -> None:
    """Non-worklog CFs in the list must NOT be picked up as keys."""
    entries = [
        {
            "custom_fields": [
                {"custom_field_id": 100, "value": "not-a-worklog-key"},
                {"custom_field_id": 205, "value": "real-key"},
            ]
        },
    ]
    keys = _extract_worklog_keys_from_op_entries(entries, worklog_cf_id=205)
    assert keys == {"real-key"}


def test_returns_empty_for_empty_input() -> None:
    assert _extract_worklog_keys_from_op_entries([], worklog_cf_id=205) == set()


def test_skips_blank_values() -> None:
    entries = [
        {"custom_fields": [{"custom_field_id": 205, "value": ""}]},
        {"custom_fields": [{"custom_field_id": 205, "value": None}]},
        {"jira_worklog_key": ""},
    ]
    assert _extract_worklog_keys_from_op_entries(entries, worklog_cf_id=205) == set()


def test_camelcase_and_legacy_keys() -> None:
    """Backward compat with ``customFields`` (camelCase) and ``cfs`` (legacy)."""
    entries = [
        {"customFields": [{"custom_field_id": 205, "value": "K1"}]},
        {"cfs": [{"custom_field_id": 205, "value": "K2"}]},
    ]
    assert _extract_worklog_keys_from_op_entries(entries, worklog_cf_id=205) == {"K1", "K2"}
