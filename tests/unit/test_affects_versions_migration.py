import pytest

from src.migrations.affects_versions_migration import (
    AFFECTS_VERSIONS_CF_NAME,
    AffectsVersionsMigration,
)


class DummyVer:
    def __init__(self, name: str) -> None:
        self.name = name


class DummyFields:
    def __init__(self, versions: list[DummyVer]):
        self.versions = versions


class DummyIssue:
    def __init__(self, key: str, versions: list[str]) -> None:
        self.key = key
        self.fields = DummyFields([DummyVer(n) for n in versions])


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "PRJ-1": DummyIssue("PRJ-1", ["1.0", "2.0"]),
            "PRJ-2": DummyIssue("PRJ-2", ["2.0", "1.0"]),
            "PRJ-3": DummyIssue("PRJ-3", []),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues[k] for k in keys if k in self.issues}


class DummyOp:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    def get_custom_field_by_name(self, name: str):
        assert name == AFFECTS_VERSIONS_CF_NAME
        raise Exception("not found")

    def execute_query(self, script: str):
        self.scripts.append(script)
        # First call returns CF id; subsequent returns true for updates
        if "CustomField" in script and "cf.id" in script:
            return 555
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
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
