# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import pytest

from src.application.components.affects_versions_migration import (
    AFFECTS_VERSIONS_CF_NAME,
    AffectsVersionsMigration,
)


class DummyJira:
    base_url = "https://jira.example.invalid"

    def __init__(self):
        self.projects = self
        self.issues = {
            k: {"id": str(i), "key": k, "fields": {"project": {"id": "40", "key": "PRJ"}, "versions": refs}}
            for i, (k, refs) in enumerate(
                {"PRJ-1": [{"id": "10", "name": "1.0"}], "PRJ-2": [{"id": "20", "name": "2.0"}], "PRJ-3": []}.items(), 1
            )
        }

    def unrestricted_version_issues(self, project, keys):
        return {k: self.issues[k] for k in keys if k in self.issues}


class DummyOp:
    def __init__(self):
        self.batches = []

    def ensure_wp_custom_field_id(self, name, field_format):
        assert name == AFFECTS_VERSIONS_CF_NAME
        return 555

    def execute_json_query(self, script):
        import base64
        import json

        payload = json.loads(base64.b64decode(script.split("strict_decode64('", 1)[1].split("')", 1)[0]))
        self.batches.append(payload)
        return {"updated": sum(bool(row["versions"]) for row in payload["issues"]), "native": True}


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PRJ": {"openproject_id": 32}},
                "work_package": {
                    "PRJ-1": {"openproject_id": 2001},
                    "PRJ-2": {"openproject_id": 2002},
                    "PRJ-3": {"openproject_id": 2003},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_affects_versions_migration_end_to_end():
    mig = AffectsVersionsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert mp.success is True
    assert ld.success is True
    # Two issues have versions; third empty
    assert ld.updated == 2


def test_numeric_mappings_and_omitted_source_fields_are_not_treated_as_explicit_deletions(monkeypatch):
    from src import config

    jira, op = DummyJira(), DummyOp()
    del jira.issues["PRJ-1"]["fields"]["versions"]
    del jira.issues["PRJ-2"]
    config.mappings._m["work_package"] = {
        str(i): {"jira_key": f"PRJ-{i}", "openproject_id": 50 + i} for i in range(1, 4)
    }
    result = AffectsVersionsMigration(jira, op).run()
    assert result.success
    assert result.details["unconfirmed_issue_mappings"] == 1
    assert op.batches[0]["issues"] == [{"wp": 53, "id": "3", "key": "PRJ-3", "versions": []}]
    assert op.batches[0]["key"] == "PRJ"
    assert op.batches[0]["jira_project_id"] == "40"


def test_native_readback_failure_surfaces_as_incomplete(monkeypatch):
    jira, op = DummyJira(), DummyOp()
    monkeypatch.setattr(op, "execute_json_query", lambda _script: None)
    result = AffectsVersionsMigration(jira, op).run()
    assert not result.success
    assert result.failed == 3


def test_cli_project_filter_limits_the_projects(monkeypatch):
    from src import config
    from tests.helpers.cli_project_filter import apply_cli_project_filter

    jira, op = DummyJira(), DummyOp()
    jira.issues["ABC-1"] = {
        "id": "9",
        "key": "ABC-1",
        "fields": {"project": {"id": "41", "key": "ABC"}, "versions": []},
    }
    config.mappings._m["project"]["ABC"] = {"openproject_id": 33}
    config.mappings._m["work_package"]["ABC-1"] = {"openproject_id": 2009}
    apply_cli_project_filter(monkeypatch, "PRJ")
    assert AffectsVersionsMigration(jira, op).run().success
    assert [batch["key"] for batch in op.batches] == ["PRJ"]
