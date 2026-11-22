# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import base64
import json

import pytest

from src.application.components.versions_migration import VersionsMigration
from tests.helpers.rails_writes import answer


class DummyFV:
    def __init__(self, name: str) -> None:
        self.name = name


class DummyFields:
    def __init__(self, fixVersions: list[DummyFV]):
        self.fixVersions = fixVersions


class DummyIssue:
    def __init__(self, key: str, versions: list[str]) -> None:
        self.key = key
        self.fields = DummyFields([DummyFV(n) for n in versions])


class DummyJira:
    def __init__(self) -> None:
        self.base_url = "https://jira.example.invalid"
        self.projects = self
        self.issues: dict[str, DummyIssue] = {
            "PRJ-1": DummyIssue("PRJ-1", ["v1", "v2"]),
            "PRJ-2": DummyIssue("PRJ-2", ["v1"]),
            "ABC-3": DummyIssue("ABC-3", ["alpha"]),
        }

    def get_versions(self, project):
        names = ["v1", "v2", "unused"] if project == "PRJ" else ["alpha"]
        return [
            {"id": str(i), "projectId": 78, "name": name, "released": False, "archived": False}
            for i, name in enumerate(names, 1)
        ]

    def unrestricted_version_issues(self, project, keys):
        return {
            k: {
                "id": k.split("-")[-1],
                "key": k,
                "fields": {"fixVersions": [{"name": v.name} for v in self.issues[k].fields.fixVersions]},
            }
            for k in keys
            if k in self.issues and k.startswith(project + "-")
        }


class DummyOp:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.created_payloads: list[dict] = []

    def execute_json_query(self, q: str):
        self.queries.append(q)
        encoded = q.split("strict_decode64('", 1)[1].split("')", 1)[0]
        data = json.loads(base64.b64decode(encoded))
        self.created_payloads.append(data)
        rows = {r["attributes"]["name"]: int(r["id"]) for r in data["versions"]}
        return {"versions": rows, "changed": len(rows)}

    def batch_update_work_packages(self, updates):
        # Pretend all succeeded
        return answer(updates)


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "PRJ-1": {"openproject_id": 2001},
                    "PRJ-2": {"openproject_id": 2002},
                    "ABC-3": {"openproject_id": 3003},
                },
                "project": {
                    "PRJ": {"openproject_id": 11},
                    "ABC": {"openproject_id": 22},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, mapping):
            self._m[name] = mapping

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_versions_migration_end_to_end():
    mig = VersionsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert mp.success is True
    assert ld.success is True
    assert ld.updated >= 1


def test_versions_migration_load_with_numeric_outer_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """_load must update WPs even when wp_map has numeric outer keys.

    Production mappings look like:
        {"144952": {"jira_key": "PRJ-1", "openproject_id": 2001}, ...}

    Before the fix, _load iterated wp_map.items() and called issues.get(key)
    with the numeric outer key ("144952"). Since issues is keyed by the
    human-readable Jira key ("PRJ-1"), every lookup returned None and zero
    work packages were updated — silent data loss.
    """
    import src.config as cfg

    numeric_wp_map = {
        "144952": {"jira_key": "PRJ-1", "openproject_id": 2001},
        "144953": {"jira_key": "PRJ-2", "openproject_id": 2002},
    }

    class NumericMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": numeric_wp_map,
                "project": {
                    "PRJ": {"openproject_id": 11},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, mapping):
            self._m[name] = mapping

    monkeypatch.setattr(cfg, "mappings", NumericMappings(), raising=False)

    op = DummyOp()
    mig = VersionsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)

    assert ld.success is True
    # Both WPs (PRJ-1 → op_id 2001, PRJ-2 → op_id 2002) must have been updated.
    # A zero updated count means every issues.get(numeric_key) returned None.
    assert ld.updated == 2, (
        f"Expected 2 WPs updated but got {ld.updated}. "
        "Likely cause: _load looked up issues with numeric outer key instead of jira_key."
    )


def test_refresh_failure_retains_preexisting_project_version_ids():
    from unittest.mock import MagicMock

    from src.application.components.project_named_records import map_project_records
    from src.models import ComponentResult

    migration = MagicMock()
    migration.mappings.get_mapping.return_value = {"AA": {"openproject_id": "4"}}
    migration.op_client.execute_json_query.side_effect = [
        [{"id": 11, "project_id": 4, "name": "One"}],
        OSError("target unavailable"),
    ]
    migration.op_client.bulk_create_records.return_value = {"created_count": 1}
    source = ComponentResult(success=True, data={"by_project": {"AA": ["One", "Two"]}, "issues": {"AA-1": {}}})
    result = map_project_records(migration, source, "Version")
    assert result.data == {"version_map": {"4": {"One": 11}}, "issues": {"AA-1": {}}}
    assert result.created_types == 1
    migration.op_client.bulk_create_records.assert_called_once_with("Version", [{"name": "Two", "project_id": 4}])
    migration.mappings.set_mapping.assert_called_once_with("version", {"4": {"One": 11}})


def test_fix_versions_require_every_reference_to_resolve():
    migration = VersionsMigration(jira_client=DummyJira(), op_client=DummyOp())
    issues = {"PRJ-1": {"id": "189", "key": "PRJ-1", "fields": {"fixVersions": [{"name": "v1"}, {"name": "v2"}]}}}
    projects = {"PRJ": {"openproject_id": 11}}
    versions = {"11": {"v1": 101, "v2": 102}}
    assert migration._version_assignment("PRJ-1", 123, issues, projects, versions) == {
        "id": 123,
        "target_version_ids": [101, 102],
        "jira_version_issue_origin": {
            "project": 11,
            "id": "189",
            "key": "PRJ-1",
            "url": "https://jira.example.invalid/browse/PRJ-1",
        },
    }
    del versions["11"]["v2"]
    with pytest.raises(KeyError):
        migration._version_assignment("PRJ-1", 123, issues, projects, versions)


