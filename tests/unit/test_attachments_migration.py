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
        # Mirror the real ``OpenProjectRailsRunner.execute_script_with_data``
        # envelope: ``{status, message, data, output}``. The counters live
        # under ``data`` (the parsed JSON the Ruby script prints between
        # ``$j2o_start_marker`` / ``$j2o_end_marker``). The earlier flat
        # shape masked a real production bug where ``_load`` was reading
        # ``res.get("results")`` directly off the envelope and silently
        # always returning ``updated=0`` — caught alongside the
        # filename-fidelity fix.
        results = []
        for i, item in enumerate(self.last_input):
            results.append(
                {
                    "jira_key": item.get("jira_key"),
                    "filename": item.get("filename"),
                    "attachment_id": 1000 + i,
                },
            )
        return {
            "status": "success",
            "message": "ok",
            "data": {"results": results, "errors": []},
            "output": "<dummy>",
        }


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


def test_attachments_migration_fails_loud_on_legacy_int_only_mapping(
    monkeypatch: pytest.MonkeyPatch,
):
    """Mapping non-empty but contains only legacy bare-int rows → FAIL loud.

    Closes review thread on PR #194. The pre-fix message implied the
    mapping was *absent* ("No work_package mapping available"); when
    it actually contained only legacy bare-int rows that
    ``_wp_lookup_by_jira_key`` strips, the operator was given the
    wrong remediation hint. Pin: present-but-unusable mapping →
    different message, same error tag.
    """
    import src.config as cfg

    class LegacyIntMappings:
        def get_mapping(self, name: str):
            if name == "work_package":
                # Bare-int rows ``isinstance(raw_entry, dict)`` is False
                # in ``_wp_lookup_by_jira_key`` so every row is skipped.
                return {"PROJ-1": 42, "PROJ-2": 43}
            return {}

    monkeypatch.setattr(cfg, "mappings", LegacyIntMappings(), raising=False)

    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    result = mig.run()

    assert result.success is False, result
    assert any("missing_work_package_mapping" in str(e) for e in (result.errors or [])), result.errors
    # Operator-facing distinction: the message must NOT say "No
    # work_package mapping available" (which implies absent); it must
    # say the mapping is present but has no usable rows.
    msg_lower = (result.message or "").lower()
    assert "no usable rows" in msg_lower, result.message
    assert "legacy" in msg_lower, result.message


# --- noname / empty-filename handling (added 2026-05-07) ---


