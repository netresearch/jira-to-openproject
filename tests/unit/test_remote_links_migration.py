import pytest

from src.migrations.remote_links_migration import RemoteLinksMigration, SECTION_TITLE


class DummyFields:
    def __init__(self, items):
        self.remotelinks = items


class DummyIssue:
    def __init__(self, key: str, links):
        self.key = key
        self.fields = DummyFields(links)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "PRJ-1": DummyIssue("PRJ-1", [{"object": {"url": "https://a.example/x", "title": "A"}}]),
            "PRJ-2": DummyIssue("PRJ-2", [{"object": {"url": "https://b.example/y"}}]),
            "PRJ-3": DummyIssue("PRJ-3", []),
        }

    def batch_get_issues(self, keys):  # noqa: ANN201, ANN001
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []

    def upsert_work_package_description_section(self, work_package_id: int, section_marker: str, content: str):  # noqa: ANN201
        assert section_marker == SECTION_TITLE
        self.calls.append((work_package_id, section_marker, content))
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.mappings as pkg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "PRJ-1": {"openproject_id": 13001},
                    "PRJ-2": {"openproject_id": 13002},
                    "PRJ-3": {"openproject_id": 13003},
                }
            }

        def get_mapping(self, name: str):  # noqa: ANN201
            return self._m.get(name, {})

    monkeypatch.setattr(pkg, "Mappings", DummyMappings)


def test_remote_links_migration_renders_markdown_and_updates():
    mig = RemoteLinksMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated == 2


