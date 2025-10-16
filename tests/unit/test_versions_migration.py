import pytest

from src.migrations.versions_migration import VersionsMigration


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

