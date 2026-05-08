"""Workflow-detail endpoints (transitions / statuses) must treat unfetchable cases quietly.

The Jira Server REST endpoints
``/rest/api/2/workflow/<name>/transitions`` and
``/rest/api/2/workflow/<name>`` are not part of the public Jira REST API
in many Server/DC versions and consistently return HTTP 404. The caller
in ``workflow_migration.py`` already swallows the exception and uses an
empty list, so logging at ERROR with a stack trace per workflow (55 such
records on the live NRS run) is pure noise.

Root cause (404 case): the ``jira`` library's ``ResilientSession.raise_on_error``
raises ``JIRAError(status_code=404)`` *before* returning any response
object, so the existing ``if getattr(response, "status_code", None) == 404``
guard is dead code that never executes. The raised ``JIRAError`` falls
straight into the ``except Exception as exc`` handler which calls
``self._logger.exception(...)`` — printing a full traceback at ERROR
level for each of the 55 workflows.

Fix (production-complete for 404): In production ``JiraClient._patch_jira_client``
wraps the ``jira`` SDK session so that *all* exceptions (including
``JIRAError``) are re-raised as ``JiraApiError`` with the original
``JIRAError`` stored as ``exc.__cause__``. Therefore the ``isinstance(exc,
JIRAError)`` check is always ``False`` in production. The fix must walk the
``__cause__`` chain so that a ``JiraApiError`` whose cause is a
``JIRAError(status_code=404)`` is also treated silently.

Additional case (Tomcat encoded-slash 400): workflow names that contain a
literal ``/`` character (e.g. ``"NREDIT: Blog/CS Workflow"``) are
percent-encoded as ``%2F`` by ``urllib.parse.quote``.  Apache Tomcat rejects
such encoded slashes in URL path segments by default (``ALLOW_ENCODED_SLASH``
is ``false`` unless explicitly changed) and returns HTTP 400 with an HTML
body containing "Invalid URI: [The encoded slash character is not allowed]".
This is structurally unfetchable — no retry can work around it.  It is
treated identically to a 404: log at DEBUG, return ``[]``.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.infrastructure.jira.jira_workflow_service import JiraWorkflowService

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_service_returning(status_code: int = 200, json_body: Any | None = None) -> JiraWorkflowService:
    """Session returns a normal response object (200 / 404 via response attribute)."""
    fake_response = MagicMock()
    fake_response.status_code = status_code
    fake_response.json.return_value = json_body if json_body is not None else {}

    def _raise_for_status() -> None:
        if status_code >= 400:
            from requests.exceptions import HTTPError

            raise HTTPError(f"{status_code} Error", response=fake_response)

    fake_response.raise_for_status.side_effect = _raise_for_status

    fake_session = MagicMock()
    fake_session.get.return_value = fake_response

    fake_jira = MagicMock()
    fake_jira._session = fake_session

    fake_client = MagicMock()
    fake_client.jira = fake_jira
    fake_client.base_url = "https://jira.example.com"

    return JiraWorkflowService(fake_client)


def _make_service_raising_jira_error(status_code: int, text: str = "Not Found") -> JiraWorkflowService:
    """Session raises JIRAError directly — bare path (no production wrapping)."""
    from jira.exceptions import JIRAError

    fake_session = MagicMock()
    fake_session.get.side_effect = JIRAError(
        text=text,
        status_code=status_code,
        url="https://jira.example.com/rest/api/2/workflow/Some%20Workflow",
    )

    fake_jira = MagicMock()
    fake_jira._session = fake_session

    fake_client = MagicMock()
    fake_client.jira = fake_jira
    fake_client.base_url = "https://jira.example.com"

    return JiraWorkflowService(fake_client)


def _make_service_raising_jira_error_400_encoded_slash(
    workflow_name: str = "NREDIT: Blog/CS Workflow",
) -> JiraWorkflowService:
    """Session raises JIRAError(400) with Tomcat encoded-slash message — bare path."""
    from jira.exceptions import JIRAError

    tomcat_body = (
        "<!doctype html><h1>HTTP Status 400 – Bad Request</h1>"
        "Message: Invalid URI: [The encoded slash character is not allowed]"
        "Apache Tomcat/9.0.85"
    )
    fake_session = MagicMock()
    fake_session.get.side_effect = JIRAError(
        text=tomcat_body,
        status_code=400,
        url=f"https://jira.example.com/rest/api/2/workflow/{workflow_name}/transitions",
    )

    fake_jira = MagicMock()
    fake_jira._session = fake_session

    fake_client = MagicMock()
    fake_client.jira = fake_jira
    fake_client.base_url = "https://jira.example.com"

    return JiraWorkflowService(fake_client)


def _make_service_raising_wrapped_jira_error(status_code: int, text: str = "Not Found") -> JiraWorkflowService:
    """Session raises JiraApiError whose __cause__ is a JIRAError.

    This simulates the PRODUCTION path: ``JiraClient._patch_jira_client``
    installs a ``patched_request`` shim that catches *all* exceptions and
    re-raises them as ``JiraApiError(msg) from original_exc``.  So the
    ``JIRAError`` raised by the ``jira`` SDK's ``ResilientSession`` never
    reaches the service directly — instead the service sees a ``JiraApiError``
    with the original ``JIRAError`` stored as ``__cause__``.
    """
    from jira.exceptions import JIRAError

    from src.infrastructure.jira.jira_client import JiraApiError

    cause = JIRAError(
        text=text,
        status_code=status_code,
        url="https://jira.example.com/rest/api/2/workflow/Some%20Workflow",
    )
    wrapper = JiraApiError(f"Error during API request: {cause!s}")
    wrapper.__cause__ = cause

    fake_session = MagicMock()
    fake_session.get.side_effect = wrapper

    fake_jira = MagicMock()
    fake_jira._session = fake_session

    fake_client = MagicMock()
    fake_client.jira = fake_jira
    fake_client.base_url = "https://jira.example.com"

    return JiraWorkflowService(fake_client)


_TOMCAT_400_TEXT = (
    "<!doctype html><h1>HTTP Status 400 – Bad Request</h1>"
    "Message: Invalid URI: [The encoded slash character is not allowed]"
    "Apache Tomcat/9.0.85"
)


def _make_service_raising_wrapped_jira_error_400_encoded_slash() -> JiraWorkflowService:
    """Production-path version: JiraApiError wrapping JIRAError(400) with Tomcat body."""
    return _make_service_raising_wrapped_jira_error(status_code=400, text=_TOMCAT_400_TEXT)


# ---------------------------------------------------------------------------
# Tests: JIRAError raised by ResilientSession (the real production path)
# ---------------------------------------------------------------------------


def test_get_workflow_transitions_no_error_log_when_jira_raises_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JIRAError(status_code=404) must be handled quietly — no ERROR log, returns []."""
    service = _make_service_raising_jira_error(status_code=404)
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_transitions("Sales + Accounting: Customer Epic")
    assert result == [], "expected empty list on 404"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log entries: {error_records}"