def test_extract_batch_derives_filename_for_empty_or_noname(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Empty / ``"noname"`` Jira filename → derived ``jira-attachment-<id>``.

    Live 2026-05-07 NRS regression: NRS-4347 had 3 attachments with
    filename ``"noname"`` (Jira's placeholder for clipboard / paste
    uploads without a real name). The pre-fix
    ``_extract_batch`` skipped them via the ``not filename.strip()``
    guard. The Rails-side ``LOWER(filename)`` idempotency would have
    further collapsed them to one entry even if they made it through.
    Pin: derived names per attachment id keep all three distinct so
    Rails creates three rows.
    """
    from types import SimpleNamespace

    # Build a fake issue with three attachments — empty, "noname",
    # whitespace-only — exercising the three skip paths the
    # pre-fix code took.
    class _FakeAtt:
        def __init__(self, id_: str, filename: str | None) -> None:
            self.id = id_
            self.filename = filename
            self.size = 100
            self.content = f"http://jira/secure/attachment/{id_}/blob"

    class _FakeFields:
        def __init__(self, atts: list[_FakeAtt]) -> None:
            self.attachment = atts

    class _FakeIssue:
        def __init__(self, key: str, atts: list[_FakeAtt]) -> None:
            self.key = key
            self.fields = _FakeFields(atts)

    class _BatchJira:
        def __init__(self) -> None:
            self.jira = SimpleNamespace(
                search_issues=lambda *_a, **_kw: [
                    _FakeIssue(
                        "NRS-1",
                        [
                            _FakeAtt("100", ""),
                            _FakeAtt("200", "noname"),
                            _FakeAtt("300", "   "),
                            _FakeAtt("400", "real-file.png"),
                        ],
                    ),
                ],
            )

    mig = AttachmentsMigration(jira_client=_BatchJira(), op_client=DummyOp())  # type: ignore[arg-type]
    result = mig._extract_batch(["NRS-1"])
    items = result.get("NRS-1", [])
    # All four attachments processed; empty/noname/whitespace get
    # derived names, real-file passes through.
    assert len(items) == 4
    by_id = {it["id"]: it["filename"] for it in items}
    assert by_id["100"] == "jira-attachment-100"
    assert by_id["200"] == "jira-attachment-200"
    assert by_id["300"] == "jira-attachment-300"
    assert by_id["400"] == "real-file.png"


def test_extract_batch_skips_when_no_id_and_no_filename(
    monkeypatch: pytest.MonkeyPatch,
):
    """Without ``aid`` AND without filename, there's no stable name to
    derive — skip rather than write an unnameable row.

    Pin: the ``aid is None`` defensive skip catches the impossible-
    in-practice case where Jira returns an attachment with neither
    id nor filename. We don't want to fabricate a filename like
    ``jira-attachment-None``.
    """
    from types import SimpleNamespace

    class _FakeAtt:
        def __init__(self) -> None:
            self.id = None
            self.filename = ""
            self.size = 100
            self.content = "http://jira/secure/attachment/x/blob"

    class _FakeFields:
        def __init__(self, atts: list[_FakeAtt]) -> None:
            self.attachment = atts

    class _FakeIssue:
        def __init__(self) -> None:
            self.key = "NRS-1"
            self.fields = _FakeFields([_FakeAtt()])

    class _BatchJira:
        def __init__(self) -> None:
            self.jira = SimpleNamespace(
                search_issues=lambda *_a, **_kw: [_FakeIssue()],
            )

    mig = AttachmentsMigration(jira_client=_BatchJira(), op_client=DummyOp())  # type: ignore[arg-type]
    result = mig._extract_batch(["NRS-1"])
    # The single attachment is skipped because there's no id to derive
    # a filename from; the issue key drops out entirely.
    assert result == {}


# --- filename fidelity (added 2026-05-07) ---


def test_load_rails_script_includes_update_columns_filename_guard():
    """The generated Rails script MUST include the byte-exact filename
    write so OP's silent normalisation (strip internal whitespace)
    doesn't corrupt the stored filename.

    Caught by the live 2026-05-07 NRS audit: ~31 of 163 "missing"
    files were present in OP under a sanitised name (e.g.
    ``Screenshot 2026-04-21 122931.png`` → ``Screenshot2026-04-21 122931.png``).
    The user-visible ``filename`` column is a plain string in OP's
    schema; ``update_columns`` skips callbacks/validations and writes
    the byte-exact value — restoring fidelity without touching the
    on-disk storage path.
    """
    op = DummyOp()
    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    # Run the load on a small payload so we can inspect the script.
    att_data = {"PRJ-1": [{"id": "1", "filename": "x.txt", "size": 1, "url": "http://example/x"}]}
    ex = ComponentResult(success=True, data={"attachments": att_data})
    mp = mig._map(ex)
    mig._load(mp)
    # Inspect the Rails script the dummy received via execute_script_with_data.
    # The dummy stores its calls; pull the script content.
    # ``transfers`` is set by transfer_file_to_container; the script is what
    # _load passes to execute_script_with_data. The dummy doesn't store the
    # script directly, so re-derive: the script source is computed in _load
    # from a static template — assert against the running migration's
    # method by re-invoking the same code path via inspecting it.
    # Instead: find the Rails string in the source — pin the substring.
    import inspect

    from src.application.components.attachments_migration import AttachmentsMigration as _AM

    src = inspect.getsource(_AM._load)
    assert "att.update_columns(filename: fname)" in src, (
        "Rails script must include the byte-exact filename guard "
        "(att.update_columns) to prevent OP's silent filename"
        " normalisation"
    )
    # Idempotency: only update if AR's setter mutated the value.
    assert "att.filename != fname" in src, (
        "update_columns must be conditional on a mismatch — otherwise"
        " every save triggers a redundant UPDATE"
    )
