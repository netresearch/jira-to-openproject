"""Jira project queries.

Phase 3a of ADR-002 starts the jira_client.py decomposition. The
project-related methods (project list, issue types, roles, permission
schemes, enhanced metadata) move into a focused service.

The service is exposed on ``JiraClient`` as ``self.projects`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Unlike the OpenProject services this client is HTTP-only —
calls go through the ``jira`` SDK or ``JiraClient._make_request`` — so
there is no Ruby-script escaping to worry about.
"""

from __future__ import annotations

from typing import Any

from src.infrastructure.jira.jira_client import (
    HTTP_NOT_FOUND,
    HTTP_OK,
    JiraApiError,
    JiraAuthenticationError,
    JiraCaptchaError,
    JiraClient,
    JiraConnectionError,
)
from src.utils.performance_optimizer import cached


class JiraProjectService:
    """Project-domain queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_projects(self) -> list[dict[str, Any]]:
        """Get all projects from Jira with enriched metadata."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        def _sanitize_str(value: Any) -> str:
            if value is None:
                return ""
            return str(value)

        projects_details: list[dict[str, Any]] = []

        try:
            projects = client.jira.projects()
            if not projects:
                self._logger.warning("No projects found in Jira")
                return []

            for project in projects:
                detail = None
                try:
                    detail = client.jira.project(project.id)
                except Exception as exc:
                    self._logger.debug(
                        "Failed to fetch detailed project metadata for %s: %s",
                        project.key,
                        exc,
                    )

                raw_detail = getattr(detail, "raw", {}) or {}

                project_category = raw_detail.get("projectCategory") or {}
                if not isinstance(project_category, dict):
                    project_category = {}

                category_name = _sanitize_str(project_category.get("name")) if project_category else ""
                category_id = _sanitize_str(project_category.get("id")) if project_category else ""

                lead_info = raw_detail.get("lead") or {}
                if not isinstance(lead_info, dict):
                    lead_info = {}

                lead_login = lead_info.get("name") or lead_info.get("key")
                lead_display = lead_info.get("displayName")

                avatar_urls = raw_detail.get("avatarUrls") or {}
                if not isinstance(avatar_urls, dict):
                    avatar_urls = {}
                preferred_avatar_url = ""
                for size_key in ("128x128", "64x64", "48x48", "32x32", "24x24", "16x16"):
                    candidate = avatar_urls.get(size_key)
                    if candidate:
                        preferred_avatar_url = str(candidate)
                        break

                project_type = raw_detail.get("projectTypeKey") or getattr(project, "projectTypeKey", None)

                browse_url = f"{client.base_url}/browse/{project.key}"

                description = raw_detail.get("description") or ""
                if description is None:
                    description = ""

                projects_details.append(
                    {
                        "key": project.key,
                        "name": project.name,
                        "id": project.id,
                        "project_type_key": project_type,
                        "project_category": project_category,
                        "project_category_name": category_name,
                        "project_category_id": category_id,
                        "description": description,
                        "lead": lead_login,
                        "lead_display": lead_display,
                        "avatar_urls": avatar_urls,
                        "avatar_url": preferred_avatar_url,
                        "url": raw_detail.get("self"),
                        "browse_url": browse_url,
                        "archived": raw_detail.get("archived", False),
                    },
                )

            return projects_details
        except Exception as e:
            error_msg = f"Failed to get projects: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_issue_types(self) -> list[dict[str, Any]]:
        """Get all issue types from Jira.

        Returns:
            List of issue type dictionaries with id, name, and description

        Raises:
            JiraApiError: If the API request fails

        """
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            issue_types = client.jira.issue_types()
            result = [
                {
                    "id": issue_type.id,
                    "name": issue_type.name,
                    "description": issue_type.description,
                }
                for issue_type in issue_types
            ]

            if not issue_types:
                self._logger.warning("No issue types found in Jira")

            return result
        except Exception as e:
            error_msg = f"Failed to get issue types: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_project_roles(self, project_key: str) -> list[dict[str, Any]]:
        """Retrieve Jira project roles and their actors for a project."""
        if not project_key:
            return []

        client = self._client
        self._logger.debug("Fetching Jira project roles for '%s'", project_key)

        try:
            role_map_response = client._make_request(
                f"/rest/api/2/project/{project_key}/role",
            )
            if role_map_response.status_code != HTTP_OK:
                msg = f"Failed to fetch Jira project roles for {project_key}: HTTP {role_map_response.status_code}"
                raise JiraApiError(msg)

            role_map = role_map_response.json() or {}
            roles: list[dict[str, Any]] = []

            for role_name, role_url in role_map.items():
                if not isinstance(role_url, str):
                    continue

                detail_path = role_url
                if role_url.startswith(client.base_url):
                    detail_path = role_url[len(client.base_url) :]
                if not detail_path.startswith("/"):
                    detail_path = f"/{detail_path}"

                detail_response = client._make_request(detail_path)
                if detail_response.status_code != HTTP_OK:
                    self._logger.warning(
                        "Skipping Jira role '%s' for project '%s' due to HTTP %s",
                        role_name,
                        project_key,
                        detail_response.status_code,
                    )
                    continue

                detail = detail_response.json() or {}
                actors = []
                for actor in detail.get("actors", []):
                    actors.append(
                        {
                            "type": actor.get("type"),
                            "name": actor.get("name"),
                            "displayName": actor.get("displayName"),
                            "accountId": ((actor.get("actorUser") or {}).get("accountId") or actor.get("accountId")),
                            "userKey": ((actor.get("actorUser") or {}).get("key") or actor.get("userKey")),
                            "groupName": ((actor.get("actorGroup") or {}).get("name") or actor.get("groupName")),
                        },
                    )

                roles.append(
                    {
                        "id": detail.get("id"),
                        "name": detail.get("name") or role_name,
                        "description": detail.get("description"),
                        "actors": actors,
                    },
                )

            self._logger.debug(
                "Discovered %s Jira project roles for '%s'",
                len(roles),
                project_key,
            )
            return roles
        except JiraCaptchaError, JiraAuthenticationError, JiraConnectionError:
            raise
        except Exception as e:
            error_msg = f"Failed to fetch Jira project roles for {project_key}: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_project_permission_scheme(self, project_key: str) -> dict[str, Any]:
        """Return the permission scheme applied to a Jira project."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        path = f"{client.base_url}/rest/api/2/project/{project_key}/permissionscheme"
        self._logger.debug("Fetching Jira permission scheme for project '%s'", project_key)

        try:
            response = client.jira._session.get(path)
            if response.status_code == HTTP_NOT_FOUND:
                return {}
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            error_msg = f"Failed to fetch permission scheme for {project_key}: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc

    @cached(ttl=3600)  # Cache for 1 hour
    def get_project_metadata_enhanced(self, project_key: str) -> dict[str, Any]:
        """Get comprehensive project metadata with caching."""
        client = self._client
        try:
            project = client.jira.project(project_key)

            # Get additional metadata
            issue_types = client.jira.createmeta_issuetypes(project.key)
            statuses = client.jira.project_status(project.key)

            return {
                "project": {
                    "id": project.id,
                    "key": project.key,
                    "name": project.name,
                    "description": getattr(project, "description", ""),
                    "lead": (
                        getattr(project, "lead", {}).get("name", "Unknown") if hasattr(project, "lead") else "Unknown"
                    ),
                    "project_type_key": getattr(project, "projectTypeKey", "software"),
                },
                "issue_types": [
                    {
                        "id": it.id,
                        "name": it.name,
                        "description": getattr(it, "description", ""),
                        "subtask": getattr(it, "subtask", False),
                    }
                    for it in issue_types
                ],
                "statuses": [
                    {
                        "id": status.id,
                        "name": status.name,
                        "description": getattr(status, "description", ""),
                        "category": getattr(status, "statusCategory", {}).get(
                            "name",
                            "Unknown",
                        ),
                    }
                    for status in statuses
                ],
            }
        except Exception as e:
            error_msg = f"Failed to get enhanced project metadata for {project_key}: {e}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    # ── batch operations ─────────────────────────────────────────────────

    def batch_get_projects(self, project_keys: list[str]) -> dict[str, dict]:
        """Retrieve multiple projects in batches for optimal performance."""
        if not project_keys:
            return {}

        # Get all projects and filter to requested keys
        all_projects = self.get_projects()
        return {project["key"]: project for project in all_projects if project["key"] in project_keys}
