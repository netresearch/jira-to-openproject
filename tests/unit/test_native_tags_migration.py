import pytest

from src.migrations.native_tags_migration import NativeTagsMigration


class DummyFields:
    def __init__(self, labels):
        self.labels = labels


class DummyIssue:
    def __init__(self, key: str, labels):
        self.key = key
        self.fields = DummyFields(labels)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "PRJ-1": DummyIssue("PRJ-1", ["alpha", "beta", "alpha"]),
            "PRJ-2": DummyIssue("PRJ-2", []),
        }

    def batch_get_issues(self, keys):  # noqa: ANN201, ANN001
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.payload = None

    def execute_script_with_data(self, script_content: str, data: object):  # noqa: ANN201
        self.payload = list(data) if isinstance(data, list) else []
        return {"updated": len(self.payload), "failed": 0}


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.mappings as pkg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {"work_package": {"PRJ-1": {"openproject_id": 901}, "PRJ-2": {"openproject_id": 902}}}

        def get_mapping(self, name: str):  # noqa: ANN201
            return self._m.get(name, {})

    monkeypatch.setattr(pkg, "Mappings", DummyMappings)


def test_native_tags_assigns_tags_to_work_packages():
    mig = NativeTagsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated == 1


