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

from typing import TYPE_CHECKING, Any

from src.clients.jira_client import (
    HTTP_OK,
    JiraApiError,
    JiraConnectionError,
    JiraResourceNotFoundError,
)

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient


class JiraSearchService:
    """Search-by-JQL and status/priority metadata queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.clients.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.clients.jira_client import logger

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
            # Method 1: Use sample issues to extract statuses
            statuses: list[dict[str, Any]] = []
            issues = self._client.jira.search_issues("order by created DESC", maxResults=50)
            self._logger.debug("Retrieving statuses from %s sample issues", len(issues))

            # Extract unique statuses from these issues
            for issue in issues:
                if hasattr(issue.fields, "status"):
                    status = issue.fields.status
                    status_dict = {
                        "id": status.id,
                        "name": status.name,
                        "description": getattr(status, "description", ""),
                        "statusCategory": {
                            "id": getattr(
                                getattr(status, "statusCategory", None),
                                "id",
                                None,
                            ),
                            "key": getattr(
                                getattr(status, "statusCategory", None),
                                "key",
                                None,
                            ),
                            "name": getattr(
                                getattr(status, "statusCategory", None),
                                "name",
                                None,
                            ),
                            "colorName": getattr(
                                getattr(status, "statusCategory", None),
                                "colorName",
                                None,
                            ),
                        },
                    }

                    # Check if status is already in list
                    if not any(s.get("id") == status.id for s in statuses):
                        statuses.append(status_dict)

            # If we found statuses from sample issues, return them
            if statuses:
                self._logger.info("Retrieved %s statuses from sample issues", len(statuses))
                return statuses

            # Method 2: Use the status_categories endpoint
            path = "/rest/api/2/statuscategory"
            response = self._client._make_request(path)
            if not response or response.status_code != HTTP_OK:
                msg = f"Failed to get status categories: HTTP {response.status_code if response else 'No response'}"
                raise JiraApiError(
                    msg,
                )

            categories = response.json()
            self._logger.debug("Retrieved %s status categories from API", len(categories))

            # Use the status endpoint
            path = "/rest/api/2/status"
            response = self._client._make_request(path)
            if not response or response.status_code != HTTP_OK:
                msg = f"Failed to get statuses: HTTP {response.status_code if response else 'No response'}"
                raise JiraApiError(
                    msg,
                )

            statuses = response.json()
            self._logger.info("Retrieved %s statuses from Jira API", len(statuses))

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
            JiraApiError: If the API request fails

        """
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
        except Exception as e:
            error_msg = f"Failed to get status categories: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e
