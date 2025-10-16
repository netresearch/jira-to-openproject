import pytest

from src.migrations.story_points_migration import STORY_POINTS_CF_NAME, StoryPointsMigration


class DummyFields:
    def __init__(self, sp=None, cf=None):
        self.storyPoints = sp
        self.customfield_10016 = cf


class DummyIssue:
    def __init__(self, key: str, sp=None, cf=None):
        self.key = key
        self.fields = DummyFields(sp=sp, cf=cf)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "PRJ-1": DummyIssue("PRJ-1", sp=3),
            "PRJ-2": DummyIssue("PRJ-2", sp=None, cf=5.5),
            "PRJ-3": DummyIssue("PRJ-3", sp=None, cf=None),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def get_custom_field_by_name(self, name: str):
        assert name == STORY_POINTS_CF_NAME
        raise Exception("not found")

    def execute_query(self, script: str):
        self.queries.append(script)
        if "cf.id" in script:
            return 801
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "PRJ-1": {"openproject_id": 11001},
                    "PRJ-2": {"openproject_id": 11002},
                    "PRJ-3": {"openproject_id": 11003},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_story_points_migration_sets_cf():
    mig = StoryPointsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    # PRJ-1, PRJ-2 have values -> 2 updates
    assert ld.success is True
    assert ld.updated == 2


