import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    import pybreaker  # type: ignore  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - fallback for tests
    class _CircuitBreaker:  # pragma: no cover - minimal behavior
        def __init__(self, *args, **kwargs) -> None:
            return None

        def call(self, func, *args, **kwargs):
            return func(*args, **kwargs)

        def close(self) -> None:
            return None

        def open(self) -> None:
            return None

        def half_open(self) -> None:
            return None

        def __call__(self, func):
            return func

    pybreaker_stub = types.SimpleNamespace(
        CircuitBreakerError=Exception,
        CircuitBreaker=_CircuitBreaker,
    )
    sys.modules["pybreaker"] = pybreaker_stub

try:
    from pydantic import BaseModel as _PydanticBaseModel  # type: ignore  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - fallback for tests
    class _FieldInfo:
        def __init__(self, *, default=None, default_factory=None) -> None:
            self.default = default
            self.default_factory = default_factory

    def Field(*, default=None, default_factory=None):  # type: ignore
        return _FieldInfo(default=default, default_factory=default_factory)

    class BaseModel:
        __annotations__: dict[str, object] = {}

        def __init__(self, **kwargs) -> None:
            annotations = getattr(self.__class__, "__annotations__", {})
            for name in annotations:
                attr = getattr(self.__class__, name, None)
                if isinstance(attr, _FieldInfo):
                    if attr.default_factory is not None:
                        value = attr.default_factory()
                    else:
                        value = attr.default
                else:
                    value = attr
                setattr(self, name, value)
            for key, value in kwargs.items():
                setattr(self, key, value)

        def dict(self) -> dict[str, object]:  # type: ignore[override]
            return self.__dict__.copy()

    sys.modules["pydantic"] = types.SimpleNamespace(BaseModel=BaseModel, Field=Field)

try:
    import structlog  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - fallback for tests
    class _StubLogger:
        def __getattr__(self, _name):
            def _noop(*_args, **_kwargs):
                return None
            return _noop

    class _StdlibNS(types.SimpleNamespace):
        def PositionalArgumentsFormatter(self):
            return lambda *args, **kwargs: None

        def LoggerFactory(self):
            return object

        BoundLogger = type("BoundLogger", (), {})

    class _ProcessorsNS(types.SimpleNamespace):
        def TimeStamper(self, *args, **kwargs):
            return lambda *a, **kw: None

        def StackInfoRenderer(self):
            return lambda *a, **kw: None

        def format_exc_info(self, *args, **kwargs):
            return None

        def UnicodeDecoder(self):
            return lambda *a, **kw: None

        def JSONRenderer(self):
            return lambda *a, **kw: None

    stdlib_ns = _StdlibNS(
        filter_by_level=lambda *a, **kw: None,
        add_logger_name=lambda *a, **kw: None,
        add_log_level=lambda *a, **kw: None,
    )
    processors_ns = _ProcessorsNS()

    structlog = types.SimpleNamespace(
        get_logger=lambda *args, **kwargs: _StubLogger(),
        configure=lambda *args, **kwargs: None,
        stdlib=stdlib_ns,
        processors=processors_ns,
    )
    sys.modules["structlog"] = structlog

try:
    import sqlalchemy  # type: ignore  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - fallback for tests
    DeclarativeMeta = type("DeclarativeMeta", (type,), {})

    def _declarative_base():
        metadata = types.SimpleNamespace(create_all=lambda *_args, **_kwargs: None)
        return type("Base", (), {"metadata": metadata})

    def _sessionmaker(**_kwargs):
        def _factory():
            return types.SimpleNamespace(commit=lambda: None, close=lambda: None)

        return _factory

    sqlalchemy_stub = types.SimpleNamespace(
        Column=lambda *args, **kwargs: None,
        DateTime=lambda *args, **kwargs: None,
        Integer=lambda *args, **kwargs: None,
        String=lambda *args, **kwargs: None,
        Text=lambda *args, **kwargs: None,
        create_engine=lambda *args, **kwargs: None,
        ext=types.SimpleNamespace(declarative=types.SimpleNamespace(declarative_base=_declarative_base)),
        orm=types.SimpleNamespace(DeclarativeMeta=DeclarativeMeta, sessionmaker=_sessionmaker),
    )
    sys.modules["sqlalchemy"] = sqlalchemy_stub
    sys.modules["sqlalchemy.ext"] = sqlalchemy_stub.ext
    sys.modules["sqlalchemy.ext.declarative"] = sqlalchemy_stub.ext.declarative
    sys.modules["sqlalchemy.orm"] = sqlalchemy_stub.orm

