"""Tests for :func:`src.utils.wp_mapping_normalizer.normalize_wp_mapping`.

The helper is a pure function with two contracts:

* every input shape that :class:`WorkPackageMappingEntry.from_legacy`
  accepts must round-trip into a uniform dict-shape;
* every other shape must be *dropped* (not raised) and counted, so the
  one-shot CLI script can survive corrupt user data.
"""

from __future__ import annotations

from src.utils.wp_mapping_normalizer import normalize_wp_mapping


def test_all_dict_input_round_trips_with_zero_dropped() -> None:
    raw = {
        "PROJ-1": {
            "openproject_id": 10,
            "openproject_project_id": 1,
            "jira_migration_date": "2026-04-30T12:00:00Z",
            "updated_at": "2026-04-30T12:01:00Z",
        },
        "PROJ-2": {
            "openproject_id": 11,
            "openproject_project_id": 1,
        },
    }

    normalized, dropped = normalize_wp_mapping(raw)

    assert dropped == 0
    assert set(normalized) == {"PROJ-1", "PROJ-2"}
    assert normalized["PROJ-1"]["openproject_id"] == 10
    assert normalized["PROJ-1"]["jira_key"] == "PROJ-1"
    assert normalized["PROJ-2"]["jira_migration_date"] is None
    assert normalized["PROJ-2"]["updated_at"] is None


def test_all_int_input_is_promoted_to_dict_shape() -> None:
    raw = {"PROJ-A": 1, "PROJ-B": 2, "PROJ-C": 3}

    normalized, dropped = normalize_wp_mapping(raw)

    assert dropped == 0
    assert set(normalized) == {"PROJ-A", "PROJ-B", "PROJ-C"}
    for key, expected_id in (("PROJ-A", 1), ("PROJ-B", 2), ("PROJ-C", 3)):
        entry = normalized[key]
        assert entry["jira_key"] == key
        assert entry["openproject_id"] == expected_id
        # Optional fields are present-as-None to keep the shape uniform.
        assert entry["openproject_project_id"] is None
        assert entry["jira_migration_date"] is None
        assert entry["updated_at"] is None


def test_mixed_input_unifies_to_dict_shape() -> None:
    raw = {
        "PROJ-D1": {"openproject_id": 100, "openproject_project_id": 9},
        "PROJ-I1": 200,
        "PROJ-D2": {"openproject_id": 300},
    }

    normalized, dropped = normalize_wp_mapping(raw)

    assert dropped == 0
    assert normalized["PROJ-D1"]["openproject_id"] == 100
    assert normalized["PROJ-D1"]["openproject_project_id"] == 9
    assert normalized["PROJ-I1"]["openproject_id"] == 200
    assert normalized["PROJ-I1"]["openproject_project_id"] is None
    assert normalized["PROJ-D2"]["openproject_id"] == 300
    # Each row carries exactly the same set of keys after normalisation.
    expected_keys = {
        "jira_key",
        "openproject_id",
        "openproject_project_id",
        "jira_migration_date",
        "updated_at",
    }
    for value in normalized.values():
        assert set(value) == expected_keys


def test_corrupt_entries_are_dropped_and_counted() -> None:
    raw = {
        "PROJ-OK": {"openproject_id": 1},
        "PROJ-STR": "not-an-id",
        "PROJ-LIST": [1, 2, 3],
        "PROJ-NONE": None,
        "PROJ-MISSING-ID": {"openproject_project_id": 4},  # no openproject_id
    }

    normalized, dropped = normalize_wp_mapping(raw)

    assert dropped == 4
    assert set(normalized) == {"PROJ-OK"}
    # Confirm the dropped keys are absent from the output entirely.
    for key in ("PROJ-STR", "PROJ-LIST", "PROJ-NONE", "PROJ-MISSING-ID"):
        assert key not in normalized


def test_empty_input_yields_empty_output() -> None:
    normalized, dropped = normalize_wp_mapping({})

    assert normalized == {}
    assert dropped == 0