def test_get_workflow_statuses_no_error_log_when_jira_raises_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JIRAError(status_code=404) must be handled quietly — no ERROR log, returns []."""
    service = _make_service_raising_jira_error(status_code=404)
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_statuses("DXP: Management tasks workflow")
    assert result == [], "expected empty list on 404"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log entries: {error_records}"


def test_get_workflow_transitions_raises_on_jira_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JIRAError(status_code=500) must still raise JiraApiError so real failures surface."""
    from src.infrastructure.jira.jira_client import JiraApiError

    service = _make_service_raising_jira_error(status_code=500)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(JiraApiError):
            service.get_workflow_transitions("X")


def test_get_workflow_statuses_raises_on_jira_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JIRAError(status_code=500) must still raise JiraApiError so real failures surface."""
    from src.infrastructure.jira.jira_client import JiraApiError

    service = _make_service_raising_jira_error(status_code=500)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(JiraApiError):
            service.get_workflow_statuses("X")


# ---------------------------------------------------------------------------
# Tests: production path — JiraApiError wrapping a JIRAError(status_code=404)
#
# In production JiraClient._patch_jira_client wraps the SDK session so every
# exception (including JIRAError) is re-raised as JiraApiError with the
# original JIRAError stored as __cause__.  The isinstance(exc, JIRAError)
# check is therefore always False in production.  _is_workflow_404 must walk
# the __cause__ chain to detect this case.
# ---------------------------------------------------------------------------


def test_get_workflow_transitions_no_error_log_when_wrapped_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JiraApiError wrapping JIRAError(404) must be handled quietly — no ERROR log, returns []."""
    service = _make_service_raising_wrapped_jira_error(status_code=404)
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_transitions("Sales + Accounting: Customer Epic")
    assert result == [], "expected empty list on production-wrapped 404"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log entries: {error_records}"


def test_get_workflow_statuses_no_error_log_when_wrapped_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JiraApiError wrapping JIRAError(404) must be handled quietly — no ERROR log, returns []."""
    service = _make_service_raising_wrapped_jira_error(status_code=404)
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_statuses("DXP: Management tasks workflow")
    assert result == [], "expected empty list on production-wrapped 404"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log entries: {error_records}"


def test_get_workflow_transitions_raises_on_wrapped_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JiraApiError wrapping JIRAError(500) must still propagate — real failures must surface."""
    from src.infrastructure.jira.jira_client import JiraApiError

    service = _make_service_raising_wrapped_jira_error(status_code=500)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(JiraApiError):
            service.get_workflow_transitions("X")


