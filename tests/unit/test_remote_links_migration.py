import pytest

from src.migrations.remote_links_migration import SECTION_TITLE, RemoteLinksMigration


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

    def batch_get_issues(self, keys):
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.bulk_sections: list[dict] = []

    def upsert_work_package_description_section(self, work_package_id: int, section_marker: str, content: str):
        assert section_marker == SECTION_TITLE
        self.calls.append((work_package_id, section_marker, content))
        return True

    def bulk_upsert_wp_description_sections(self, sections: list[dict]):
        self.bulk_sections.extend(sections)
        for s in sections:
            self.calls.append((s["work_package_id"], s["section_marker"], s["content"]))
        return {"updated": len(sections), "failed": 0}


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "PRJ-1": {"openproject_id": 13001},
                    "PRJ-2": {"openproject_id": 13002},
                    "PRJ-3": {"openproject_id": 13003},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_remote_links_migration_renders_markdown_and_updates():
    mig = RemoteLinksMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated == 2
