"""Jira search and metadata queries.

Phase 3i of ADR-002 continues the jira_client.py decomposition. The
search-by-JQL count and the status / priority / status-category
metadata reads move into a focused service.

The service is exposed on ``JiraClient`` as ``self.search`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like the other Phase 3 services this is HTTP-only — calls
go through the ``jira`` SDK or the client's ``_make_request`` helper
— so there is no Ruby-script escaping to worry about.
"""

from __future__ import annotations

from typing import Any

from src.infrastructure.jira.jira_client import (
    HTTP_OK,
    JiraApiError,
    JiraClient,
    JiraConnectionError,
    JiraResourceNotFoundError,
)


class JiraSearchService:
    """Search-by-JQL and status/priority metadata queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_issue_count(self, project_key: str) -> int:
        """Get the total number of issues in a project.

        Args:
            project_key: The key of the Jira project to count issues from

        Returns:
            The total number of issues in the project

        Raises:
            JiraResourceNotFoundError: If the project is not found
            JiraApiError: If the API request fails

        """
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Use JQL to count issues in the project - surround with quotes to handle reserved words
            jql = f'project="{project_key}"'

            # Using fields="key" and expand='' minimizes data transfer
            issues = self._client.jira.search_issues(
                jql,
                maxResults=1,
                fields="key",
                expand="",
            )

            # Get total from the response
            return issues.total
        except Exception as e:
            error_msg = f"Failed to get issue count for project {project_key}: {e!s}"
            self._logger.exception(error_msg)
            if "project does not exist" in str(e).lower() or "project not found" in str(e).lower():
                msg = f"Project {project_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def get_all_statuses(self) -> list[dict[str, Any]]:
        """Get all statuses from Jira.

        Returns:
            List of status dictionaries with id, name, and category information

        Raises:
            JiraApiError: If the API request fails

        """
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Primary: ``/rest/api/2/status`` returns the FULL list of
            # statuses configured on the instance. The pre-extraction
            # code used this only as a fallback after sampling 50
            # recently-created issues — which silently dropped any
            # status not represented in that sample. Reverse the
            # priority: hit the canonical endpoint first, fall back
            # to sampling only when the endpoint is unavailable
            # (older self-hosted instances without /rest/api/2/status
            # exposed). Dropped the now-unused
            # ``/rest/api/2/statuscategory`` call that previously
            # only contributed to a debug log.
            try:
                response = self._client._make_request("/rest/api/2/status")
            except Exception as primary_err:
                self._logger.debug(
                    "Status endpoint unavailable (%s); falling back to sampled issues",
                    primary_err,
                )
                response = None

            if response is not None and response.status_code == HTTP_OK:
                statuses_payload = response.json()
                self._logger.info(
                    "Retrieved %s statuses from Jira /rest/api/2/status",
                    len(statuses_payload),
                )
                return statuses_payload

            # Fallback: extract statuses from a sample of 50 most-recent
            # issues. Best-effort — a status that no current issue uses
            # won't appear here.
            statuses: list[dict[str, Any]] = []
            issues = self._client.jira.search_issues("order by created DESC", maxResults=50)
            self._logger.debug("Retrieving statuses from %s sample issues", len(issues))

            for issue in issues:
                if hasattr(issue.fields, "status"):
                    status = issue.fields.status
                    category = getattr(status, "statusCategory", None)
                    status_dict = {
                        "id": status.id,
                        "name": status.name,
                        "description": getattr(status, "description", ""),
                        "statusCategory": {
                            "id": getattr(category, "id", None),
                            "key": getattr(category, "key", None),
                            "name": getattr(category, "name", None),
                            "colorName": getattr(category, "colorName", None),
                        },
                    }
                    if not any(s.get("id") == status.id for s in statuses):
                        statuses.append(status_dict)

            self._logger.info(
                "Retrieved %s statuses from sample issues (fallback)",
                len(statuses),
            )
            return statuses

        except Exception as e:
            error_msg = f"Failed to get statuses: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_priorities(self) -> list[dict[str, Any]]:
        """Get all priorities from Jira.

        Returns:
            List of priority dictionaries with id, name, and status

        Raises:
            JiraApiError: If the API request fails

        """
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # jira client has priorities() helper in many versions
            priorities = self._client.jira.priorities()
            result = [
                {
                    "id": getattr(p, "id", None),
                    "name": getattr(p, "name", None),
                    "status": getattr(p, "status", None),
                }
                for p in priorities
            ]
            if not priorities:
                self._logger.warning("No priorities found in Jira")
            return result
        except Exception as e:
            error_msg = f"Failed to get priorities: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_status_categories(self) -> list[dict[str, Any]]:
        """Get all status categories from Jira.

        Returns:
            List of status category dictionaries

        Raises:
            JiraConnectionError: If the Jira client isn't initialized.
            JiraApiError: If the API request fails for any other reason.

        """
        # Match the early-check pattern other service methods use so
        # connection failures surface as ``JiraConnectionError`` with
        # the standard message instead of being wrapped as
        # ``JiraApiError`` by the catch-all below.
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Use the REST API to get all status categories
            path = "/rest/api/2/statuscategory"

            # Make the request using our generic method
            response = self._client._make_request(path)
            if not response:
                msg = "Failed to get status categories (request failed)"
                raise JiraApiError(msg)

            if response.status_code != HTTP_OK:
                msg = f"Failed to get status categories: HTTP {response.status_code}"
                raise JiraApiError(msg)

            categories = response.json()
            self._logger.info("Retrieved %s status categories from Jira API", len(categories))

            return categories
        except JiraConnectionError, JiraApiError:
            # Already the right shape — let it propagate without wrapping.
            raise
        except Exception as e:
            error_msg = f"Failed to get status categories: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e
