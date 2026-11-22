# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Remote links come from each issue's remotelink resource, never from a search (#501)."""

from types import SimpleNamespace

import pytest

from src.application.components.remote_links_migration import SECTION_TITLE, RemoteLinksMigration


def _link(url, title=None):
    """One entry in the shape of GET /rest/api/2/issue/{key}/remotelink on Jira Server 9.12."""
    target = {"url": url, "icon": {}, "status": {"resolved": False, "icon": {}}}
    if title is not None:
        target["title"] = title
    return {
        "id": 10001,
        "self": "https://jira.local/rest/api/2/issue/PRJ-1/remotelink/10001",
        "application": {},
        "object": target,
    }


class DummyJira:
    def __init__(self, links=None, failing=(), unreturned=()) -> None:
        self.links = (
            {
                "PRJ-1": [_link("https://a.example/x", "A")],
                "PRJ-2": [_link("https://b.example/y")],
                "PRJ-3": [],
            }
            if links is None
            else links
        )
        self.failing = set(failing)
        self.unreturned = set(unreturned)
        self.reads: list[str] = []

    def batch_get_issues(self, keys):
        # A search answer carries no remote links.
        return {k: SimpleNamespace(key=k, fields=SimpleNamespace()) for k in keys if k not in self.unreturned}

    def get_issue_remote_links(self, key):
        self.reads.append(key)
        if key in self.failing:
            raise OSError("remotelink unavailable")
        return self.links.get(key, [])


class DummyOp:
    def __init__(self) -> None:
        self.bulk_sections: list[dict] = []

    def bulk_upsert_wp_description_sections(self, sections: list[dict]):
        self.bulk_sections.extend(sections)
        return {"updated": len(sections), "failed": 0}


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def get_mapping(self, name: str):
            work_packages = {
                "PRJ-1": {"openproject_id": 13001},
                "PRJ-2": {"openproject_id": 13002},
                "PRJ-3": {"openproject_id": 13003},
            }
            return {"work_package": work_packages}.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def _run(jira):
    op = DummyOp()
    mig = RemoteLinksMigration(jira_client=jira, op_client=op)  # type: ignore[arg-type]
    return mig._load(mig._map(mig._extract())), op


def test_the_links_of_the_remotelink_resource_become_a_section():
    result, op = _run(jira := DummyJira())
    assert (result.success, result.updated, result.failed) == (True, 2, 0)
    assert sorted(jira.reads) == ["PRJ-1", "PRJ-2", "PRJ-3"]
    assert {(s["work_package_id"], s["section_marker"], s["content"]) for s in op.bulk_sections} == {
        (13001, SECTION_TITLE, "- [A](https://a.example/x)"),
        (13002, SECTION_TITLE, "- [https://b.example/y](https://b.example/y)"),
    }


def test_only_http_and_https_links_are_kept():
    links = {"PRJ-1": [_link("ftp://a.example/x", "F"), _link("  https://a.example/y ", " T "), {"id": 1}]}
    _result, op = _run(DummyJira(links))
    assert [s["content"] for s in op.bulk_sections] == ["- [T](https://a.example/y)"]


def test_an_unreadable_remotelink_answer_counts_as_failed():
    result, op = _run(DummyJira(failing=["PRJ-2"]))
    assert (result.success, result.updated, result.failed) == (False, 1, 1)
    assert [s["work_package_id"] for s in op.bulk_sections] == [13001]


def test_an_issue_the_batch_read_does_not_return_is_not_read():
    # For example an issue that got a security level after it was migrated: its links stay unread.
    result, op = _run(jira := DummyJira(unreturned=["PRJ-1"]))
    assert "PRJ-1" not in jira.reads
    assert [s["work_package_id"] for s in op.bulk_sections] == [13002]
    assert result.failed == 0
