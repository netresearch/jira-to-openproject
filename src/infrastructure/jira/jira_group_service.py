"""Jira group directory queries.

Phase 3g of ADR-002 continues the jira_client.py decomposition. The
group-related methods (group listing via the picker endpoint, group
member lookup with pagination) move into a focused service.

The service is exposed on ``JiraClient`` as ``self.groups`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like the other Phase 3 services this is HTTP-only — calls
go through the client's ``_make_request`` helper — so there is no
Ruby-script escaping to worry about.
"""

from __future__ import annotations

from typing import Any

from src.infrastructure.jira.jira_client import (
    HTTP_OK,
    JiraApiError,
    JiraAuthenticationError,
    JiraCaptchaError,
    JiraClient,
    JiraConnectionError,
)


class JiraGroupService:
    """Group-domain queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_groups(self) -> list[dict[str, Any]]:
        """Retrieve all Jira groups visible to the migration user."""
        self._logger.info("Fetching Jira groups via groups picker endpoint")

        try:
            response = self._client._make_request(
                "/rest/api/2/groups/picker",
                params={
                    "query": "",
                    "maxResults": 1000,
                    "includeInactive": "true",
                },
            )
            if response.status_code != HTTP_OK:
                msg = f"Failed to fetch Jira groups: HTTP {response.status_code}"
                raise JiraApiError(msg)

            payload = response.json() or {}
            groups = payload.get("groups", [])
            self._logger.info("Retrieved %s Jira groups", len(groups))
            normalized: list[dict[str, Any]] = []
            for group in groups:
                normalized.append(
                    {
                        "name": group.get("name"),
                        "groupId": group.get("groupId"),
                        "html": group.get("html"),
                        "labels": group.get("labels", []),
                    },
                )
            return normalized
        except (
            JiraCaptchaError,
            JiraAuthenticationError,
            JiraConnectionError,
        ):
            raise
        except Exception as e:
            error_msg = f"Failed to get Jira groups: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_group_members(self, group_name: str) -> list[dict[str, Any]]:
        """Retrieve members for a Jira group, handling pagination."""
        if not group_name:
            return []

        members: list[dict[str, Any]] = []
        start_at = 0
        max_results = 100

        self._logger.debug("Fetching members for Jira group '%s'", group_name)

        try:
            while True:
                response = self._client._make_request(
                    "/rest/api/2/group/member",
                    params={
                        "groupname": group_name,
                        "includeInactiveUsers": "true",
                        "maxResults": max_results,
                        "startAt": start_at,
                    },
                )
                if response.status_code != HTTP_OK:
                    msg = f"Failed to fetch group members for {group_name}: HTTP {response.status_code}"
                    raise JiraApiError(msg)

                payload = response.json() or {}
                values = payload.get("values", [])
                for entry in values:
                    members.append(
                        {
                            "accountId": entry.get("accountId"),
                            "key": entry.get("key"),
                            "name": entry.get("name"),
                            "displayName": entry.get("displayName"),
                            "emailAddress": entry.get("emailAddress"),
                            "active": entry.get("active", True),
                        },
                    )

                start_at += len(values)
                is_last = payload.get("isLast")
                total = payload.get("total")
                if is_last or not values:
                    break
                if total is not None and start_at >= int(total):
                    break

            self._logger.debug(
                "Loaded %s members for Jira group '%s'",
                len(members),
                group_name,
            )
            return members
        except (
            JiraCaptchaError,
            JiraAuthenticationError,
            JiraConnectionError,
        ):
            raise
        except Exception as e:
            error_msg = f"Failed to get Jira group members for {group_name}: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e
