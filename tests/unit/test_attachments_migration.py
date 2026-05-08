import os
import re
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
        self.queries: list[str] = []
        # ``Setting.attachment_max_size`` value (in KB) seen by the
        # dummy. Tests can override before calling ``run()``.
        self.attachment_max_kb: int = 5120

    def transfer_file_to_container(self, local_path: Path, container_path: str):
        self.transfers.append((local_path, container_path))

    def execute_script_with_data(self, script_content: str, data: object):
        # Marker-fenced ``Setting.attachment_max_size`` script — extract the
        # write target (if any) so the dummy mirrors a real Rails read/write
        # round-trip via the envelope.
        if "Setting.attachment_max_size" in script_content:
            m = re.search(
                r"Setting\.attachment_max_size\s*=\s*(\d+)",
                script_content,
            )
            if m:
                self.attachment_max_kb = int(m.group(1))
            self.queries.append(script_content)
            return {
                "status": "success",
                "message": "ok",
                "data": {"value": self.attachment_max_kb},
                "output": "<dummy>",
            }
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
        "update_columns must be conditional on a mismatch — otherwise every save triggers a redundant UPDATE"
    )


# --- per-stage loss counters (added 2026-05-07) ---


def test_extract_batch_increments_extract_no_url_when_url_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """Attachment with no download URL → counted under
    ``extract_no_url`` (not silently dropped without trace).

    Live 2026-05-07 NRS regression: NRS-3630 had 9 sequential JPGs
    silently missing with no log clue. The per-stage counters tell
    operators *which* stage drops files instead of leaving them to
    grep through 90k log lines.
    """
    from types import SimpleNamespace

    class _Att:
        def __init__(self, id_: str, filename: str, url: str | None) -> None:
            self.id = id_
            self.filename = filename
            self.size = 100
            self.content = url

    class _FakeIssue:
        def __init__(self, atts: list[_Att]) -> None:
            self.key = "NRS-1"
            self.fields = SimpleNamespace(attachment=atts)

    class _Jira:
        def __init__(self) -> None:
            self.jira = SimpleNamespace(
                search_issues=lambda *_a, **_kw: [
                    _FakeIssue(
                        [
                            _Att("100", "good.txt", "http://jira/a"),
                            _Att("200", "no-url.txt", None),
                            _Att("300", "empty-url.txt", ""),
                        ],
                    ),
                ],
            )

    mig = AttachmentsMigration(jira_client=_Jira(), op_client=DummyOp())  # type: ignore[arg-type]
    mig._extract_batch(["NRS-1"])
    assert mig._loss_counters["extract_no_url"] == 2, dict(mig._loss_counters)


def test_run_resets_loss_counters_at_start(monkeypatch: pytest.MonkeyPatch):
    """``run()`` must clear cumulative counters from any prior
    invocation on the same instance.

    Pin: when ``attachment_recovery_migration`` constructs an
    ``AttachmentsMigration`` and delegates per-batch work, the
    inner instance's counters reflect THAT recovery's run, not
    leftover state from an earlier call.
    """
    import src.config as cfg

    class _EmptyMappings:
        def get_mapping(self, name):
            return {}

    monkeypatch.setattr(cfg, "mappings", _EmptyMappings(), raising=False)

    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    # Seed pre-existing counters as if from a previous invocation.
    mig._loss_counters["extract_no_url"] = 99
    mig._loss_counters["load_transfer_failed"] = 7
    # ``run`` fail-louds on the empty WP map, but
    # ``self._loss_counters.clear()`` happens BEFORE that exit so
    # the pre-seeded buckets are gone regardless of exit path.
    try:
        mig.run()
    except Exception:
        pass
    assert mig._loss_counters.get("extract_no_url", 0) == 0
    assert mig._loss_counters.get("load_transfer_failed", 0) == 0


