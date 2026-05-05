"""Bug D2: WP migration must populate ALL provenance CFs, not just Origin Key.

After Bug D's startup bootstrap, all 8 ``WorkPackageCustomField`` provenance
CFs are created. But the live audit on TEST showed only ``J2O Origin Key``
gets a *value* on each WP — the other 7 CFs exist but are populated for
zero WPs.

Root cause: ``_build_skeleton_payload`` adds a single CF entry. We need a
helper that returns all relevant {id, value} entries for a given Jira issue.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.application.components.work_package_skeleton_migration import (
    _build_provenance_custom_field_entries,
)


def _make_issue() -> Any:
    return SimpleNamespace(
        id="10042",
        key="TEST-1",
        fields=SimpleNamespace(
            project=SimpleNamespace(id="10001", key="TEST"),
            summary="x",
        ),
    )


def test_returns_entry_per_known_cf_when_all_have_ids() -> None:
    cf_ids = {
        "J2O Origin Key": 100,
        "J2O Origin ID": 101,
        "J2O Origin System": 102,
        "J2O Origin URL": 103,
        "J2O Project Key": 104,
        "J2O Project ID": 105,
    }
    entries = _build_provenance_custom_field_entries(
        _make_issue(),
        cf_ids,
        jira_base_url="https://jira.example.com",
    )
    by_id = {e["id"]: e["value"] for e in entries}
    assert by_id[100] == "TEST-1"
    assert by_id[101] == "10042"
    assert by_id[102] == "Jira"
    assert by_id[103] == "https://jira.example.com/browse/TEST-1"
    assert by_id[104] == "TEST"
    assert by_id[105] == "10001"


def test_skips_cfs_with_no_id_in_map() -> None:
    """If a CF wasn't created (id missing), don't emit an entry for it."""
    cf_ids = {"J2O Origin Key": 100}
    entries = _build_provenance_custom_field_entries(
        _make_issue(), cf_ids, jira_base_url="https://jira.x"
    )
    assert len(entries) == 1
    assert entries[0] == {"id": 100, "value": "TEST-1"}


def test_handles_missing_jira_project_attribute() -> None:
    """Some Jira issues might lack project info — don't crash."""
    issue = SimpleNamespace(id="1", key="X-1", fields=SimpleNamespace(summary="x"))
    cf_ids = {
        "J2O Origin Key": 100,
        "J2O Project Key": 104,
        "J2O Project ID": 105,
    }
    entries = _build_provenance_custom_field_entries(
        issue, cf_ids, jira_base_url="https://j.x"
    )
    by_id = {e["id"]: e["value"] for e in entries}
    assert by_id[100] == "X-1"
    # Project Key/ID should be absent since the source field is missing
    assert 104 not in by_id
    assert 105 not in by_id


def test_handles_empty_base_url() -> None:
    """If the base URL is unset, don't synthesize a broken URL."""
    cf_ids = {"J2O Origin Key": 100, "J2O Origin URL": 103}
    entries = _build_provenance_custom_field_entries(
        _make_issue(), cf_ids, jira_base_url=""
    )
    by_id = {e["id"]: e["value"] for e in entries}
    assert by_id[100] == "TEST-1"
    assert 103 not in by_id  # no URL written when base is unknown
