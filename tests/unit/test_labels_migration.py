import pytest

from src.migrations.labels_migration import LabelsMigration, LABELS_CF_NAME


class DummyIssue:
    def __init__(self, labels: list[str] | None) -> None:
        class F:
            def __init__(self, labels: list[str] | None) -> None:
                self.labels = labels

        self.fields = F(labels)


class DummyJira:
    def batch_get_issues(self, keys):  # noqa: ANN201, ANN001
        return {"J1": DummyIssue(["x", "y", "x"]) , "J2": DummyIssue([])}


class DummyOp:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    def get_custom_field_by_name(self, name: str):  # noqa: ANN201
        assert name == LABELS_CF_NAME
        raise Exception("not found")

    def execute_query(self, script: str):  # noqa: ANN201
        self.scripts.append(script)
        if "cf.id" in script:
            return 77
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.mappings as pkg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "J1": {"openproject_id": 2001},
                    "J2": {"openproject_id": 2002},
                }
            }

        def get_mapping(self, name: str):  # noqa: ANN201
            return self._m.get(name, {})

    monkeypatch.setattr(pkg, "Mappings", DummyMappings)


def test_labels_migration_sets_cf_text():
    mig = LabelsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated >= 1

