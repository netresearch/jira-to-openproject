import pytest

from src.migrations.resolution_migration import RESOLUTION_CF_NAME, ResolutionMigration


class DummyIssue:
    def __init__(self, name: str | None) -> None:
        class F:
            def __init__(self, n: str | None) -> None:
                self.resolution = type("R", (), {"name": n}) if n else None

        self.fields = F(name)


class DummyJira:
    def batch_get_issues(self, keys):
        return {"J1": DummyIssue("Fixed"), "J2": DummyIssue(None)}


class DummyOp:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def get_custom_field_by_name(self, name: str):
        assert name == RESOLUTION_CF_NAME
        raise Exception("not found")

    def execute_query(self, script: str):
        self.queries.append(script)
        if "cf.id" in script:
            return 99
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "J1": {"openproject_id": 1001},
                    "J2": {"openproject_id": 1002},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_resolution_migration_sets_cf_and_journal():
    mig = ResolutionMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated >= 1