def test_load_logs_sample_when_rails_returns_per_op_errors(
    caplog: pytest.LogCaptureFixture,
):
    """When Rails returns a non-empty ``errors`` array, ``_load`` must
    log a sample so operators can see WHY ops failed — not just count.

    Live 2026-05-07 NRS regression: a re-run reported
    ``load_rails_per_op_error: 39`` with no error text in the log,
    leaving the underlying Rails failure invisible. The fix logs the
    first 5 error objects.
    """
    import logging

    class _ErrOp(DummyOp):
        def execute_script_with_data(self, script_content: str, data: object):
            self.last_input = list(data) if isinstance(data, list) else []
            return {
                "status": "success",
                "message": "ok",
                "data": {
                    "results": [],
                    "errors": [
                        {"jira_key": "PRJ-1", "filename": "a.txt", "error": "boom-1"},
                        {"jira_key": "PRJ-1", "filename": "b.txt", "error": "boom-2"},
                    ],
                },
                "output": "<dummy>",
            }

    op = _ErrOp()
    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    att_data = {
        "PRJ-1": [
            {"id": "1", "filename": "a.txt", "size": 1, "url": "http://example/a"},
            {"id": "2", "filename": "b.txt", "size": 1, "url": "http://example/b"},
        ],
    }
    ex = ComponentResult(success=True, data={"attachments": att_data})
    mp = mig._map(ex)
    # ``AttachmentsMigration`` logs via ``from src.config import logger``,
    # which is the stdlib ``"migration"`` logger configured in
    # ``src/config``. The previous attempt to gate capture by the
    # module-path name was a no-op (the explicit ``logger=`` arg
    # only sets the level on the named logger; capture itself flows
    # through the root logger's handler) — caught by PR #212 review.
    with caplog.at_level(logging.WARNING, logger="migration"):
        mig._load(mp)
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "boom-1" in joined and "boom-2" in joined, joined
    assert mig._loss_counters["load_rails_per_op_error"] == 2


# --- attachment_max_size pre-flight (added 2026-05-07) ---


def test_run_raises_op_attachment_max_size_then_restores(
    monkeypatch: pytest.MonkeyPatch,
):
    """``run()`` must bump ``Setting.attachment_max_size`` if the
    target value exceeds the current OP cap, then restore the
    original cap after the per-project loop completes.

    Live 2026-05-07 NRS regression: 34 silent attachment losses
    traced to OP's default 5 MB ``attachment_max_size``. Without
    this pre-flight, every Jira attachment >5 MB hits
    ``Validation failed: File is too large`` and the file is lost.
    """
    monkeypatch.setenv("J2O_ATTACHMENT_MAX_KB", "1048576")  # 1 GiB

    op = DummyOp()
    op.attachment_max_kb = 5120  # OP default
    jira = DummyJira()
    mig = AttachmentsMigration(jira_client=jira, op_client=op)  # type: ignore[arg-type]

    # Make the per-project loop a no-op so we just exercise the
    # bump/restore wrap.
    monkeypatch.setattr(
        mig,
        "_run_per_project_loop",
        lambda **_kw: ComponentResult(success=True, updated=0, failed=0),
    )

    mig.run()

    # Verify the cap was raised and restored — the dummy stores the
    # latest write, so after ``run()`` it must be the original value.
    assert op.attachment_max_kb == 5120, "cap not restored"
    # Three envelope round-trips: read original, bump write, restore write.
    setting_calls = [q for q in op.queries if "Setting.attachment_max_size" in q]
    assert len(setting_calls) == 3, setting_calls
    writes = [q for q in setting_calls if re.search(r"Setting\.attachment_max_size\s*=", q)]
    assert any("1048576" in w for w in writes), setting_calls
    assert any("= 5120" in w for w in writes), setting_calls


def test_run_skips_bump_when_cap_already_high_enough(
    monkeypatch: pytest.MonkeyPatch,
):
    """If OP's existing cap already meets the target, ``run()`` must
    not write anything — avoiding pointless Rails round-trips and
    keeping the operator's intentional cap intact.
    """
    monkeypatch.setenv("J2O_ATTACHMENT_MAX_KB", "1048576")

    op = DummyOp()
    op.attachment_max_kb = 2_000_000  # already higher than target
    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    monkeypatch.setattr(
        mig,
        "_run_per_project_loop",
        lambda **_kw: ComponentResult(success=True, updated=0, failed=0),
    )

    mig.run()

    # Read happened once; no writes — only the read script is in queries.
    setting_calls = [q for q in op.queries if "Setting.attachment_max_size" in q]
    writes = [q for q in setting_calls if re.search(r"Setting\.attachment_max_size\s*=", q)]
    assert writes == [], setting_calls
    assert op.attachment_max_kb == 2_000_000


# --- duplicate filename disambiguation (added 2026-05-07) ---


def test_disambiguate_filenames_in_group_no_duplicates_returns_input():
    """No collisions → identity transform.

    Pin: the disambiguator must not silently rename files when there's
    nothing to fix.
    """
    out = AttachmentsMigration.disambiguate_duplicate_filenames_in_group(
        ["a.png", "b.png", "c.txt"],
    )
    assert out == ["a.png", "b.png", "c.txt"]