def test_get_workflow_statuses_raises_on_wrapped_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JiraApiError wrapping JIRAError(500) must still propagate — real failures must surface."""
    from src.infrastructure.jira.jira_client import JiraApiError

    service = _make_service_raising_wrapped_jira_error(status_code=500)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(JiraApiError):
            service.get_workflow_statuses("X")


# ---------------------------------------------------------------------------
# Tests: Tomcat encoded-slash 400 — bare JIRAError path
# ---------------------------------------------------------------------------


def test_get_workflow_transitions_no_error_log_when_jira_raises_400_encoded_slash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JIRAError(400) with Tomcat encoded-slash body must be silenced at DEBUG, returns []."""
    service = _make_service_raising_jira_error_400_encoded_slash("NREDIT: Blog/CS Workflow")
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_transitions("NREDIT: Blog/CS Workflow")
    assert result == [], "expected empty list on Tomcat 400 encoded-slash"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log entries: {error_records}"


def test_get_workflow_statuses_no_error_log_when_jira_raises_400_encoded_slash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JIRAError(400) with Tomcat encoded-slash body must be silenced at DEBUG, returns []."""
    service = _make_service_raising_jira_error_400_encoded_slash("NREDIT: Blog/CS Workflow")
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_statuses("NREDIT: Blog/CS Workflow")
    assert result == [], "expected empty list on Tomcat 400 encoded-slash"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log entries: {error_records}"


def test_get_workflow_transitions_400_encoded_slash_logs_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The encoded-slash 400 suppression must emit a DEBUG message (not silence entirely)."""
    service = _make_service_raising_jira_error_400_encoded_slash("NREDIT: Blog/CS Workflow")
    with caplog.at_level(logging.DEBUG):
        service.get_workflow_transitions("NREDIT: Blog/CS Workflow")
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "unfetchable" in r.message.lower() or "encoded" in r.message.lower() or "400" in r.message
        for r in debug_records
    ), f"Expected a DEBUG log mentioning unfetchable/encoded/400, got: {[r.message for r in debug_records]}"


# ---------------------------------------------------------------------------
# Tests: Tomcat encoded-slash 400 — production-wrapped path
# ---------------------------------------------------------------------------


def test_get_workflow_transitions_no_error_log_when_wrapped_400_encoded_slash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JiraApiError wrapping JIRAError(400)+encoded-slash must be silenced at DEBUG, returns []."""
    service = _make_service_raising_wrapped_jira_error_400_encoded_slash()
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_transitions("NREDIT: Blog/CS Workflow")
    assert result == [], "expected empty list on production-wrapped 400 encoded-slash"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log entries: {error_records}"


def test_get_workflow_statuses_no_error_log_when_wrapped_400_encoded_slash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JiraApiError wrapping JIRAError(400)+encoded-slash must be silenced at DEBUG, returns []."""
    service = _make_service_raising_wrapped_jira_error_400_encoded_slash()
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_statuses("NREDIT: Blog/CS Workflow")
    assert result == [], "expected empty list on production-wrapped 400 encoded-slash"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log entries: {error_records}"


# ---------------------------------------------------------------------------
# Tests: 400 without encoded-slash body must NOT be silenced (negative regression)
# ---------------------------------------------------------------------------


def test_get_workflow_transitions_raises_on_jira_400_other_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JIRAError(400) with a generic body (not encoded-slash) must still raise at ERROR."""
    from src.infrastructure.jira.jira_client import JiraApiError

    # Generic 400 — bad request for some other reason
    service = _make_service_raising_jira_error(status_code=400, text="Bad Request: malformed query")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(JiraApiError):
            service.get_workflow_transitions("SomeWorkflow")


def test_get_workflow_statuses_raises_on_jira_400_other_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JIRAError(400) with a generic body (not encoded-slash) must still raise at ERROR."""
    from src.infrastructure.jira.jira_client import JiraApiError

    service = _make_service_raising_jira_error(status_code=400, text="Bad Request: malformed query")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(JiraApiError):
            service.get_workflow_statuses("SomeWorkflow")


# ---------------------------------------------------------------------------
# Tests: original response-object path (kept for regression coverage)
# ---------------------------------------------------------------------------


def test_get_workflow_transitions_returns_empty_on_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _make_service_returning(status_code=404)
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_transitions("Sales: Customer Epic")
    assert result == []
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log: {error_records}"


def test_get_workflow_statuses_returns_empty_on_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _make_service_returning(status_code=404)
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_statuses("Sales: Customer Epic")
    assert result == []
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log: {error_records}"


def test_get_workflow_transitions_still_raises_on_other_errors() -> None:
    """Non-404 errors should still surface as JiraApiError."""
    from src.infrastructure.jira.jira_client import JiraApiError

    service = _make_service_returning(status_code=500)
    with pytest.raises(JiraApiError):
        service.get_workflow_transitions("X")


def test_get_workflow_transitions_returns_data_on_success() -> None:
    """Sanity: a 200 with proper payload returns the transitions list."""
    transitions = [{"id": "1", "name": "To Do → In Progress"}]
    service = _make_service_returning(status_code=200, json_body={"transitions": transitions})
    assert service.get_workflow_transitions("X") == transitions
