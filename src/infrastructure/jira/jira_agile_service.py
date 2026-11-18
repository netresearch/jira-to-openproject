# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Jira Software (Agile) board and sprint queries.

Phase 3c of ADR-002 continues the jira_client.py decomposition. The
agile-related methods (board listing, board configuration, board
sprints) move into a focused service.

The service is exposed on ``JiraClient`` as ``self.agile`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like ``JiraProjectService`` and ``JiraWorkflowService`` this
is HTTP-only — calls go through the ``jira`` SDK session — so there is
no Ruby-script escaping to worry about.
"""

from __future__ import annotations

import re
from typing import Any

from requests import exceptions

from src.infrastructure.jira import CLIENT_NOT_INITIALIZED
from src.infrastructure.jira.jira_client import (
    HTTP_BAD_REQUEST_MIN,
    JiraApiError,
    JiraClient,
    JiraConnectionError,
)


class JiraAgileService:
    """Jira Software (Agile) queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_boards(self) -> list[dict[str, Any]]:
        """Return Jira Software boards (Scrum/Kanban)."""
        return self._paged_values("board")

    def get_boards_for_project(self, project_key: str) -> list[dict[str, Any]]:
        """Return boards Jira associates with this project, including shared boards."""
        return self._paged_values("board", projectKeyOrId=project_key)

    def get_board_projects(self, board_id: int) -> list[dict[str, Any]]:
        """Return projects covered by a Server/DC board's filter."""
        return self._paged_values(f"board/{board_id}/project")

    def get_board_issue_keys(self, board_id: int, project_key: str) -> list[str]:
        """Read just this project's issue keys from the board's permission-filtered membership."""
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project_key, re.ASCII):
            msg = "Invalid Jira project key for board snapshot"
            raise ValueError(msg)
        client = self._client
        if not client.jira:
            raise JiraConnectionError(CLIENT_NOT_INITIALIZED)
        url = f"{client.base_url}/rest/agile/1.0/board/{int(board_id)}/issue"
        keys: list[str] = []
        total: int | None = None
        while total is None or len(keys) < total:
            params = {
                "jql": f'project = "{project_key}" ORDER BY id',
                "fields": "project",
                "startAt": len(keys),
                "maxResults": 50,
            }
            response = client.jira._session.get(url, params=params)
            response.raise_for_status()
            page = response.json()
            if page["startAt"] != len(keys) or (total is not None and total != page["total"]):
                msg = "Board membership changed while paging"
                raise JiraApiError(msg)
            total = int(page["total"])
            for issue in page["issues"]:
                if issue["fields"]["project"]["key"] != project_key or not issue["key"].startswith(project_key + "-"):
                    msg = "Board returned an issue outside the selected project"
                    raise JiraApiError(msg)
                keys.append(issue["key"])
            if not page["issues"]:
                break
        if len(keys) != total or len(set(keys)) != len(keys):
            msg = "Incomplete or duplicate Jira board issue keys"
            raise JiraApiError(msg)
        return keys

    def _paged_values(self, resource: str, **scope: str) -> list[dict[str, Any]]:
        client = self._client
        if not client.jira:
            msg = CLIENT_NOT_INITIALIZED
            raise JiraConnectionError(msg)
        url = f"{client.base_url}/rest/agile/1.0/{resource}"
        items: list[dict[str, Any]] = []
        try:
            while True:
                response = client.jira._session.get(url, params={**scope, "startAt": len(items), "maxResults": 50})
                response.raise_for_status()
                page = response.json()
                values = page["values"]
                if not isinstance(values, list):
                    msg = f"Expected a page of Jira {resource} values"
                    raise TypeError(msg)
                if page.get("startAt", len(items)) != len(items):
                    msg = f"Unexpected page offset for Jira {resource}"
                    raise JiraApiError(msg)
                if not values and page.get("isLast") is False:
                    msg = f"Incomplete Jira {resource} inventory"
                    raise JiraApiError(msg)
                items.extend(values)
                if page.get("isLast") or not values:
                    return items
        except Exception as exc:
            msg = f"Failed to fetch Jira {resource}: {exc}"
            raise JiraApiError(msg) from exc

    def get_board_configuration(self, board_id: int) -> dict[str, Any]:
        """Return configuration details for a Jira Software board."""
        client = self._client
        if not client.jira:
            msg = CLIENT_NOT_INITIALIZED
            raise JiraConnectionError(msg)

        url = f"{client.base_url}/rest/agile/1.0/board/{board_id}/configuration"
        self._logger.debug("Fetching board configuration for %s", board_id)

        try:
            response = client.jira._session.get(url)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                msg = "Unexpected board configuration payload"
                raise ValueError(msg)
            return payload
        except Exception as exc:
            error_msg = f"Failed to fetch Jira board configuration ({board_id}): {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc

    def _sprint_page(self, url: str, params: dict[str, Any]) -> Any:
        try:
            response = self._client.jira._session.get(url, params=params)
            response.raise_for_status()
        except JiraApiError as exc:
            if "doesn't support sprints" in str(exc):
                return None
            raise
        except exceptions.HTTPError as exc:
            response = exc.response
            if (
                response is not None
                and response.status_code == HTTP_BAD_REQUEST_MIN
                and "doesn't support sprints" in (response.text or "")
            ):
                return None
            raise
        return response.json() or {}

    def get_board_sprints(self, board_id: int) -> list[dict[str, Any]]:
        """Return sprints associated with a Jira Software board."""
        client = self._client
        if not client.jira:
            msg = CLIENT_NOT_INITIALIZED
            raise JiraConnectionError(msg)

        url = f"{client.base_url}/rest/agile/1.0/board/{board_id}/sprint"
        self._logger.debug("Fetching sprints for board %s", board_id)

        try:
            start_at = 0
            max_results = 50
            sprints: list[dict[str, Any]] = []

            while True:
                params = {
                    "startAt": start_at,
                    "maxResults": max_results,
                    "state": "future,active,closed",
                }
                payload = self._sprint_page(url, params)
                if payload is None:
                    self._logger.debug("Board %s does not support sprints; skipping sprint extraction", board_id)
                    return []
                values = payload.get("values") if isinstance(payload, dict) else None
                batch = values if isinstance(values, list) else []
                sprints.extend(batch)

                is_last = payload.get("isLast", False) if isinstance(payload, dict) else True
                if is_last or not batch:
                    break
                start_at += len(batch)

            self._logger.debug("Board %s has %s sprints", board_id, len(sprints))
            return sprints
        except Exception as exc:
            error_msg = f"Failed to fetch sprints for board {board_id}: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc
