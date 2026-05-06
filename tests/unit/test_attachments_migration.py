import os
from pathlib import Path

import pytest

from src.application.components.attachments_migration import AttachmentsMigration
from src.models import ComponentResult


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
                "PRJ-1",
                [DummyAtt("1", "a.txt", "http://example/a"), DummyAtt("2", "b.txt", "http://example/b")],
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
        # Return results in the expected format for attachments_migration._load
        results = []
        for i, item in enumerate(self.last_input):
            results.append(
                {
                    "jira_key": item.get("jira_key"),
                    "filename": item.get("filename"),
                    "attachment_id": 1000 + i,
                },
            )
        return {"results": results, "errors": []}


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
    # Build extracted attachment data directly — _extract() is a legacy no-op;
    # the real run() uses _extract_batch() which returns this format.
    att_data = {
        "PRJ-1": [
            {"id": "1", "filename": "a.txt", "size": 10, "url": "http://example/a"},
            {"id": "2", "filename": "b.txt", "size": 10, "url": "http://example/b"},
        ],
        "PRJ-2": [
            {"id": "3", "filename": "a.txt", "size": 10, "url": "http://example/a"},
        ],
    }
    ex = ComponentResult(success=True, data={"attachments": att_data})
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert mp.success is True
    assert ld.success is True
    assert ld.updated >= 2


def test_attachments_migration_fails_loud_on_empty_wp_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Empty WP mapping must FAIL loud, not silently exit success.

    Caught by the live TEST audit (2026-05-06): TEST showed Jira=10
    attachments, OP=0 — 100% loss. Root cause: ``run()`` exited
    early with ``success=True, updated=0`` when
    ``_wp_lookup_by_jira_key()`` returned an empty dict (because
    the WP migration hadn't completed or hadn't persisted its
    mapping). The ``success=True`` verdict masked the real failure
    — the orchestrator moved on, the audit only saw the OP-side
    count, and the missing precondition was invisible.

    Pin: empty WP mapping → ``ComponentResult(success=False)`` with
    a ``missing_work_package_mapping`` error tag and a message
    pointing the operator at the precondition. The orchestration
    will then surface it instead of silently swallowing.
    """
    import src.config as cfg

    # Override the autouse fixture's mapping with an EMPTY one.
    class EmptyMappings:
        def get_mapping(self, name: str):
            return {}

    monkeypatch.setattr(cfg, "mappings", EmptyMappings(), raising=False)

    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    result = mig.run()

    assert result.success is False, f"Empty WP mapping must fail loud, not silently succeed. Got: {result}"
    assert "work_package" in (result.message or "").lower(), result.message
    # Error tag for downstream consumers (audit, dashboards, alerts)
    assert any("missing_work_package_mapping" in str(e) for e in (result.errors or [])), result.errors
