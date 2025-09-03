import pytest

from src.migrations.category_defaults_migration import CategoryDefaultsMigration


class DummyJira:
    def __init__(self) -> None:
        # get_project_components returns dicts with name and lead
        self.components = {
            "PRJ": [
                {"name": "Backend", "lead": {"name": "alice"}},
                {"name": "API", "lead": {"name": "bob"}},
            ]
        }

    def get_project_components(self, project_key: str):  # noqa: ANN201
        return self.components.get(project_key, [])


class DummyOp:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    def execute_query(self, script: str):  # noqa: ANN201
        self.scripts.append(script)
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.mappings as pkg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PRJ": {"openproject_id": 20001}},
                "user": {
                    "alice": {"openproject_id": 30001},
                    "bob": {"openproject_id": 30002},
                },
            }

        def get_mapping(self, name: str):  # noqa: ANN201
            return self._m.get(name, {})

    monkeypatch.setattr(pkg, "Mappings", DummyMappings)


def test_category_defaults_sets_assignee_from_component_lead():
    mig = CategoryDefaultsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    # two components -> two updates
    assert ld.updated == 2