def test_disambiguate_filenames_in_group_basic_duplicates():
    """Repeated filename → ``" (N)"`` suffix before the extension.

    Live 2026-05-07 NRS regression: dozens of WPs each carried multiple
    Jira attachments sharing a name (e.g. ``logobottom.gif`` x2). The
    Rails idempotency check (``LOWER(filename) = ?``) kept the first
    and silently dropped the rest.
    """
    out = AttachmentsMigration.disambiguate_duplicate_filenames_in_group(
        ["a.png", "a.png", "a.png"],
    )
    assert out == ["a.png", "a (2).png", "a (3).png"]


def test_disambiguate_filenames_in_group_case_insensitive():
    """Disambiguation must use case-insensitive comparison — Rails's
    idempotency check uses ``LOWER(filename)``.
    """
    out = AttachmentsMigration.disambiguate_duplicate_filenames_in_group(
        ["IMG.PNG", "img.png", "Img.PNG"],
    )
    # The first wins its raw form; subsequent collisions get suffixes
    # in the input's original casing.
    assert out[0] == "IMG.PNG"
    assert "img (2).png" in (n.lower() for n in out)
    assert "img (3).png" in (n.lower() for n in out)


def test_disambiguate_filenames_in_group_no_extension():
    """Names without an extension still get a suffix appended.

    The ``rpartition('.')`` fallback puts the suffix at the end so the
    bare ``doc`` becomes ``doc (2)``.
    """
    out = AttachmentsMigration.disambiguate_duplicate_filenames_in_group(["doc", "doc"])
    assert out == ["doc", "doc (2)"]


def test_disambiguate_filenames_in_group_avoids_existing_suffix_collision():
    """If the natural suffix is already taken, fall through to the
    next available counter.

    Edge case: input ``["a.png", "a (2).png", "a.png"]`` — the second
    is already disambiguated, so the third must skip ``(2)`` and use
    ``(3)``.
    """
    out = AttachmentsMigration.disambiguate_duplicate_filenames_in_group(
        ["a.png", "a (2).png", "a.png"],
    )
    assert out[0] == "a.png"
    assert out[1] == "a (2).png"
    assert out[2] == "a (3).png", out


def test_double_encode_slashes_returns_none_when_nothing_to_change():
    """No ``%2F`` / ``%2C`` in URL → no transform — caller short-circuits."""
    assert AttachmentsMigration._double_encode_slashes("https://j/secure/attachment/1/foo.png") is None


def test_double_encode_slashes_replaces_2f_and_2c():
    """``%2F`` → ``%252F``, ``%2C`` → ``%252C``.

    Live 2026-05-08 NRS regression: NRS-3127's
    ``[EXTERNAL] ... 70227341// , INVDE196205.msg`` URL contained
    ``%2F%2F%2C`` in the path. Tomcat rejects literal encoded
    slashes; double-encoding bypasses that check.
    """
    src = "https://j/secure/attachment/92933/foo%2F%2F%2Cbar.msg"
    assert AttachmentsMigration._double_encode_slashes(src) == (
        "https://j/secure/attachment/92933/foo%252F%252F%252Cbar.msg"
    )


def test_double_encode_slashes_preserves_other_percent_encoded_chars():
    """The fallback only swaps ``%2F`` and ``%2C`` — other
    percent-encoded characters in the URL must be left untouched
    (e.g. ``%5B`` for ``[``, ``%20`` for space). Per PR #217 review:
    the URL under test has no actual query string, so the previous
    name "query_chars" was misleading — this is about preserving
    other percent-encodings *anywhere* in the URL.
    """
    src = "https://j/x/%5BEXTERNAL%5D+foo%2Fbar%2Cbaz%20qux.msg"
    out = AttachmentsMigration._double_encode_slashes(src)
    assert out == "https://j/x/%5BEXTERNAL%5D+foo%252Fbar%252Cbaz%20qux.msg", out


