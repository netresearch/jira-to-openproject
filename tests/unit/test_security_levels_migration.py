import pytest

from src.migrations.security_levels_migration import (
    SecurityLevelsMigration,
    SECURITY_LEVEL_CF_NAME,
)


class DummySec:
    def __init__(self, name: str | None) -> None:
        self.name = name


class DummyFields:
    def __init__(self, sec: str | None):
        self.security = DummySec(sec) if sec else None


class DummyIssue:
    def __init__(self, key: str, sec: str | None) -> None:
        self.key = key
        self.fields = DummyFields(sec)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "PRJ-1": DummyIssue("PRJ-1", "Top Secret"),
            "PRJ-2": DummyIssue("PRJ-2", None),
        }

    def batch_get_issues(self, keys):  # noqa: ANN201, ANN001
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def get_custom_field_by_name(self, name: str):  # noqa: ANN201
        assert name == SECURITY_LEVEL_CF_NAME
        raise Exception("not found")

    def execute_query(self, script: str):  # noqa: ANN201
        self.queries.append(script)
        if "cf.id" in script:
            return 501
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.mappings as pkg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "PRJ-1": {"openproject_id": 9001},
                    "PRJ-2": {"openproject_id": 9002},
                }
            }

        def get_mapping(self, name: str):  # noqa: ANN201
            return self._m.get(name, {})

    monkeypatch.setattr(pkg, "Mappings", DummyMappings)


def test_security_levels_migration_sets_cf():
    mig = SecurityLevelsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated == 1


