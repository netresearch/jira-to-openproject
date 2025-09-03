import pytest

from src.migrations.sprint_epic_migration import SprintEpicMigration, SPRINT_CF_NAME


class DummyFields:
    def __init__(self, epic=None, sprint=None):  # noqa: ANN001
        self.customfield_10008 = epic  # Epic Link common
        self.customfield_10020 = sprint  # Sprint common


class DummyIssue:
    def __init__(self, key: str, epic=None, sprint=None):  # noqa: ANN001
        self.key = key
        self.fields = DummyFields(epic=epic, sprint=sprint)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "EPIC-1": DummyIssue("EPIC-1", epic=None, sprint=[{"name": "Sprint A"}]),
            "PRJ-1": DummyIssue("PRJ-1", epic="EPIC-1", sprint=[{"name": "Sprint A"}, {"name": "Sprint B"}]),
            "PRJ-2": DummyIssue("PRJ-2", epic=None, sprint=None),
        }

    def batch_get_issues(self, keys):  # noqa: ANN201, ANN001
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.queries: list[str] = []

    def batch_update_work_packages(self, updates):  # noqa: ANN201, ANN001
        self.updates.extend(updates)
        return {"updated": len(updates), "failed": 0}

    def get_custom_field_by_name(self, name: str):  # noqa: ANN201
        assert name == SPRINT_CF_NAME
        raise Exception("not found")

    def execute_query(self, script: str):  # noqa: ANN201
        self.queries.append(script)
        if "cf.id" in script:
            return 901
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.mappings as pkg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "EPIC-1": {"openproject_id": 12000},
                    "PRJ-1": {"openproject_id": 12001},
                    "PRJ-2": {"openproject_id": 12002},
                }
            }

        def get_mapping(self, name: str):  # noqa: ANN201
            return self._m.get(name, {})

    monkeypatch.setattr(pkg, "Mappings", DummyMappings)


def test_sprint_epic_migration_sets_parent_and_sprint_cf():
    mig = SprintEpicMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    # Expect: 1 parent link (PRJ-1 -> EPIC-1) + 2 sprint CF updates (EPIC-1, PRJ-1)
    # batch_update_work_packages updated=1, CF updates add 2 more -> updated==3
    assert ld.updated == 3


