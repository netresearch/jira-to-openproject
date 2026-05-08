import pytest

from src.application.components.versions_migration import VersionsMigration


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
        self.issues: dict[str, DummyIssue] = {
            "PRJ-1": DummyIssue("PRJ-1", ["v1", "v2"]),
            "PRJ-2": DummyIssue("PRJ-2", ["v1"]),
            "ABC-3": DummyIssue("ABC-3", ["alpha"]),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues[k] for k in keys if k in self.issues}


class DummyOp:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.created_payloads: list[dict] = []

    def execute_json_query(self, q: str):
        self.queries.append(q)
        if "Version.where" in q:
            # Return synthesized existing versions from what we've created so far
            out = []
            for i, rec in enumerate(self.created_payloads, start=1):
                out.append({"id": 100 + i, "name": rec.get("name"), "project_id": rec.get("project_id")})
            return out
        return []

    def bulk_create_records(self, model: str, records: list[dict], **_):
        assert model == "Version"
        self.created_payloads.extend(records)
        return {"created_count": len(records)}

    def batch_update_work_packages(self, updates):
        # Pretend all succeeded
        return {"updated": len(updates)}


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
