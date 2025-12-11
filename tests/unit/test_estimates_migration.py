import pytest

from src.migrations.estimates_migration import EstimatesMigration


class DummyTT:
    def __init__(self, originalEstimate=None, remainingEstimate=None):
        self.originalEstimate = originalEstimate
        self.remainingEstimate = remainingEstimate


class DummyFields:
    def __init__(self, timeoriginalestimate=None, timeestimate=None, timetracking=None):
        self.timeoriginalestimate = timeoriginalestimate
        self.timeestimate = timeestimate
        self.timetracking = timetracking


class DummyIssue:
    def __init__(self, key: str, fields: DummyFields) -> None:
        self.key = key
        self.fields = fields


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "PRJ-1": DummyIssue("PRJ-1", DummyFields(timeoriginalestimate=7200, timeestimate=3600)),
            "PRJ-2": DummyIssue("PRJ-2", DummyFields(timetracking=DummyTT("1h 30m", "45m"))),
            "PRJ-3": DummyIssue("PRJ-3", DummyFields(timeoriginalestimate=None, timeestimate=None)),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues[k] for k in keys if k in self.issues}


class DummyOp:
    def __init__(self) -> None:
        self.updated_payloads: list[dict] = []

    def batch_update_work_packages(self, updates):
        self.updated_payloads.extend(updates)
        return {"updated": len(updates), "failed": 0}


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


def test_estimates_migration_end_to_end():
    mig = EstimatesMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert mp.success is True
    assert ld.success is True
    # Should update PRJ-1 and PRJ-2; PRJ-3 has no estimates
    assert ld.updated == 2
