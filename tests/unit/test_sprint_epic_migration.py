import pytest

from src.migrations.sprint_epic_migration import SPRINT_CF_NAME, SprintEpicMigration


class DummyFields:
    def __init__(self, epic=None, sprint=None):
        self.customfield_10008 = epic  # Epic Link common
        self.customfield_10020 = sprint  # Sprint common


class DummyIssue:
    def __init__(self, key: str, epic=None, sprint=None):
        self.key = key
        self.fields = DummyFields(epic=epic, sprint=sprint)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "EPIC-1": DummyIssue("EPIC-1", epic=None, sprint=[{"name": "Sprint A"}]),
            "PRJ-1": DummyIssue("PRJ-1", epic="EPIC-1", sprint=[{"name": "Sprint A"}, {"name": "Sprint B"}]),
            "PRJ-2": DummyIssue("PRJ-2", epic=None, sprint=None),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.queries: list[str] = []

    def batch_update_work_packages(self, updates):
        self.updates.extend(updates)
        return {"updated": len(updates), "failed": 0}

    def get_custom_field_by_name(self, name: str):
        assert name == SPRINT_CF_NAME
        raise Exception("not found")

    def execute_query(self, script: str):
        self.queries.append(script)
        if "cf.id" in script:
            return 901
        return True

    def execute_query_to_json_file(self, script: str):
        """Same behavior as execute_query but returns the result directly."""
        return self.execute_query(script)


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "EPIC-1": {"openproject_id": 12000},
                    "PRJ-1": {"openproject_id": 12001},
                    "PRJ-2": {"openproject_id": 12002},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_sprint_epic_migration_sets_parent_and_sprint_cf():
    mig = SprintEpicMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    # Expect: 1 parent link (PRJ-1 -> EPIC-1) + 2 sprint CF updates (EPIC-1, PRJ-1)
    # batch_update_work_packages updated=1, CF updates add 2 more -> updated==3
    assert ld.updated == 3
