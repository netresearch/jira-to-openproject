import pytest

from src.migrations.inline_refs_migration import InlineRefsMigration


class DummyJira:
    pass


class DummyOp:
    def __init__(self) -> None:
        self.last_ids = None

    def execute_script_with_data(self, script_content: str, data: object):  # noqa: ANN201
        self.last_ids = list(data) if isinstance(data, list) else []
        return {"updated": len(self.last_ids), "failed": 0}


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.mappings as pkg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {"work_package": {"PRJ-1": {"openproject_id": 801}, "PRJ-2": {"openproject_id": 802}}}

        def get_mapping(self, name: str):  # noqa: ANN201
            return self._m.get(name, {})

    monkeypatch.setattr(pkg, "Mappings", DummyMappings)


def test_inline_refs_invokes_script_with_wp_ids():
    mig = InlineRefsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated == 2


