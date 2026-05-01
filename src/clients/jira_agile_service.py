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

from typing import TYPE_CHECKING, Any

from requests import exceptions

from src.clients.jira_client import (
    HTTP_BAD_REQUEST_MIN,
    JiraApiError,
    JiraConnectionError,
)

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient


class JiraAgileService:
    """Jira Software (Agile) queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.clients.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.clients.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_boards(self) -> list[dict[str, Any]]:
        """Return Jira Software boards (Scrum/Kanban)."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        url = f"{client.base_url}/rest/agile/1.0/board"
        self._logger.info("Fetching Jira boards")

        try:
            start_at = 0
            max_results = 50
            boards: list[dict[str, Any]] = []

            while True:
                params = {"startAt": start_at, "maxResults": max_results}
                response = client.jira._session.get(
                    url,
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
                values = payload.get("values") if isinstance(payload, dict) else None
                batch = values if isinstance(values, list) else []
                boards.extend(batch)

                is_last = payload.get("isLast", False) if isinstance(payload, dict) else True
                if is_last or not batch:
                    break
                start_at += max_results

            self._logger.info("Retrieved %s Jira boards", len(boards))
            return boards
        except Exception as exc:
            error_msg = f"Failed to fetch Jira boards: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc

    def get_board_configuration(self, board_id: int) -> dict[str, Any]:
        """Return configuration details for a Jira Software board."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
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

    def get_board_sprints(self, board_id: int) -> list[dict[str, Any]]:
        """Return sprints associated with a Jira Software board."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
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
                try:
                    response = client.jira._session.get(
                        url,
                        params=params,
                    )
                    response.raise_for_status()
                except JiraApiError as exc:
                    message = str(exc)
                    if "doesn't support sprints" in message:
                        self._logger.debug(
                            "Board %s does not support sprints; skipping sprint extraction",
                            board_id,
                        )
                        return []
                    raise
                except exceptions.HTTPError as exc:
                    status = getattr(exc.response, "status_code", None)
                    if status == HTTP_BAD_REQUEST_MIN and exc.response is not None:
                        text = exc.response.text or ""
                        if "doesn't support sprints" in text:
                            self._logger.debug(
                                "Board %s does not support sprints; skipping sprint extraction",
                                board_id,
                            )
                            return []
                    raise
                payload = response.json()
                values = payload.get("values") if isinstance(payload, dict) else None
                batch = values if isinstance(values, list) else []
                sprints.extend(batch)

                is_last = payload.get("isLast", False) if isinstance(payload, dict) else True
                if is_last or not batch:
                    break
                start_at += max_results

            self._logger.debug("Board %s has %s sprints", board_id, len(sprints))
            return sprints
        except Exception as exc:
            error_msg = f"Failed to fetch sprints for board {board_id}: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc
