"""Tests for :class:`src.models.mapping.WorkPackageMappingEntry`.

ADR-002 phase 3c: the typed entry must accept both legacy shapes
(``int`` and ``dict``) and reject every other shape at the boundary.
The round-trip test pins that ``model_dump(by_alias=True)`` produces a
payload that re-validates without loss — a precondition for the
forward-only on-disk migration in ``scripts/normalize_wp_mapping.py``.
"""

from __future__ import annotations

import pytest

from src.models.mapping import WorkPackageMappingEntry


def test_from_legacy_int_value_yields_typed_entry() -> None:
    entry = WorkPackageMappingEntry.from_legacy("PROJ-1", 42)

    assert entry.jira_key == "PROJ-1"
    assert entry.openproject_id == 42
    # Optional fields default to ``None`` for the int legacy shape because
    # the ``int`` carries no metadata.
    assert entry.openproject_project_id is None
    assert entry.jira_migration_date is None
    assert entry.updated_at is None


def test_from_legacy_full_dict_yields_typed_entry() -> None:
    entry = WorkPackageMappingEntry.from_legacy(
        "PROJ-2",
        {
            "openproject_id": 99,
            "openproject_project_id": 7,
            "jira_migration_date": "2026-04-30T12:00:00Z",
            "updated_at": "2026-05-01T08:30:00Z",
        },
    )

    assert entry.jira_key == "PROJ-2"
    assert entry.openproject_id == 99
    assert entry.openproject_project_id == 7
    assert entry.jira_migration_date == "2026-04-30T12:00:00Z"
    assert entry.updated_at == "2026-05-01T08:30:00Z"


def test_from_legacy_partial_dict_with_required_fields_only() -> None:
    entry = WorkPackageMappingEntry.from_legacy(
        "PROJ-3",
        {"openproject_id": 5},
    )

    assert entry.jira_key == "PROJ-3"
    assert entry.openproject_id == 5
    assert entry.openproject_project_id is None
    assert entry.jira_migration_date is None
    assert entry.updated_at is None


def test_from_legacy_dict_keeps_explicit_jira_key_when_matching() -> None:
    """If the dict already carries ``jira_key`` and it matches the outer key,
    the explicit value is fine — the outer-key injection is harmless because
    Pydantic uses the latest value for duplicate kwargs.
    """
    entry = WorkPackageMappingEntry.from_legacy(
        "PROJ-4",
        {"jira_key": "PROJ-4", "openproject_id": 17},
    )

    assert entry.jira_key == "PROJ-4"
    assert entry.openproject_id == 17


@pytest.mark.parametrize(
    "value",
    ["a-string", ["a", "list"], None, 1.5, True, False],
    ids=["str", "list", "none", "float", "true", "false"],
)
def test_from_legacy_unsupported_shape_raises_value_error(value: object) -> None:
    with pytest.raises(ValueError, match="Unsupported wp_map entry shape"):
        WorkPackageMappingEntry.from_legacy("PROJ-X", value)


def test_from_dict_full_payload_with_extra_keys_are_ignored() -> None:
    entry = WorkPackageMappingEntry.from_dict(
        {
            "jira_key": "PROJ-5",
            "openproject_id": 10,
            "openproject_project_id": 11,
            "jira_migration_date": "2026-04-29T00:00:00Z",
            "updated_at": "2026-04-29T01:00:00Z",
            # Extra fields the migration may have written historically:
            "lockVersion": 0,
            "_links": {"self": {"href": "/api/v3/work_packages/10"}},
        },
    )

    assert entry.jira_key == "PROJ-5"
    assert entry.openproject_id == 10
    assert entry.openproject_project_id == 11

    dump = entry.model_dump()
    assert "lockVersion" not in dump
    assert "_links" not in dump


def test_round_trip_via_model_dump_by_alias() -> None:
    original = WorkPackageMappingEntry.from_legacy(
        "PROJ-6",
        {
            "openproject_id": 21,
            "openproject_project_id": 3,
            "jira_migration_date": "2026-04-15T09:00:00Z",
            "updated_at": "2026-04-15T09:01:00Z",
        },
    )

    payload = original.model_dump(by_alias=True)
    revived = WorkPackageMappingEntry.model_validate(payload)

    assert revived == original
    # And dumping again should be byte-identical, proving idempotence:
    assert revived.model_dump(by_alias=True) == payload


def test_from_legacy_dict_missing_required_field_raises() -> None:
    """Missing ``openproject_id`` in a dict shape should fail validation."""
    with pytest.raises(ValueError, match="openproject_id"):
        WorkPackageMappingEntry.from_legacy(
            "PROJ-7",
            {"openproject_project_id": 4},
        )
