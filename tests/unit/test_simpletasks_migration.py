import pytest

from src.migrations.simpletasks_migration import SimpleTasksMigration


class DummyJira:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def get_issue_property(self, key: str, prop: str):  # noqa: ANN001, ANN201
        return self._payload.get(key)


class DummyOp:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def set_checklist_section(self, wp_id: int, md: str, **_kwargs):  # noqa: ANN201
        self.calls.append((wp_id, md))
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    from src import mappings as pkg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "J1": {"openproject_id": 101},
                    "J2": {"openproject_id": 102},
                }
            }

        def get_mapping(self, name: str):  # noqa: ANN201
            return self._m.get(name, {})

    monkeypatch.setattr(pkg, "Mappings", DummyMappings)


def test_simpletasks_migration_renders_markdown_and_updates():
    jira = DummyJira(
        {
            "J1": {"tasks": [{"title": "A", "checked": True}, {"title": "B", "labels": ["x", "y"]}]},
            "J2": {"value": [{"text": "C", "dueDate": "2025-09-01"}]},
        }
    )
    op = DummyOp()

    mig = SimpleTasksMigration(jira_client=jira, op_client=op)  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)

    assert ld.success is True
    assert ld.updated == 2
    # Verify markdown shape
    assert any("- [x] A" in md for _, md in op.calls)
    assert any("labels: x, y" in md for _, md in op.calls)
    assert any("due:" in md for _, md in op.calls)


