import pytest

from src.application.components.attachment_provenance_migration import AttachmentProvenanceMigration
from src.models import ComponentResult


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
            "PRJ-1": DummyIssue(
                "PRJ-1",
                [DummyAtt("a.txt", "2024-01-01T00:00:00Z"), DummyAtt("b.txt", "2024-01-02T00:00:00Z")],
            ),
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
                    "PRJ-1": {"openproject_id": 501, "jira_key": "PRJ-1"},
                    "PRJ-2": {"openproject_id": 502, "jira_key": "PRJ-2"},
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
    # Use _extract_batch (the actual extraction method) — _extract is not
    # implemented for this custom-run() migration.
    items = mig._extract_batch(["PRJ-1", "PRJ-2"])
    extracted = ComponentResult(success=True, data={"items": items})
    mp = mig._map(extracted)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated == 2


def test_attachment_provenance_fails_loud_on_empty_wp_mapping(
    monkeypatch: pytest.MonkeyPatch,
):
    """Empty WP mapping must FAIL loud, not silently exit success.

    Same anti-pattern as the pre-#194 attachments path. If
    ``work_packages_skeleton`` doesn't persist its mapping (e.g.
    ``_save_mapping`` swallows a write error per #197), this
    component used to return ``success=True, updated=0`` —
    masking the missing precondition. Pin: empty WP mapping →
    ``success=False`` with a stable error tag.
    """
    import src.config as cfg

    class EmptyMappings:
        def get_mapping(self, name: str):
            return {}

    monkeypatch.setattr(cfg, "mappings", EmptyMappings(), raising=False)

    mig = AttachmentProvenanceMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    result = mig.run()

    assert result.success is False, result
    assert any("missing_work_package_mapping" in str(e) for e in (result.errors or [])), result.errors
    assert "work_package" in (result.message or "").lower(), result.message


def test_attachment_provenance_fails_loud_on_legacy_int_only_mapping(
    monkeypatch: pytest.MonkeyPatch,
):
    """Mapping non-empty but contains only legacy bare-int rows → FAIL loud.

    Closes review thread on PR #198. The pre-fix code only guarded
    against an *empty* ``wp_map``; a legacy-int-only mapping (e.g.
    ``{"PROJ-1": 42}``) passed the empty check, then the
    grouping loop filtered every row out, leaving ``by_project={}``.
    The function returned ``success=True, updated=0`` and silently
    lost attachment provenance for the whole run.
    """
    import src.config as cfg

    class LegacyIntMappings:
        def get_mapping(self, name: str):
            if name == "work_package":
                # Bare-int rows: ``isinstance(raw_entry, dict)`` is False,
                # so ``inner_key`` ends up ``None`` and the row is filtered
                # out of ``by_project``.
                return {"PROJ-1": 42, "PROJ-2": 43}
            return {}

    monkeypatch.setattr(cfg, "mappings", LegacyIntMappings(), raising=False)

    mig = AttachmentProvenanceMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    result = mig.run()

    assert result.success is False, result
    assert any("missing_work_package_mapping" in str(e) for e in (result.errors or [])), result.errors
    # Message must mention the present-but-unusable distinction so
    # operators know to back-fill ``jira_key`` rather than re-running
    # skeleton from scratch.
    assert "no usable rows" in (result.message or "").lower(), result.message
