"""Non-JSON Jira responses must surface as a typed signal, not a JSONDecodeError.

Issue #260 background
---------------------
A user ran ``--profile full`` against Jira 9.12.2 + OpenProject 17.4.0 and saw
the same opaque error fire from five different endpoints::

    Failed to retrieve Tempo customers: Expecting value: line 10 column 1 (char 9)
                                        JSONDecodeError: Expecting value: ...

The ``char 9`` / ``line 10`` signature is HTML (``<!DOCTYPE html>``…) — Jira
returned its HTML chrome where the caller expected JSON. Causes vary across
sites: missing plugin (Tempo not installed), reverse-proxy interception,
authentication that returns the login page at HTTP 200, or a CAPTCHA / WebSudo
challenge. From j2o's point of view the *cause* doesn't matter — the
*signal* should be uniform: this endpoint is unavailable, and Tempo-style
optional callers should skip cleanly rather than fail the whole component.

These tests pin the contract:
    1. ``_looks_like_html`` recognises HTML by Content-Type and by body sniff.
    2. ``_assert_json_response`` raises ``JiraServiceUnavailableError`` (a new
       typed exception) when a response isn't JSON.
    3. ``JiraTempoService`` callers (accounts, customers, work logs) treat the
       new exception as a soft "skip" — return ``[]`` with a WARNING — instead
       of raising a fatal ``JiraApiError``.
    4. ``JiraProjectService.get_project_roles`` does the same.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from src.infrastructure.jira.jira_client import (
    JiraClient,
    JiraServiceUnavailableError,
    _assert_json_response,
    _looks_like_html,
)
from src.infrastructure.jira.jira_tempo_service import JiraTempoService
from src.infrastructure.jira.jira_worklog_service import JiraWorklogService


def _resp(status_code: int = 200, *, content_type: str = "application/json", body: str = "[]") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.headers = {"Content-Type": content_type}
    r.text = body
    return r


# ---------------------------------------------------------------------------
# 1. Helpers
# ---------------------------------------------------------------------------


class TestLooksLikeHtml:
    def test_html_content_type_is_detected(self) -> None:
        assert _looks_like_html(_resp(content_type="text/html; charset=utf-8", body="ignored"))

    def test_html_body_with_wrong_content_type_is_detected(self) -> None:
        # Some misconfigured servers send text/plain but the body is HTML.
        assert _looks_like_html(_resp(content_type="text/plain", body="<!DOCTYPE html><html>...</html>"))

    def test_proper_json_is_not_html(self) -> None:
        assert not _looks_like_html(_resp(content_type="application/json", body='{"ok": true}'))

    def test_empty_body_is_not_html(self) -> None:
        assert not _looks_like_html(_resp(content_type="application/json", body=""))

    def test_xml_body_is_not_treated_as_html_when_content_type_says_json(self) -> None:
        # Edge case: a server *says* it's JSON, but body looks like XML/HTML.
        # We err on the side of detecting non-JSON — XML triggers _looks_like_html too,
        # because '<' indicates the body is not JSON regardless of which markup it is.
        assert _looks_like_html(_resp(content_type="application/json", body="<xml/>"))

    def test_works_with_case_insensitive_dict_headers(self) -> None:
        """Production responses carry ``requests.structures.CaseInsensitiveDict``,
        which is NOT a ``dict`` subclass. Must still trigger detection.
        """
        from requests.structures import CaseInsensitiveDict

        r = MagicMock()
        r.status_code = 200
        r.headers = CaseInsensitiveDict({"content-type": "text/html; charset=utf-8"})
        r.text = "<html></html>"
        assert _looks_like_html(r)

    def test_safe_with_non_mapping_headers(self) -> None:
        """A Mock or other object in place of headers must not crash detection."""
        r = MagicMock()
        r.status_code = 200
        r.headers = MagicMock()  # not a Mapping
        r.text = '{"ok": true}'
        # No exception; falls back to body sniff which says JSON.
        assert not _looks_like_html(r)


class TestAssertJsonResponse:
    def test_raises_on_html_response(self) -> None:
        with pytest.raises(JiraServiceUnavailableError) as excinfo:
            _assert_json_response(_resp(content_type="text/html", body="<html/>"), path="/rest/x/y")
        # The exception message must contain the path (so logs are useful)
        # and at least one of the canonical causes (so users know where to look).
        msg = str(excinfo.value)
        assert "/rest/x/y" in msg
        assert any(token in msg.lower() for token in ("plugin", "auth", "login", "proxy", "captcha"))

    def test_does_not_raise_on_json_response(self) -> None:
        _assert_json_response(_resp(content_type="application/json", body='{"ok": true}'), path="/rest/x/y")

    def test_does_not_raise_when_body_is_empty(self) -> None:
        _assert_json_response(_resp(content_type="application/json", body=""), path="/rest/x/y")


# ---------------------------------------------------------------------------
# 2. Tempo service callers must skip cleanly on non-JSON
# ---------------------------------------------------------------------------


@pytest.fixture
def tempo_client(monkeypatch: pytest.MonkeyPatch) -> tuple[object, MagicMock]:
    """Build a minimal JiraClient with a stubbed _make_request / _session."""
    import time

    from src.utils.rate_limiter import create_jira_rate_limiter

    client = JiraClient.__new__(JiraClient)
    client.jira = MagicMock()
    client.jira_url = "https://jira.local"
    client.base_url = "https://jira.local"
    client.rate_limiter = create_jira_rate_limiter()
    client.request_count = 0
    client.period_start = time.time()
    client.worklogs = JiraWorklogService(client)
    client.tempo = JiraTempoService(client)

    # Track responses returned by both code paths the service uses.
    session_response = MagicMock()
    client.jira._session.get = MagicMock(return_value=session_response)
    client._make_request = MagicMock(return_value=session_response)  # type: ignore[method-assign]

    return client, session_response


def _set_html_response(resp: MagicMock, *, status_code: int = 200) -> None:
    """Configure a MagicMock response to look like an HTML body."""
    resp.status_code = status_code
    resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    resp.text = "<!DOCTYPE html>\n<html><head><title>Log in</title></head>...</html>"
    # If anyone still calls .json(), it should raise the same way requests would.
    import json

    resp.json.side_effect = json.JSONDecodeError("Expecting value", "<!DOCTYPE html>", 9)


class TestTempoServiceSkipsOnHtml:
    def test_get_tempo_accounts_returns_empty_on_html(
        self,
        tempo_client: tuple[object, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, resp = tempo_client
        _set_html_response(resp)
        with caplog.at_level(logging.WARNING):
            result = client.tempo.get_tempo_accounts()
        assert result == []
        # A single, informative WARNING should explain the skip.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a WARNING-level log entry on Tempo unavailability"
        assert any("tempo" in r.getMessage().lower() for r in warnings)

    def test_get_tempo_customers_returns_empty_on_html(
        self,
        tempo_client: tuple[object, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, resp = tempo_client
        _set_html_response(resp)
        with caplog.at_level(logging.WARNING):
            result = client.tempo.get_tempo_customers()
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings

    def test_get_tempo_work_logs_returns_empty_on_html(
        self,
        tempo_client: tuple[object, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, resp = tempo_client
        _set_html_response(resp)
        with caplog.at_level(logging.WARNING):
            result = client.tempo.get_tempo_work_logs()
        assert result == []

    def test_get_tempo_accounts_returns_empty_on_404_html(
        self,
        tempo_client: tuple[object, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Tempo not installed → Jira may return 404 + HTML "page not found"."""
        client, resp = tempo_client
        _set_html_response(resp, status_code=404)
        with caplog.at_level(logging.WARNING):
            result = client.tempo.get_tempo_accounts()
        assert result == []


# ---------------------------------------------------------------------------
# 3. Project roles must also skip cleanly
# ---------------------------------------------------------------------------


class TestProjectRolesSkipsOnHtml:
    def test_get_project_roles_returns_empty_on_html(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from src.infrastructure.jira.jira_project_service import JiraProjectService

        fake_client = MagicMock()
        fake_client.base_url = "https://jira.local"
        # /rest/api/2/project/<KEY>/role returns HTML on this site
        resp = MagicMock()
        _set_html_response(resp)
        fake_client._make_request = MagicMock(return_value=resp)

        svc = JiraProjectService(fake_client)
        with caplog.at_level(logging.WARNING):
            roles = svc.get_project_roles("TK")

        assert roles == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a WARNING-level log entry on project-roles unavailability"
