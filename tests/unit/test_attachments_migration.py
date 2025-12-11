import os
from pathlib import Path

import pytest

from src.migrations.attachments_migration import AttachmentsMigration


class DummyAtt:
    def __init__(self, id: str, filename: str, url: str, size: int = 10) -> None:  # noqa: A002
        self.id = id
        self.filename = filename
        self.content = url
        self.size = size


class DummyFields:
    def __init__(self, attachments: list[DummyAtt]):
        self.attachment = attachments


class DummyIssue:
    def __init__(self, key: str, atts: list[DummyAtt]) -> None:
        self.key = key
        self.fields = DummyFields(atts)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "PRJ-1": DummyIssue(
                "PRJ-1", [DummyAtt("1", "a.txt", "http://example/a"), DummyAtt("2", "b.txt", "http://example/b")]
            ),
            "PRJ-2": DummyIssue("PRJ-2", [DummyAtt("3", "a.txt", "http://example/a")]),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues[k] for k in keys if k in self.issues}


class DummyOp:
    def __init__(self) -> None:
        self.transfers: list[tuple[Path, str]] = []
        self.last_input: list[dict] | None = None

    def transfer_file_to_container(self, local_path: Path, container_path: str):
        self.transfers.append((local_path, container_path))

    def execute_script_with_data(self, script_content: str, data: object):
        self.last_input = list(data) if isinstance(data, list) else []
        return {"updated": len(self.last_input), "failed": 0}


@pytest.fixture(autouse=True)
def _mock_mappings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "PRJ-1": {"openproject_id": 2001},
                    "PRJ-2": {"openproject_id": 2002},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)

    # Ensure attachment dir exists and stub download
    def fake_download(self, url: str, dest_path: Path):
        dest_path.write_bytes(os.urandom(32))
        return dest_path

    monkeypatch.setattr(AttachmentsMigration, "_download_attachment", fake_download, raising=True)


def test_attachments_migration_end_to_end(tmp_path: Path):
    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert mp.success is True
    assert ld.success is True
    assert ld.updated >= 2