try:
    import tenacity  # type: ignore  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - fallback for tests
    def _pass_through(*_args, **_kwargs):
        def _decorator(func):
            return func
        return _decorator

    tenacity_stub = types.SimpleNamespace(
        retry=_pass_through,
        retry_if_exception_type=_pass_through,
        wait_exponential=_pass_through,
        stop_after_attempt=_pass_through,
        after_log=_pass_through,
        before_sleep_log=_pass_through,
    )
    sys.modules["tenacity"] = tenacity_stub

from src.migrations.attachment_provenance_migration import AttachmentProvenanceMigration
from src.migrations.attachments_migration import AttachmentsMigration

pytestmark = pytest.mark.integration


class DummyMappings:
    def __init__(self) -> None:
        self._mapping = {
            "work_package": {"KEY-1": {"openproject_id": 42}},
            "user": {"alice": {"openproject_id": 7}},
        }

    def get_mapping(self, name: str):
        return self._mapping.get(name, {})


def _make_issue() -> SimpleNamespace:
    attachment = SimpleNamespace(
        id="att-1",
        filename="note.txt",
        size=12,
        content="https://example.test/note.txt",
        author=SimpleNamespace(name="alice"),
        created="2024-01-01T00:00:00Z",
    )
    comment = SimpleNamespace(
        id="c-1",
        created="2024-01-02T12:30:00Z",
        author=SimpleNamespace(name="alice", displayName="Alice Doe", emailAddress="alice@example.test"),
        body="Initial upload",
    )
    fields = SimpleNamespace(
        attachment=[attachment],
        comment=SimpleNamespace(comments=[comment]),
    )
    return SimpleNamespace(key="KEY-1", fields=fields)


@pytest.fixture
def patched_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from src import config as global_config

    attachment_dir = tmp_path / "attachments"
    attachment_dir.mkdir(parents=True, exist_ok=True)

    dummy = DummyMappings()
    monkeypatch.setattr(global_config, "mappings", dummy, raising=False)
    monkeypatch.setattr(global_config, "get_mappings", lambda: dummy, raising=False)
    monkeypatch.setattr(global_config, "migration_config", {"attachment_path": attachment_dir.as_posix()}, raising=False)
    monkeypatch.setattr(global_config, "get_path", lambda _name: tmp_path, raising=False)
    return attachment_dir


def test_attachment_provenance_pipeline(monkeypatch: pytest.MonkeyPatch, patched_config: Path) -> None:
    issue = _make_issue()
    jira_client = MagicMock()
    jira_client.batch_get_issues.return_value = {"KEY-1": issue}

    op_client = MagicMock()
    op_client.execute_script_with_data.side_effect = [
        {"updated": 1, "failed": 0},
        {"updated": 1, "failed": 0},
    ]

    def fake_download(self, url: str, dest_path: Path) -> Path:
        dest_path.write_bytes(b"stub")
        return dest_path

    monkeypatch.setattr(AttachmentsMigration, "_download_attachment", fake_download)

    attachments_migration = AttachmentsMigration(jira_client=jira_client, op_client=op_client)
    provenance_migration = AttachmentProvenanceMigration(jira_client=jira_client, op_client=op_client)

    attachments_result = attachments_migration.run()
    provenance_result = provenance_migration.run()

    assert attachments_result.success is True
    assert provenance_result.success is True

    op_client.transfer_file_to_container.assert_called_once()
    assert op_client.execute_script_with_data.call_count == 2

    _, provenance_call = op_client.execute_script_with_data.call_args_list
    payload = provenance_call.args[1]
    assert len(payload) == 1
    assert payload[0]["filename"] == "note.txt"
    assert payload[0]["author_id"] == 7
