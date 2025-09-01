import pytest

from src.migrations.priority_migration import PriorityMigration


class DummyJira:
    def get_priorities(self):  # noqa: ANN201
        return [
            {"name": "High"},
            {"name": "Normal"},
        ]

    def batch_get_issues(self, keys):  # noqa: ANN201, ANN001
        class F:
            def __init__(self, name: str) -> None:
                class FF:
                    def __init__(self, n: str) -> None:
                        self.priority = type("P", (), {"name": n})

                self.fields = FF(name)

        return {"J1": F("High")}


class DummyOp:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.updated: list[dict] = []

    def get_issue_priorities(self):  # noqa: ANN201
        return [{"id": 1, "name": "Normal"}]

    def create_issue_priority(self, name: str, position=None, is_default=False):  # noqa: ANN001, ANN201
        self.created.append(name)
        return {"id": 2 if name == "High" else 3, "name": name}

    def batch_update_work_packages(self, updates):  # noqa: ANN001, ANN201
        self.updated.extend(updates)
        return {"updated": len(updates), "failed": 0, "results": []}


@pytest.fixture(autouse=True)
def _map_store(monkeypatch: pytest.MonkeyPatch):
    class DummyMappings:
        def __init__(self) -> None:
            self._maps: dict[str, dict] = {
                "work_package": {"J1": {"openproject_id": 10}},
            }

        def get_mapping(self, name: str):  # noqa: ANN201
            return self._maps.get(name, {})

        def set_mapping(self, name: str, mapping):  # noqa: ANN001, ANN201
            self._maps[name] = mapping

    from src import mappings as _m  # type: ignore  # noqa: PLC0415
    monkeypatch.setattr(_m, "Mappings", DummyMappings)


def test_priority_migration_creates_and_updates(monkeypatch: pytest.MonkeyPatch):
    jira = DummyJira()
    op = DummyOp()

    # No enhanced client patching needed; DummyJira provides batch_get_issues

    mig = PriorityMigration(jira_client=jira, op_client=op)  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)

    assert mp.created_types == 1  # High created
    assert any(u["priority_id"] == 2 for u in op.updated)
    assert ld.success is True

