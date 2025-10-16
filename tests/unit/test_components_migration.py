import pytest

from src.migrations.components_migration import ComponentsMigration


class DummyComp:
    def __init__(self, name: str) -> None:
        self.name = name


class DummyFields:
    def __init__(self, components: list[DummyComp]):
        self.components = components


class DummyIssue:
    def __init__(self, key: str, comps: list[str]) -> None:
        self.key = key
        self.fields = DummyFields([DummyComp(n) for n in comps])


class DummyJira:
    def __init__(self) -> None:
        self.issues: dict[str, DummyIssue] = {
            "PRJ-1": DummyIssue("PRJ-1", ["Backend", "API"]),
            "PRJ-2": DummyIssue("PRJ-2", ["Backend"]),
            "ABC-3": DummyIssue("ABC-3", ["Core"]),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues[k] for k in keys if k in self.issues}


class DummyOp:
    def __init__(self) -> None:
        self.created_payloads: list[dict] = []

    def execute_json_query(self, q: str):
        if "Category.where" in q:
            return [
                {"id": 501, "name": rec.get("name"), "project_id": rec.get("project_id")}
                for rec in self.created_payloads
            ]
        return []

    def bulk_create_records(self, model: str, records: list[dict], **_):
        assert model == "Category"
        self.created_payloads.extend(records)
        return {"created_count": len(records)}

    def batch_update_work_packages(self, updates):
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


def test_components_migration_end_to_end():
    mig = ComponentsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert mp.success is True
    assert ld.success is True
    assert ld.updated >= 1

