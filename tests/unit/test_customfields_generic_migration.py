import pytest

from src.migrations.customfields_generic_migration import CustomFieldsGenericMigration


class FieldsObj:
    def __init__(self) -> None:
        self.customfield_10010 = [
            {"id": "1", "name": "OptA"},
            {"id": "2", "name": "OptB"},
        ]
        self.customfield_10011 = "Free text"


class DummyIssue:
    def __init__(self, key: str) -> None:
        self.key = key
        self.fields = FieldsObj()


class DummyJira:
    def batch_get_issues(self, keys):
        return {k: DummyIssue(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def get_custom_field_by_name(self, name: str):
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
                    "PRJ-1": {"openproject_id": 20001},
                },
                "custom_field": {
                    "customfield_10010": {
                        "jira_id": "customfield_10010",
                        "jira_name": "Multi Option",
                        "openproject_name": "Multi Option",
                        "openproject_type": "list",
                    },
                    "customfield_10011": {
                        "jira_id": "customfield_10011",
                        "jira_name": "Text Field",
                        "openproject_name": "Text Field",
                        "openproject_type": "text",
                    },
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_customfields_generic_migration_extracts_and_sets_cf():
    mig = CustomFieldsGenericMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    assert ex.success is True
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated >= 1
