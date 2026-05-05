"""Workflow-detail endpoints (transitions / statuses) must treat 404 quietly.

The Jira Server REST endpoints
``/rest/api/2/workflow/<name>/transitions`` and
``/rest/api/2/workflow/<name>`` are not part of the public Jira REST API
in many Server/DC versions and consistently return HTTP 404. The caller
in ``workflow_migration.py`` already swallows the exception and uses an
empty list, so logging at ERROR with a stack trace per workflow (55 such
records on the live NRS run) is pure noise.

Fix: short-circuit on 404 and return ``[]`` silently. Real (non-404)
errors still surface as warnings + JiraApiError so they're not buried.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.infrastructure.jira.jira_workflow_service import JiraWorkflowService


def _make_service(status_code: int = 404, json_body: Any | None = None) -> JiraWorkflowService:
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


def test_get_workflow_transitions_returns_empty_on_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _make_service(status_code=404)
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_transitions("Sales: Customer Epic")
    assert result == []
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log: {error_records}"


def test_get_workflow_statuses_returns_empty_on_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _make_service(status_code=404)
    with caplog.at_level(logging.DEBUG):
        result = service.get_workflow_statuses("Sales: Customer Epic")
    assert result == []
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Got unexpected ERROR log: {error_records}"


def test_get_workflow_transitions_still_raises_on_other_errors() -> None:
    """Non-404 errors should still surface as JiraApiError."""
    from src.infrastructure.jira.jira_client import JiraApiError

    service = _make_service(status_code=500)
    with pytest.raises(JiraApiError):
        service.get_workflow_transitions("X")


def test_get_workflow_transitions_returns_data_on_success() -> None:
    """Sanity: a 200 with proper payload returns the transitions list."""
    transitions = [{"id": "1", "name": "To Do → In Progress"}]
    service = _make_service(status_code=200, json_body={"transitions": transitions})
    assert service.get_workflow_transitions("X") == transitions
