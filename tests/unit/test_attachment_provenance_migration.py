import pytest

from src.migrations.attachment_provenance_migration import AttachmentProvenanceMigration


class DummyAtt:
    def __init__(self, filename: str, created: str, author: dict | None = None) -> None:
        self.filename = filename
        self.created = created
        self.author = author or {"name": "alice"}


class DummyFields:
    def __init__(self, attachments):
        self.attachment = attachments


class DummyIssue:
    def __init__(self, key: str, attachments):
        self.key = key
        self.fields = DummyFields(attachments)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "PRJ-1": DummyIssue("PRJ-1", [DummyAtt("a.txt", "2024-01-01T00:00:00Z"), DummyAtt("b.txt", "2024-01-02T00:00:00Z")]),
            "PRJ-2": DummyIssue("PRJ-2", []),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.last_payload = None

    def execute_script_with_data(self, script_content: str, data: object):
        self.last_payload = list(data) if isinstance(data, list) else []
        # Pretend all updates succeed
        return {"updated": len(self.last_payload), "failed": 0}


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "PRJ-1": {"openproject_id": 501},
                    "PRJ-2": {"openproject_id": 502},
                },
                "user": {
                    "alice": {"openproject_id": 301},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_attachment_provenance_updates_author_and_timestamp():
    mig = AttachmentProvenanceMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated == 2