def test_version_catalogue_failure_is_reported_per_affected_issue(monkeypatch, caplog):
    jira = DummyJira()
    jira.issues["ABC-3"] = DummyIssue("ABC-3", [])
    op = DummyOp()
    monkeypatch.setattr(jira, "get_versions", lambda _project: [])
    migration = VersionsMigration(jira_client=jira, op_client=op)
    result = migration.run()
    assert not result.success
    assert result.updated == 0
    assert result.failed == 2
    assert "PRJ-1" in caplog.text
    assert "v1" in caplog.text


def test_complete_catalogue_includes_unassigned_versions_and_preserves_project_scope(monkeypatch):
    from src import config

    jira, op = DummyJira(), DummyOp()
    config.mappings.set_mapping("work_package", {})
    config.mappings.set_mapping("version", {"22": {"outside": 34}})
    monkeypatch.setitem(config.migration_config, "jira_project_filter", ["PRJ"])
    result = VersionsMigration(jira_client=jira, op_client=op).run()
    assert result.success
    assert config.mappings.get_mapping("version") == {"11": {"v1": 1, "v2": 2, "unused": 3}, "22": {"outside": 34}}
    assert len(op.created_payloads) == 1


def test_cli_project_filter_limits_the_catalogues(monkeypatch):
    from src import config
    from tests.helpers.cli_project_filter import apply_cli_project_filter

    jira, op = DummyJira(), DummyOp()
    config.mappings.set_mapping("work_package", {})
    apply_cli_project_filter(monkeypatch, "PRJ")
    assert VersionsMigration(jira_client=jira, op_client=op).run().success
    assert [payload["key"] for payload in op.created_payloads] == ["PRJ"]


def test_unavailable_catalogue_is_a_failure_without_target_writes(monkeypatch):
    from unittest.mock import Mock

    jira, op = DummyJira(), DummyOp()
    monkeypatch.setattr(jira, "get_versions", Mock(side_effect=OSError("unavailable")))
    result = VersionsMigration(jira_client=jira, op_client=op).run()
    assert not result.success
    assert not op.queries


def test_native_catalogue_preserves_both_flags_and_dates_and_rejects_partial_responses(monkeypatch):
    from src.infrastructure.openproject.version_catalog import sync_project_versions

    op = DummyOp()
    row = {
        "id": "41",
        "projectId": 78,
        "name": "R1",
        "archived": True,
        "released": True,
        "startDate": "2026-01-02",
        "releaseDate": "2026-04-15",
        "description": "Release notes",
    }
    assert sync_project_versions(op, 3, "https://jira.example.invalid/", "PRJ", [row]) == ({"R1": 41}, 1)
    saved = op.created_payloads[0]["versions"][0]
    assert saved == {
        "id": "41",
        "released": True,
        "archived": True,
        "attributes": {
            "name": "R1",
            "description": "Release notes",
            "status": "closed",
            "start_date": "2026-01-02",
            "effective_date": "2026-04-15",
        },
    }
    with pytest.raises(ValueError, match="Ambiguous"):
        sync_project_versions(op, 3, "https://jira.example.invalid", "PRJ", [row, row])
    monkeypatch.setattr(op, "execute_json_query", lambda query: {"versions": {}, "changed": 0})
    with pytest.raises(ValueError, match="Incomplete"):
        sync_project_versions(op, 3, "https://jira.example.invalid", "PRJ", [row])


def test_version_catalogue_reader_rejects_incomplete_or_non_json_responses():
    from unittest.mock import Mock

    from src.infrastructure.jira.jira_client import JiraApiError
    from src.infrastructure.jira.jira_project_service import JiraProjectService

    client = Mock()
    response = client._make_request.return_value
    response.headers = {"Content-Type": "application/json"}
    response.text = "{}"
    response.json.return_value = {"values": []}
    service = JiraProjectService(client)
    with pytest.raises(JiraApiError):
        service.get_versions("PRJ")
    client._make_request.assert_called_once_with("/rest/api/2/project/PRJ/versions")
    response.json.return_value = []
    assert service.get_versions("PRJ") == []


def test_version_origin_rejects_urls_containing_credentials():
    from src.infrastructure.openproject.version_catalog import sync_project_versions

    client = DummyOp()
    with pytest.raises(ValueError, match="credential-free"):
        sync_project_versions(
            client,
            3,
            "https://user@jira.example.invalid/",
            "PRJ",
            [{"projectId": 78, "id": "1", "name": "v", "released": False, "archived": False}],
        )


def test_version_assignments_require_positive_unrestricted_search_and_pagination():
    from unittest.mock import Mock

    from src.infrastructure.jira.jira_client import JiraApiError
    from src.infrastructure.jira.jira_project_service import JiraProjectService

    client = Mock()
    response = client._make_request.return_value
    response.headers = {"Content-Type": "application/json"}
    response.text = "{}"
    issue = {"key": "PRJ-1", "fields": {"project": {"key": "PRJ"}}}
    response.json.return_value = {"startAt": 0, "total": 1, "issues": [issue]}
    service = JiraProjectService(client)
    assert service.unrestricted_version_issues("PRJ", ["PRJ-1", "PRJ-2"]) == {"PRJ-1": issue}
    assert "level IS EMPTY" in client._make_request.call_args.kwargs["params"]["jql"]
    issue["fields"]["security"] = {"id": "99"}
    with pytest.raises(JiraApiError, match="restricted"):
        service.unrestricted_version_issues("PRJ", ["PRJ-1"])