def test_disambiguate_per_wp_only(monkeypatch: pytest.MonkeyPatch):
    """``_disambiguate_duplicate_filenames`` only renames within the
    same ``work_package_id``. Two ops with the same filename for
    different WPs both keep their original names — Rails's
    idempotency is per-WP, so cross-WP "collisions" aren't real.
    """
    op = DummyOp()
    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    ops = [
        {"work_package_id": 1, "filename": "a.png", "jira_key": "K-1", "container_path": "/x1"},
        {"work_package_id": 2, "filename": "a.png", "jira_key": "K-2", "container_path": "/x2"},
        {"work_package_id": 1, "filename": "a.png", "jira_key": "K-1", "container_path": "/x3"},
    ]
    out = mig._disambiguate_duplicate_filenames(ops)
    names = [o["filename"] for o in out]
    assert names == ["a.png", "a.png", "a (2).png"], names


# --- Regression tests for PR #224 fixes (added 2026-05-08) ---


def test_load_treats_non_dict_data_as_failure_not_silent_pass(tmp_path: Path):
    """When Rails returns ``data`` that isn't a dict (e.g. a string
    or null after a malformed runner output), ``_load`` must:

    * count the whole batch as failed (`failed == len(container_ops)`),
    * report `success=False`,
    * increment ``load_rails_malformed_data`` by ``len(container_ops)``.

    Prior to PR #224 the path coerced to ``[]`` and silently
    reported a green batch — that hid real Rails-side breakage from
    the operator. Per PR #224 review.
    """

    class _MalformedDataOp(DummyOp):
        def execute_script_with_data(self, script_content: str, data: object):
            self.last_input = list(data) if isinstance(data, list) else []
            return {
                "status": "success",
                "message": "ok",
                "data": "not-a-dict",  # malformed
                "output": "<dummy>",
            }

    op = _MalformedDataOp()
    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    att_data = {
        "PRJ-1": [
            {"id": "1", "filename": "a.txt", "size": 1, "url": "http://example/a"},
            {"id": "2", "filename": "b.txt", "size": 1, "url": "http://example/b"},
        ],
    }
    ex = ComponentResult(success=True, data={"attachments": att_data})
    mp = mig._map(ex)
    result = mig._load(mp)
    assert result.success is False
    assert result.failed == 2  # both ops counted as failed
    assert mig._loss_counters["load_rails_malformed_data"] == 2


def test_load_treats_non_list_results_as_failure_not_silent_pass(tmp_path: Path):
    """``data['results']`` / ``data['errors']`` of a wrong type
    (dict / string / None) must also count the batch as failed
    instead of silently passing. Per PR #224 review.
    """

    class _MalformedListsOp(DummyOp):
        def execute_script_with_data(self, script_content: str, data: object):
            self.last_input = list(data) if isinstance(data, list) else []
            return {
                "status": "success",
                "message": "ok",
                "data": {"results": "should-be-a-list", "errors": None},
                "output": "<dummy>",
            }

    op = _MalformedListsOp()
    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    att_data = {
        "PRJ-1": [{"id": "1", "filename": "a.txt", "size": 1, "url": "http://example/a"}],
    }
    ex = ComponentResult(success=True, data={"attachments": att_data})
    mp = mig._map(ex)
    result = mig._load(mp)
    assert result.success is False
    assert result.failed == 1
    assert mig._loss_counters["load_rails_malformed_data"] == 1


def test_prepare_op_uses_attachment_id_to_disambiguate_local_path(tmp_path: Path):
    """Two Jira attachments sharing a ``filename`` but with distinct
    ``id`` values must produce distinct ``local_path`` values. Without
    the per-id prefix the second download would overwrite the first
    on disk before ``_load`` could transfer them, attaching the wrong
    bytes. Per PR #224 review.
    """
    op = DummyOp()
    mig = AttachmentsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    mig.attachment_dir = tmp_path

    item_a = {"id": "100", "filename": "image.png", "size": 1, "url": "http://example/a"}
    item_b = {"id": "200", "filename": "image.png", "size": 1, "url": "http://example/b"}

    op_a = mig._prepare_op_from_item(item=item_a, jira_key="K-1", work_package_id=1)
    op_b = mig._prepare_op_from_item(item=item_b, jira_key="K-2", work_package_id=2)
    assert op_a is not None and op_b is not None
    assert op_a["local_path"] != op_b["local_path"], (op_a["local_path"], op_b["local_path"])
    # The user-visible filename stays the original — only the local
    # path differs. The Rails attach script writes the filename
    # column from the op's ``filename`` field, not the path.
    assert op_a["filename"] == "image.png"
    assert op_b["filename"] == "image.png"
    # Each path carries the source id as a prefix.
    assert "/100_" in op_a["local_path"], op_a["local_path"]
    assert "/200_" in op_b["local_path"], op_b["local_path"]
