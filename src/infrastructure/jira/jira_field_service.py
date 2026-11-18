# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Jira field-metadata queries.

Phase 3j of ADR-002 continues the jira_client.py decomposition. The
field-metadata methods (custom-field listing, per-field createmeta
lookup, and issue-property reads) move into a focused service.

The service is exposed on ``JiraClient`` as ``self.fields`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like the other Phase 3 services this is HTTP-only — calls
go through the ``jira`` SDK or the client's ``_make_request`` helper
— so there is no Ruby-script escaping to worry about.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from src.infrastructure.jira.jira_client import (
    HTTP_OK,
    JiraApiError,
    JiraClient,
    JiraConnectionError,
    JiraResourceNotFoundError,
)
from src.infrastructure.jira.jira_issue_service import JiraIssueService


class JiraFieldService:
    """Field-metadata and issue-property queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_issue_property(self, issue_key: str, property_key: str) -> dict[str, Any] | None:
        """Get an issue property JSON by key. Returns None if missing or on 404.

        This supports add-ons like Simple Tasklists that store data in properties.
        """
        from urllib.parse import quote

        path = f"/rest/api/2/issue/{quote(issue_key, safe='')}/properties/{quote(property_key, safe='')}"
        try:
            response = self._client._make_request(path)
        except JiraResourceNotFoundError:
            return None
        except JiraApiError as exc:
            if JiraIssueService._extract_http_status(exc) == HTTPStatus.NOT_FOUND:
                return None
            raise
        data = response.json()
        if not isinstance(data, dict):
            msg = "Expected an object in the Jira issue property response."
            raise JiraApiError(msg)
        return data.get("value", data)

    def _createmeta_fields(self, project_key: str | None, issue_type: dict[str, Any]) -> dict[str, Any]:
        client = self._client
        issue_type_id = issue_type.get("id")
        # Use the createmeta endpoint with expansion for fields (query params form)
        # API shape (v2): /rest/api/2/issue/createmeta?
        #   projectKeys=KEY&issuetypeIds=ID&expand=projects.issuetypes.fields
        params = {
            "projectKeys": project_key,
            "issuetypeIds": issue_type_id,
            "expand": "projects.issuetypes.fields",
        }
        path = "/rest/api/2/issue/createmeta"
        self._logger.debug(
            "Trying createmeta for project=%s issuetype=%s (%s)",
            project_key,
            issue_type_id,
            issue_type.get("name"),
        )

        try:
            meta_response = client._make_request(path, params=params)
        except JiraApiError as e:
            self._logger.debug("createmeta request failed: %s", e)
            return {}

        if not meta_response or meta_response.status_code != HTTP_OK:
            return {}

        meta_data = meta_response.json() or {}
        projects_arr = meta_data.get("projects") or []
        if not projects_arr:
            return {}
        issuetypes_arr = (projects_arr[0] or {}).get("issuetypes") or []
        if not issuetypes_arr:
            return {}
        fields_map = (issuetypes_arr[0] or {}).get("fields") or {}
        if not isinstance(fields_map, dict):
            return {}

        return fields_map

    def _get_field_metadata_via_createmeta(self, field_id: str) -> dict[str, Any]:
        """Get field metadata using the issue/createmeta endpoint.

        Args:
            field_id: The ID of the custom field to retrieve metadata for

        Returns:
            Dictionary containing field metadata

        Raises:
            JiraResourceNotFoundError: If the field is not found
            JiraApiError: If the API request fails

        """
        client = self._client
        self._logger.debug(
            "Attempting to get field metadata for %s using createmeta endpoint",
            field_id,
        )

        try:
            # We no longer need to check the cache here as it's done in the calling method

            # Get a list of projects
            projects = client.project_cache or client.get_projects()
            if not projects:
                msg = "No projects found to retrieve field metadata"
                raise JiraApiError(msg)

            # Get a list of issue types
            issue_types = client.issue_type_cache or client.get_issue_types()
            if not issue_types:
                msg = "No issue types found to retrieve field metadata"
                raise JiraApiError(msg)

            # Try with each project/issue type combination until we find field metadata
            for project in projects:
                project_key = project.get("key")
                for issue_type in issue_types:
                    fields_map = self._createmeta_fields(project_key, issue_type)
                    # Cache all discovered fields
                    client.field_options_cache.update(fields_map)

                    # If this is the field we're looking for, return it immediately
                    if field_id in fields_map:
                        return fields_map[field_id]

            # If we've checked all project/issue type combinations and still haven't found it
            field_data = client.field_options_cache.get(field_id)
            if field_data:
                return field_data

            msg = f"Field {field_id} not found in any project/issue type combination"
            raise JiraResourceNotFoundError(msg)

        except JiraResourceNotFoundError:
            raise  # Re-raise specific exceptions
        except Exception as e:
            error_msg = f"Error getting field metadata via createmeta endpoint: {e!s}"
            self._logger.warning(error_msg)
            raise JiraApiError(error_msg) from e

    def get_field_metadata(self, field_id: str) -> dict[str, Any]:
        """Get field metadata for a specific custom field.

        Args:
            field_id: The Jira custom field ID (e.g., 'customfield_10001')

        Returns:
            Dict containing field metadata

        Raises:
            JiraClientError: If the field cannot be retrieved

        """
        return self._get_field_metadata_via_createmeta(field_id)

    def get_custom_fields(self) -> list[dict[str, Any]]:
        """Get all custom fields from Jira.

        Returns:
            List of custom field dictionaries

        Raises:
            JiraConnectionError: If the Jira client isn't initialized.
            JiraApiError: If the API request fails.

        """
        client = self._client
        # Match the early-check pattern other service methods use.
        # Without this, ``client.jira.fields()`` would raise
        # ``AttributeError`` on a None client and get re-wrapped as
        # ``JiraApiError`` by the catch-all below.
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Use the fields endpoint to get all fields, then filter for custom fields
            response = client.jira.fields()

            # Filter for custom fields (custom fields typically start with 'customfield_')
            custom_fields = [field for field in response if field.get("id", "").startswith("customfield_")]

            self._logger.debug("Retrieved %d custom fields from Jira", len(custom_fields))
            return custom_fields

        except Exception as e:
            error_msg = f"Failed to retrieve custom fields: {e}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e
