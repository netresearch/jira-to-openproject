"""Jira reporting (filter and dashboard) queries.

Phase 3h of ADR-002 continues the jira_client.py decomposition. The
filter and dashboard related methods (filter listing with favourites
fallback, dashboard listing, dashboard details lookup) move into a
focused service.

The service is exposed on ``JiraClient`` as ``self.reporting`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like the other Phase 3 services this is HTTP-only — calls
go through the ``jira`` SDK session — so there is no Ruby-script
escaping to worry about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from requests import exceptions

from src.clients.jira_client import (
    JiraApiError,
    JiraConnectionError,
)

if TYPE_CHECKING:
    from jira.exceptions import JIRAError as AtlassianJIRAError

    from src.clients.jira_client import JiraClient
else:
    # At runtime, avoid importing jira to prevent stub issues
    AtlassianJIRAError = Exception  # type: ignore[misc,assignment]


class JiraReportingService:
    """Filter and dashboard queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.clients.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.clients.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_filters(self) -> list[dict[str, Any]]:
        """Return Jira filters visible to the authenticated user."""
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        self._logger.info("Fetching Jira filters")
        filters: list[dict[str, Any]] = []

        try:

            def _fetch_favourites() -> list[dict[str, Any]]:
                fav_resp = self._client.jira._session.get(
                    f"{self._client.base_url}/rest/api/2/filter/favourite",
                )
                fav_resp.raise_for_status()
                fav_payload = fav_resp.json()
                return fav_payload if isinstance(fav_payload, list) else []

            def _extract_status_from_error(exc: BaseException | None) -> int | None:
                current: BaseException | None = exc
                while current:
                    if isinstance(current, AtlassianJIRAError):
                        status = getattr(current, "status_code", None)
                        if status is not None:
                            return int(status)
                        response = getattr(current, "response", None)
                        if response is not None:
                            status = getattr(response, "status_code", None)
                            if status is not None:
                                return int(status)
                    response = getattr(current, "response", None)
                    if response is not None:
                        status = getattr(response, "status_code", None)
                        if status is not None:
                            return int(status)
                    current = getattr(current, "__cause__", None)
                return None

            try:
                response = self._client.jira._session.get(
                    f"{self._client.base_url}/rest/api/2/filter/search",
                    params={"startAt": 0, "maxResults": 1000},
                )
            except JiraApiError as exc:
                status = _extract_status_from_error(exc) or _extract_status_from_error(exc.__cause__)
                if status in (404, 405):
                    self._logger.warning(
                        "Filter search endpoint (status %s) not available; falling back to favourites list",
                        status,
                    )
                    filters = _fetch_favourites()
                    self._logger.info("Retrieved %s Jira filters (favourites fallback)", len(filters))
                    return filters
                raise

            try:
                response.raise_for_status()
                payload = response.json()
                values = payload.get("values") if isinstance(payload, dict) else None
                filters = values if isinstance(values, list) else []
            except (
                exceptions.HTTPError,
                AtlassianJIRAError,
            ) as exc:
                status = None
                if isinstance(exc, exceptions.HTTPError):
                    status = getattr(exc.response, "status_code", None)
                else:
                    status = getattr(exc, "status_code", None) or getattr(
                        getattr(exc, "response", None),
                        "status_code",
                        None,
                    )

                if status in (404, 405):
                    self._logger.warning(
                        "Filter search endpoint (status %s) not available; falling back to favourites list",
                        status,
                    )
                    filters = _fetch_favourites()
                else:
                    raise
            except JiraApiError as exc:
                message = str(exc)
                if "HTTP 404" in message or "HTTP 405" in message:
                    self._logger.warning(
                        "Filter search endpoint not available (%s); falling back to favourites list",
                        message,
                    )
                    filters = _fetch_favourites()
                else:
                    raise

            self._logger.info("Retrieved %s Jira filters", len(filters))
            return filters
        except Exception as exc:
            error_msg = f"Failed to fetch Jira filters: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc

    def get_dashboards(self) -> list[dict[str, Any]]:
        """Return Jira dashboards visible to the authenticated user."""
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        url = f"{self._client.base_url}/rest/api/2/dashboard"
        self._logger.info("Fetching Jira dashboards")

        try:
            response = self._client.jira._session.get(url)
            response.raise_for_status()
            payload = response.json()
            values = payload.get("dashboards") if isinstance(payload, dict) else None
            dashboards = values if isinstance(values, list) else []
            self._logger.info("Retrieved %s Jira dashboards", len(dashboards))
            return dashboards
        except Exception as exc:
            error_msg = f"Failed to fetch Jira dashboards: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc

    def get_dashboard_details(self, dashboard_id: int) -> dict[str, Any]:
        """Return details for a specific Jira dashboard."""
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        url = f"{self._client.base_url}/rest/api/2/dashboard/{dashboard_id}"
        self._logger.debug("Fetching dashboard details for %s", dashboard_id)

        try:
            response = self._client.jira._session.get(url)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                msg = "Unexpected dashboard payload"
                raise ValueError(msg)
            return payload
        except Exception as exc:
            error_msg = f"Failed to fetch dashboard {dashboard_id}: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc
