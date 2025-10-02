"""Jira API client for the migration project.

Provides a clean, exception-based interface for Jira resource access.
Enhanced with performance optimizations including batch operations,
caching, and parallel processing.
"""

import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from requests import Response

from jira import Issue
from src import config
from src.display import configure_logging
from src.utils.config_validation import ConfigurationValidationError, SecurityValidator
from src.utils.performance_optimizer import (
    PerformanceOptimizer,
    StreamingPaginator,
    cached,
    rate_limited,
)
from src.utils.rate_limiter import create_jira_rate_limiter
from src.utils.timezone import UTC

HTTP_OK = 200
HTTP_BAD_REQUEST_MIN = 400
HTTP_NOT_FOUND = 404

try:
    from src.config import logger
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)


class JiraError(Exception):
    """Base exception for all Jira client errors."""


class JiraConnectionError(JiraError):
    """Error when connection to Jira server fails."""


class JiraAuthenticationError(JiraError):
    """Error when authentication to Jira fails."""


class JiraApiError(JiraError):
    """Error when Jira API returns an error response."""


class JiraResourceNotFoundError(JiraError):
    """Error when a requested Jira resource is not found."""


class JiraCaptchaError(JiraError):
    """Error when Jira requires CAPTCHA resolution."""


class JiraClient:
    """Jira client for API interactions.

    Provides a clean, exception-based interface for interacting with the Jira API,
    including project/issue operations and Tempo plugin integration.

    Instead of returning empty lists or None on failure, methods will raise appropriate
    exceptions that can be caught and handled by the caller.
    """

    def __init__(self, **kwargs: object) -> None:
        """Initialize the Jira client with proper exception handling and performance optimizations."""
        # Get connection details from config
        self.jira_url: str = config.jira_config.get("url", "")
        self.jira_username: str = config.jira_config.get("username", "")
        self.jira_token: str = config.jira_config.get("api_token", "")
        self.verify_ssl: bool = config.jira_config.get("verify_ssl", True)

        # Validate required configuration
        if not self.jira_url:
            msg = "Jira URL is required"
            raise ValueError(msg)
        if not self.jira_token:
            msg = "Jira API token is required"
            raise ValueError(msg)

        # ScriptRunner configuration
        self.scriptrunner_enabled = config.jira_config.get("scriptrunner", {}).get(
            "enabled",
            False,
        )
        self.scriptrunner_custom_field_options_endpoint = config.jira_config.get(
            "scriptrunner",
            {},
        ).get(
            "custom_field_options_endpoint",
            "",
        )

        # Initialize client
        self.jira: JIRA | None = None

        # Initialize rate limiter
        self.rate_limiter = create_jira_rate_limiter()
        self.request_count = 0
        self.period_start = time.time()
        self.base_url = self.jira_url.rstrip("/")

        # Cache fields
        self.project_cache: list[dict[str, Any]] | None = None
        self.issue_type_cache: list[dict[str, Any]] | None = None
        self.field_options_cache: dict[str, Any] = {}

        # ===== PERFORMANCE OPTIMIZER SETUP =====
        # Validate performance configuration parameters using SecurityValidator
        try:
            cache_size = SecurityValidator.validate_numeric_parameter(
                "cache_size",
                kwargs.get("cache_size", 2000),
            )
            cache_ttl = SecurityValidator.validate_numeric_parameter(
                "cache_ttl",
                kwargs.get("cache_ttl", 1800),
            )
            batch_size = SecurityValidator.validate_numeric_parameter(
                "batch_size",
                kwargs.get("batch_size", 100),
            )
            max_workers = SecurityValidator.validate_numeric_parameter(
                "max_workers",
                kwargs.get("max_workers", 15),
            )
            rate_limit = SecurityValidator.validate_numeric_parameter(
                "rate_limit_per_sec",
                kwargs.get("rate_limit", 15.0),
            )

            # Validate resource allocation to prevent system overload
            SecurityValidator.validate_resource_allocation(
                batch_size,
                max_workers,
                2048,
            )  # 2GB memory limit

        except ConfigurationValidationError:
            logger.exception("JiraClient configuration validation failed")
            raise

        # Initialize performance optimizer with validated parameters
        self.performance_optimizer = PerformanceOptimizer(
            cache_size=cache_size,
            cache_ttl=cache_ttl,
            batch_size=batch_size,
            max_workers=max_workers,
            rate_limit=rate_limit,
        )

        self.batch_size = batch_size
        self.parallel_workers = max_workers

        # Connect to Jira
        self._connect()
        self._patch_jira_client()

    def _connect(self) -> None:
        """Connect to the Jira API.

        Raises:
            JiraConnectionError: If connection to Jira server fails
            JiraAuthenticationError: If authentication fails

        """
        connection_errors = []

        # Helper: import real jira module even if a local test stub shadows it
        def _import_real_jira_module():
            try:
                import importlib, importlib.util, site, sys
                from pathlib import Path as _Path

                # If a shadow stub is loaded from the project, purge it
                if "jira" in sys.modules:
                    mod = sys.modules["jira"]
                    mod_file = getattr(mod, "__file__", "")
                    if mod_file and "/p/j2o/jira/__init__.py" in mod_file:
                        # Remove stub and any submodules to force a clean import
                        for key in list(sys.modules.keys()):
                            if key == "jira" or key.startswith("jira."):
                                sys.modules.pop(key, None)

                # Prefer virtualenv site-packages path
                candidates: list[_Path] = []
                try:
                    candidates.extend(_Path(p) for p in site.getsitepackages())
                except Exception:  # noqa: BLE001
                    pass
                try:
                    usp = site.getusersitepackages()
                    if usp:
                        candidates.append(_Path(usp))
                except Exception:  # noqa: BLE001
                    pass

                for base in candidates:
                    jira_init = base / "jira" / "__init__.py"
                    if jira_init.exists():
                        # Load the real package under the canonical name 'jira'
                        spec = importlib.util.spec_from_file_location("jira", str(jira_init))
                        if spec and spec.loader:
                            mod = importlib.util.module_from_spec(spec)
                            sys.modules["jira"] = mod  # ensure relative imports resolve to this package
                            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
                            if hasattr(mod, "JIRA"):
                                return mod

                # Fallback: regular import (may still hit stub if unresolved)
                return importlib.import_module("jira")
            except Exception:  # noqa: BLE001
                import importlib
                return importlib.import_module("jira")

        jira_mod = _import_real_jira_module()

        # Try to connect using token auth (Jira Cloud and Server PAT)
        try:
            logger.info("Attempting to connect to Jira using token authentication")
            self.jira = jira_mod.JIRA(server=self.jira_url, token_auth=self.jira_token)
            server_info = self.jira.server_info()
            logger.success(
                "Successfully connected to Jira server: %s (%s)",
                server_info.get("baseUrl"),
                server_info.get("version"),
            )
            # Explicit auth verification: /rest/api/2/myself must be 200 for valid PAT
            try:
                resp = self.jira._session.get(f"{self.base_url}/rest/api/2/myself")  # noqa: SLF001
                if resp.status_code != HTTP_OK:
                    logger.error(
                        "Auth verification failed on /myself: HTTP %s, headers=%s",
                        resp.status_code,
                        {k: v for k, v in resp.headers.items() if k.lower() in ("www-authenticate", "x-ausername")},
                    )
                    auth_msg = f"/myself auth check failed with HTTP {resp.status_code}"
                    raise JiraAuthenticationError(auth_msg)  # noqa: TRY301
                logger.info("Auth verification successful on /myself")
            except JiraAuthenticationError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("Auth verification error on /myself: %s", e)
            return  # noqa: TRY300
        except Exception as e:  # noqa: BLE001
            error_msg = f"Token authentication failed: {e!s}"
            logger.warning(error_msg)
            connection_errors.append(error_msg)

        # Try basic authentication
        try:
            self.jira = jira_mod.JIRA(
                server=self.jira_url,
                basic_auth=(self.jira_username, self.jira_token),
                options={"verify": self.verify_ssl},
            )
            self._patch_jira_client()
            logger.debug(
                "Successfully connected using basic authentication",
            )
            return  # noqa: TRY300
        except Exception as e2:  # noqa: BLE001
            error_msg = f"Basic authentication failed: {e2!s}"
            logger.warning(error_msg)
            connection_errors.append(error_msg)

        # If all methods failed, raise exception with details
        error_details = "; ".join(connection_errors)
        logger.error(
            "All authentication methods failed for Jira connection to %s",
            self.jira_url,
        )
        msg = f"Failed to authenticate with Jira: {error_details}"
        raise JiraAuthenticationError(msg) from None

    def get_projects(self) -> list[dict[str, Any]]:
        """Get all projects from Jira with enriched metadata."""

        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        def _sanitize_str(value: Any) -> str:
            if value is None:
                return ""
            return str(value)

        projects_details: list[dict[str, Any]] = []

        try:
            projects = self.jira.projects()
            if not projects:
                logger.warning("No projects found in Jira")
                return []

            for project in projects:
                detail = None
                try:
                    detail = self.jira.project(project.id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
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

                browse_url = f"{self.base_url}/browse/{project.key}"

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
        except Exception as e:  # noqa: BLE001
            error_msg = f"Failed to get projects: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_issue_types(self) -> list[dict[str, Any]]:
        """Get all issue types from Jira.

        Returns:
            List of issue type dictionaries with id, name, and description

        Raises:
            JiraApiError: If the API request fails

        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            issue_types = self.jira.issue_types()
            result = [
                {
                    "id": issue_type.id,
                    "name": issue_type.name,
                    "description": issue_type.description,
                }
                for issue_type in issue_types
            ]

            if not issue_types:
                logger.warning("No issue types found in Jira")

            return result  # noqa: TRY300
        except Exception as e:
            error_msg = f"Failed to get issue types: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_all_issues_for_project(
        self,
        project_key: str,
        *,
        expand_changelog: bool = True,
    ) -> list[Issue]:
        """Get all issues for a specific project, handling pagination."""
        all_issues: list[Issue] = []
        start_at = 0
        max_results = 100  # Fetch in batches of 100
        # Surround project key with quotes to handle reserved words
        jql = f'project = "{project_key}" ORDER BY created ASC'
        fields = None  # Get all fields
        expand = "changelog" if expand_changelog else None

        logger.notice("Fetching all issues for project '%s'...", project_key)

        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        # Verify project exists
        try:
            # Simple way to check if project exists - will raise exception if not found
            self.jira.project(project_key)
        except Exception as e:
            msg = f"Project '{project_key}' not found: {e!s}"
            raise JiraResourceNotFoundError(msg) from e

        # Fetch all pages
        while True:
            try:
                logger.debug(
                    "Fetching issues for %s: startAt=%s, maxResults=%s",
                    project_key,
                    start_at,
                    max_results,
                )

                issues_page = self.jira.search_issues(
                    jql,
                    startAt=start_at,
                    maxResults=max_results,
                    fields=fields,
                    expand=expand,
                    json_result=False,  # Get jira.Issue objects
                )

                if not issues_page:
                    logger.debug(
                        "No more issues found for %s at startAt=%s",
                        project_key,
                        start_at,
                    )
                    break  # Exit loop if no more issues are returned

                all_issues.extend(issues_page)
                logger.debug(
                    "Fetched %s issues (total: %s) for %s",
                    len(issues_page),
                    len(all_issues),
                    project_key,
                )

                # Check if this was the last page
                if len(issues_page) < max_results:
                    break

                start_at += len(issues_page)

            except Exception as e:
                error_msg = f"Failed to get issues page for project {project_key} at startAt={start_at}: {e!s}"
                logger.exception(error_msg)
                raise JiraApiError(error_msg) from e

        logger.info(
            "Finished fetching %s issues for project '%s'.",
            len(all_issues),
            project_key,
        )
        return all_issues

    def get_issue_details(self, issue_key: str) -> dict[str, Any]:
        """Get detailed information about a specific issue.

        Args:
            issue_key: The key of the issue to get details for

        Returns:
            A dictionary containing detailed issue information

        Raises:
            JiraResourceNotFoundError: If the issue is not found
            JiraApiError: If the API request fails

        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            issue = self.jira.issue(issue_key)

            # Extract basic issue data
            issue_data = {
                "id": issue.id,
                "key": issue.key,
                "summary": issue.fields.summary,
                "description": issue.fields.description,
                "issue_type": {
                    "id": issue.fields.issuetype.id,
                    "name": issue.fields.issuetype.name,
                },
                "status": {
                    "id": issue.fields.status.id,
                    "name": issue.fields.status.name,
                },
                "created": issue.fields.created,
                "updated": issue.fields.updated,
                "assignee": None,
                "reporter": None,
                "comments": [],
                "attachments": [],
            }

            # Add assignee if exists
            if hasattr(issue.fields, "assignee") and issue.fields.assignee:
                issue_data["assignee"] = {
                    "name": issue.fields.assignee.name,
                    "display_name": issue.fields.assignee.displayName,
                }

            # Add reporter if exists
            if hasattr(issue.fields, "reporter") and issue.fields.reporter:
                issue_data["reporter"] = {
                    "name": issue.fields.reporter.name,
                    "display_name": issue.fields.reporter.displayName,
                }

            # Add comments
            if hasattr(issue.fields, "comment") and issue.fields.comment:
                issue_data["comments"] = [
                    {
                        "id": comment.id,
                        "body": comment.body,
                        "author": comment.author.displayName,
                        "created": comment.created,
                    }
                    for comment in issue.fields.comment.comments
                ]

            # Add attachments
            if hasattr(issue.fields, "attachment") and issue.fields.attachment:
                issue_data["attachments"] = [
                    {
                        "id": attachment.id,
                        "filename": attachment.filename,
                        "size": attachment.size,
                        "content": attachment.url,
                    }
                    for attachment in issue.fields.attachment
                ]

            return issue_data  # noqa: TRY300
        except Exception as e:
            error_msg = f"Failed to get issue details for {issue_key}: {e!s}"
            logger.exception(error_msg)
            if (
                "issue does not exist" in str(e).lower()
                or "issue not found" in str(e).lower()
            ):
                msg = f"Issue {issue_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def get_users(self) -> list[dict[str, Any]]:
        """Get all users from Jira.

        Returns:
            List of user dictionaries with key, name, display name, email, and active status

        Raises:
            JiraApiError: If the API request fails

        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            users = self.jira.search_users(
                user=".",
                includeInactive=True,
                startAt=0,
                maxResults=1000,
            )

            logger.info("Retrieved %s users from Jira API", len(users))

            # Convert user objects to dictionaries with provenance metadata
            enriched_users = []
            for user in users:
                avatar_urls = getattr(user, "avatarUrls", None)
                if avatar_urls:
                    try:
                        avatar_urls = {
                            str(k): str(v)
                            for k, v in dict(avatar_urls).items()
                            if v
                        }
                    except Exception:  # noqa: BLE001
                        avatar_urls = None
                jira_user = {
                    "key": getattr(user, "key", None),
                    "name": getattr(user, "name", None),
                    "displayName": getattr(user, "displayName", None),
                    "emailAddress": getattr(user, "emailAddress", ""),
                    "active": getattr(user, "active", True),
                    "accountId": getattr(user, "accountId", None),
                    "timeZone": getattr(user, "timeZone", None) or getattr(user, "timezone", None),
                    "locale": getattr(user, "locale", None),
                    "self": getattr(user, "self", None),
                    "avatarUrls": avatar_urls,
                }
                enriched_users.append(jira_user)

            return enriched_users

        except Exception as e:
            error_msg = f"Failed to get users: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_user_info(self, user_key: str) -> dict[str, Any] | None:
        """Get information for a specific user by key.

        Args:
            user_key: The user key (account ID or username) to look up

        Returns:
            User dictionary with account ID, display name, email, and active status,
            or None if user not found

        Raises:
            JiraApiError: If the API request fails

        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Try to get the user by account ID first
            user = self.jira.user(user_key)

            if user:
                avatar_urls = getattr(user, "avatarUrls", None)
                if avatar_urls:
                    try:
                        avatar_urls = {
                            str(k): str(v)
                            for k, v in dict(avatar_urls).items()
                            if v
                        }
                    except Exception:  # noqa: BLE001
                        avatar_urls = None

                return {
                    "accountId": getattr(user, "accountId", None),
                    "displayName": getattr(user, "displayName", None),
                    "emailAddress": getattr(user, "emailAddress", ""),
                    "active": getattr(user, "active", True),
                    "key": getattr(user, "key", None),
                    "name": getattr(user, "name", None),
                    "timeZone": getattr(user, "timeZone", None) or getattr(user, "timezone", None),
                    "locale": getattr(user, "locale", None),
                    "avatarUrls": avatar_urls,
                }

            return None  # noqa: TRY300

        except Exception as e:
            # Log at debug level for not found cases, exception level for others
            if "404" in str(e) or "not found" in str(e).lower():
                logger.debug("User not found: %s", user_key)
                return None
            error_msg = f"Failed to get user info for {user_key}: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def download_user_avatar(self, avatar_url: str) -> tuple[bytes, str] | None:
        """Download a Jira user avatar and return (bytes, content_type)."""

        if not avatar_url:
            return None

        session = getattr(self.jira, "_session", None)
        if session is None:
            msg = "Jira session not initialized"
            raise JiraConnectionError(msg)

        try:
            response = session.get(avatar_url, stream=True, timeout=30)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to download avatar %s: %s", avatar_url, exc)
            return None

        content_type = response.headers.get("Content-Type", "image/png")
        try:
            data = response.content
        finally:
            response.close()

        if not data:
            return None

        return data, content_type

    def get_groups(self) -> list[dict[str, Any]]:
        """Retrieve all Jira groups visible to the migration user."""

        logger.info("Fetching Jira groups via groups picker endpoint")

        try:
            response = self._make_request(
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
            logger.info("Retrieved %s Jira groups", len(groups))
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
        except (JiraCaptchaError, JiraAuthenticationError, JiraConnectionError):
            raise
        except Exception as e:
            error_msg = f"Failed to get Jira groups: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_group_members(self, group_name: str) -> list[dict[str, Any]]:
        """Retrieve members for a Jira group, handling pagination."""

        if not group_name:
            return []

        members: list[dict[str, Any]] = []
        start_at = 0
        max_results = 100

        logger.debug("Fetching members for Jira group '%s'", group_name)

        try:
            while True:
                response = self._make_request(
                    "/rest/api/2/group/member",
                    params={
                        "groupname": group_name,
                        "includeInactiveUsers": "true",
                        "maxResults": max_results,
                        "startAt": start_at,
                    },
                )
                if response.status_code != HTTP_OK:
                    msg = (
                        "Failed to fetch group members"
                        f" for {group_name}: HTTP {response.status_code}"
                    )
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

            logger.debug(
                "Loaded %s members for Jira group '%s'",
                len(members),
                group_name,
            )
            return members
        except (JiraCaptchaError, JiraAuthenticationError, JiraConnectionError):
            raise
        except Exception as e:
            error_msg = f"Failed to get Jira group members for {group_name}: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_project_roles(self, project_key: str) -> list[dict[str, Any]]:
        """Retrieve Jira project roles and their actors for a project."""

        if not project_key:
            return []

        logger.debug("Fetching Jira project roles for '%s'", project_key)

        try:
            role_map_response = self._make_request(
                f"/rest/api/2/project/{project_key}/role",
            )
            if role_map_response.status_code != HTTP_OK:
                msg = (
                    "Failed to fetch Jira project roles"
                    f" for {project_key}: HTTP {role_map_response.status_code}"
                )
                raise JiraApiError(msg)

            role_map = role_map_response.json() or {}
            roles: list[dict[str, Any]] = []

            for role_name, role_url in role_map.items():
                if not isinstance(role_url, str):
                    continue

                detail_path = role_url
                if role_url.startswith(self.base_url):
                    detail_path = role_url[len(self.base_url) :]
                if not detail_path.startswith("/"):
                    detail_path = f"/{detail_path}"

                detail_response = self._make_request(detail_path)
                if detail_response.status_code != HTTP_OK:
                    logger.warning(
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
                            "accountId": (
                                (actor.get("actorUser") or {}).get("accountId")
                                or actor.get("accountId")
                            ),
                            "userKey": (
                                (actor.get("actorUser") or {}).get("key")
                                or actor.get("userKey")
                            ),
                            "groupName": (
                                (actor.get("actorGroup") or {}).get("name")
                                or actor.get("groupName")
                            ),
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

            logger.debug(
                "Discovered %s Jira project roles for '%s'",
                len(roles),
                project_key,
            )
            return roles
        except (JiraCaptchaError, JiraAuthenticationError, JiraConnectionError):
            raise
        except Exception as e:
            error_msg = f"Failed to fetch Jira project roles for {project_key}: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

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
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Use JQL to count issues in the project - surround with quotes to handle reserved words
            jql = f'project="{project_key}"'

            # Using fields="key" and expand='' minimizes data transfer
            issues = self.jira.search_issues(
                jql,
                maxResults=1,
                fields="key",
                expand="",
            )

            # Get total from the response
            return issues.total  # noqa: TRY300
        except Exception as e:
            error_msg = f"Failed to get issue count for project {project_key}: {e!s}"
            logger.exception(error_msg)
            if (
                "project does not exist" in str(e).lower()
                or "project not found" in str(e).lower()
            ):
                msg = f"Project {project_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def get_issue_watchers(self, issue_key: str) -> list[dict[str, Any]]:
        """Get the watchers for a specific Jira issue.

        Args:
            issue_key: The key of the issue to get watchers for (e.g., 'PROJECT-123')

        Returns:
            List of watcher dictionaries

        Raises:
            JiraResourceNotFoundError: If the issue is not found
            JiraApiError: If the API request fails

        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Use the JIRA library's watchers() method
            result = self.jira.watchers(issue_key)

            if not result:
                logger.debug("No watchers found for issue %s", issue_key)
                return []

            # Convert watchers to dictionaries
            return [
                {
                    "name": getattr(watcher, "name", None),
                    "displayName": getattr(watcher, "displayName", None),
                    "emailAddress": getattr(watcher, "emailAddress", None),
                    "active": getattr(watcher, "active", True),
                }
                for watcher in result.watchers
            ]
        except Exception as e:
            error_msg = f"Failed to get watchers for issue {issue_key}: {e!s}"
            logger.exception(error_msg)
            if (
                "issue does not exist" in str(e).lower()
                or "issue not found" in str(e).lower()
            ):
                msg = f"Issue {issue_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def get_all_statuses(self) -> list[dict[str, Any]]:
        """Get all statuses from Jira.

        Returns:
            List of status dictionaries with id, name, and category information

        Raises:
            JiraApiError: If the API request fails

        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Method 1: Use sample issues to extract statuses
            statuses: list[dict[str, Any]] = []
            issues = self.jira.search_issues("order by created DESC", maxResults=50)
            logger.debug("Retrieving statuses from %s sample issues", len(issues))

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
                logger.info("Retrieved %s statuses from sample issues", len(statuses))
                return statuses

            # Method 2: Use the status_categories endpoint
            path = "/rest/api/2/statuscategory"
            response = self._make_request(path)
            if not response or response.status_code != HTTP_OK:
                msg = f"Failed to get status categories: HTTP {response.status_code if response else 'No response'}"
                raise JiraApiError(  # noqa: TRY301
                    msg,
                )

            categories = response.json()
            logger.debug("Retrieved %s status categories from API", len(categories))

            # Use the status endpoint
            path = "/rest/api/2/status"
            response = self._make_request(path)
            if not response or response.status_code != HTTP_OK:
                msg = f"Failed to get statuses: HTTP {response.status_code if response else 'No response'}"
                raise JiraApiError(  # noqa: TRY301
                    msg,
                )

            statuses = response.json()
            logger.info("Retrieved %s statuses from Jira API", len(statuses))

            return statuses  # noqa: TRY300

        except Exception as e:
            error_msg = f"Failed to get statuses: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_priorities(self) -> list[dict[str, Any]]:
        """Get all priorities from Jira.

        Returns:
            List of priority dictionaries with id, name, and status

        Raises:
            JiraApiError: If the API request fails

        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # jira client has priorities() helper in many versions
            priorities = self.jira.priorities()
            result = [
                {
                    "id": getattr(p, "id", None),
                    "name": getattr(p, "name", None),
                    "status": getattr(p, "status", None),
                }
                for p in priorities
            ]
            if not priorities:
                logger.warning("No priorities found in Jira")
            return result
        except Exception as e:
            error_msg = f"Failed to get priorities: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_issue_property(self, issue_key: str, property_key: str) -> dict[str, Any] | None:
        """Get an issue property JSON by key. Returns None if missing or on 404.

        This supports add-ons like Simple Tasklists that store data in properties.
        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Prefer REST call via underlying session if available
            base_url = self.config.jira_config.get("url", "") if hasattr(self, "config") else ""
            if base_url:
                from urllib.parse import quote  # noqa: PLC0415

                url = f"{base_url}/rest/api/2/issue/{quote(issue_key)}/properties/{quote(property_key)}"
                resp = self._session.get(url, timeout=15) if hasattr(self, "_session") else None
                if resp and resp.status_code == 200:
                    data = resp.json()
                    # property payload can be under 'value'
                    if isinstance(data, dict):
                        return data.get("value", data)  # type: ignore[return-value]
                return None
        except Exception:  # noqa: BLE001
            logger.exception("Failed to fetch issue property: %s %s", issue_key, property_key)
            return None

    def _handle_response(self, response: Response) -> None:
        """Check response for CAPTCHA challenge and raise appropriate exception if found.

        Args:
            response: The HTTP response to check

        Raises:
            JiraCaptchaError: If a CAPTCHA challenge is detected

        """
        # Check for CAPTCHA challenge header
        if "X-Authentication-Denied-Reason" in response.headers:
            header_value = response.headers["X-Authentication-Denied-Reason"]
            if "CAPTCHA_CHALLENGE" in header_value:
                # Extract login URL if present
                login_url = self.jira_url + "/login.jsp"  # Default
                if "; login-url=" in header_value:
                    login_url = header_value.split("; login-url=")[1].strip()

                error_msg = (
                    f"CAPTCHA challenge detected. Please open {login_url} in your web "
                    f"browser, log in to resolve the CAPTCHA, and then restart the application"
                )
                logger.error("CAPTCHA challenge detected from Jira!")
                logger.error(
                    "Please open %s in your web browser and log in to resolve the CAPTCHA challenge",
                    login_url,
                )
                logger.debug("Jira client request count: %s", self.request_count)

                raise JiraCaptchaError(error_msg)

        # Check for other error responses
        if response.status_code >= HTTP_BAD_REQUEST_MIN:
            error_msg = f"HTTP Error {response.status_code}: {response.reason}"
            try:
                error_json = response.json()
                if "errorMessages" in error_json:
                    error_msg = (
                        f"{error_msg} - {', '.join(error_json['errorMessages'])}"
                    )
                elif "errors" in error_json:
                    error_msg = f"{error_msg} - {error_json['errors']}"
            except Exception:  # noqa: BLE001, S110
                pass

            if response.status_code == HTTP_NOT_FOUND:
                raise JiraResourceNotFoundError(error_msg) from None
            if response.status_code in {401, 403}:
                raise JiraAuthenticationError(error_msg) from None
            raise JiraApiError(error_msg) from None

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
            response = self._make_request(path)
            if not response:
                msg = "Failed to get status categories (request failed)"
                raise JiraApiError(msg)  # noqa: TRY301

            if response.status_code != HTTP_OK:
                msg = f"Failed to get status categories: HTTP {response.status_code}"
                raise JiraApiError(msg)  # noqa: TRY301

            categories = response.json()
            logger.info("Retrieved %s status categories from Jira API", len(categories))

            return categories  # noqa: TRY300
        except Exception as e:
            error_msg = f"Failed to get status categories: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def _get_field_metadata_via_createmeta(self, field_id: str) -> dict[str, Any]:  # noqa: C901, PLR0912
        """Get field metadata using the issue/createmeta endpoint.

        Args:
            field_id: The ID of the custom field to retrieve metadata for

        Returns:
            Dictionary containing field metadata

        Raises:
            JiraResourceNotFoundError: If the field is not found
            JiraApiError: If the API request fails

        """
        logger.debug(
            "Attempting to get field metadata for %s using createmeta endpoint",
            field_id,
        )

        try:
            # We no longer need to check the cache here as it's done in the calling method

            # Get a list of projects
            projects = self.project_cache or self.get_projects()
            if not projects:
                msg = "No projects found to retrieve field metadata"
                raise JiraApiError(msg)  # noqa: TRY301

            # Get a list of issue types
            issue_types = self.issue_type_cache or self.get_issue_types()
            if not issue_types:
                msg = "No issue types found to retrieve field metadata"
                raise JiraApiError(msg)  # noqa: TRY301

            # Try with each project/issue type combination until we find field metadata
            for project in projects:
                project_key = project.get("key")
                for issue_type in issue_types:
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
                    logger.debug(
                        "Trying createmeta for project=%s issuetype=%s (%s)",
                        project_key,
                        issue_type_id,
                        issue_type.get("name"),
                    )

                    try:
                        meta_response = self._make_request(path, params=params)
                    except JiraApiError as e:
                        logger.debug("createmeta request failed: %s", e)
                        continue

                    if not meta_response or meta_response.status_code != HTTP_OK:
                        continue

                    meta_data = meta_response.json() or {}
                    projects_arr = meta_data.get("projects") or []
                    if not projects_arr:
                        continue
                    issuetypes_arr = (projects_arr[0] or {}).get("issuetypes") or []
                    if not issuetypes_arr:
                        continue
                    fields_map = (issuetypes_arr[0] or {}).get("fields") or {}
                    if not isinstance(fields_map, dict):
                        continue

                    # Cache all discovered fields
                    for fid, fdef in fields_map.items():
                        self.field_options_cache[fid] = fdef

                    # If this is the field we're looking for, return it immediately
                    if field_id in fields_map:
                        return fields_map[field_id]

            # If we've checked all project/issue type combinations and still haven't found it
            field_data = self.field_options_cache.get(field_id)
            if field_data:
                return field_data

            msg = f"Field {field_id} not found in any project/issue type combination"
            raise JiraResourceNotFoundError(msg)  # noqa: TRY301

        except JiraResourceNotFoundError:
            raise  # Re-raise specific exceptions
        except Exception as e:
            error_msg = f"Error getting field metadata via createmeta endpoint: {e!s}"
            logger.warning(error_msg)
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

        """
        try:
            # Use the fields endpoint to get all fields, then filter for custom fields
            response = self.jira.fields()

            # Filter for custom fields (custom fields typically start with 'customfield_')
            custom_fields = [
                field
                for field in response
                if field.get("id", "").startswith("customfield_")
            ]

            logger.debug("Retrieved %d custom fields from Jira", len(custom_fields))
            return custom_fields  # noqa: TRY300

        except Exception as e:
            error_msg = f"Failed to retrieve custom fields: {e}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def _patch_jira_client(self) -> None:
        """Patch the JIRA client to catch CAPTCHA challenges.

        This adds CAPTCHA detection to all API requests made through the JIRA library.
        """
        if not self.jira:
            msg = "Cannot patch JIRA client: No active connection"
            raise JiraConnectionError(msg)

        # Store original _session.request method
        original_request = self.jira._session.request  # noqa: SLF001

        # Create patched method that checks for CAPTCHA
        def patched_request(method: str, url: str, **kwargs: object) -> Response:
            try:
                self.request_count += 1
                # dump requestcount
                logger.debug("Jira client request count: %s", self.request_count)
                response = original_request(method, url, **kwargs)

                # Check for CAPTCHA or other errors
                self._handle_response(response)

                return response  # noqa: TRY300
            except (
                JiraCaptchaError,
                JiraAuthenticationError,
                JiraResourceNotFoundError,
            ):
                raise  # Re-raise specific exceptions
            except Exception as e:
                msg = f"Error during API request to {url}: {e!s}"
                raise JiraApiError(msg) from e

        # Replace the method with our patched version
        self.jira._session.request = patched_request  # noqa: SLF001
        logger.debug("JIRA client patched to handle errors and CAPTCHA challenges")

    def _make_request(
        self,
        path: str,
        method: str = "GET",
        content_type: str = "application/json",
        **kwargs: object,
    ) -> Response:
        """Make API requests with proper error handling and CAPTCHA detection.

        Args:
            path: API path relative to base_url
            method: HTTP method (GET, POST, etc.)
            content_type: Content type for request headers
            **kwargs: Additional arguments to pass to jira._session.request

        Returns:
            Response object if successful

        Raises:
            JiraConnectionError: If client is not initialized or connection fails
            JiraApiError: If the API request fails
            JiraCaptchaError: If a CAPTCHA challenge is detected

        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        # Construct full URL
        url = f"{self.base_url}{path}"

        # Add content headers if requested
        headers = {}
        if content_type:
            headers.update(
                {
                    "Content-Type": content_type,
                    "Accept": content_type,
                },
            )

        # Add any headers passed in kwargs
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))

        try:
            return self.jira._session.request(method, url, headers=headers, **kwargs)  # noqa: SLF001

            # If we got here, we've passed the CAPTCHA check in patched_request
        except (
            JiraCaptchaError,
            JiraAuthenticationError,
            JiraResourceNotFoundError,
            JiraApiError,
        ):
            raise  # Re-raise specific exceptions
        except Exception as e:
            msg = f"Error during API request to {url}: {e!s}"
            raise JiraConnectionError(msg) from e

    # Tempo API methods
    def get_tempo_accounts(self, *, expand: bool = False) -> list[dict[str, Any]]:  # noqa: ARG002
        """Retrieve all Tempo accounts."""
        path = "/rest/tempo-accounts/1/account"
        params = {
            "expand": "true",
            "skipArchived": "false",
        }

        logger.info("Fetching Tempo accounts")
        try:
            response = self._make_request(path, params=params)
            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo accounts: HTTP {response.status_code}"
                raise JiraApiError(msg)  # noqa: TRY301

            accounts = response.json()
            logger.info("Successfully retrieved %s Tempo accounts.", len(accounts))
            return accounts  # noqa: TRY300
        except (JiraCaptchaError, JiraAuthenticationError, JiraConnectionError):
            raise  # Re-raise specific exceptions
        except Exception as e:
            error_msg = f"Failed to retrieve Tempo accounts: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_customers(self) -> list[dict[str, Any]]:
        """Retrieve all Tempo customers (often used for Companies).

        Returns:
            A list of Tempo customers

        Raises:
            JiraApiError: If the API request fails

        """
        path = "/rest/tempo-accounts/1/customer"
        logger.info("Fetching Tempo customers (Companies)")

        try:
            response = self._make_request(path)
            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo customers: HTTP {response.status_code}"
                raise JiraApiError(msg)  # noqa: TRY301

            customers = response.json()
            logger.info("Successfully retrieved %s Tempo customers.", len(customers))
            return customers  # noqa: TRY300
        except (JiraCaptchaError, JiraAuthenticationError, JiraConnectionError):
            raise  # Re-raise specific exceptions
        except Exception as e:
            error_msg = f"Failed to retrieve Tempo customers: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_account_links_for_project(
        self,
        project_id: int,
    ) -> list[dict[str, Any]]:
        """Retrieve Tempo account links for a specific Jira project.

        Args:
            project_id: The Jira project ID.

        Returns:
            A list of account links

        Raises:
            JiraResourceNotFoundError: If the project is not found
            JiraApiError: If the API request fails

        """
        # Use the Tempo account-by-project endpoint for project-specific account lookup
        path = f"/rest/tempo-accounts/1/account/project/{project_id}"

        logger.debug("Fetching Tempo account links for project '%s'", project_id)
        try:
            response = self._make_request(path)

            # Handle 404s specially - these might be expected if no links exist
            if response.status_code == HTTP_NOT_FOUND:
                logger.warning("No account links found for project %s.", project_id)
                return []

            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve account links: HTTP {response.status_code}"
                raise JiraApiError(msg)  # noqa: TRY301

            links = response.json()
            logger.debug(
                "Successfully retrieved %s account links for project %s.",
                len(links),
                project_id,
            )
            return links  # noqa: TRY300
        except (JiraCaptchaError, JiraAuthenticationError, JiraConnectionError):
            raise  # Re-raise specific exceptions
        except JiraResourceNotFoundError:
            # Convert to empty list for this specific case since it's an expected condition
            logger.warning(
                "Project %s not found or no account links exist.",
                project_id,
            )
            return []
        except Exception as e:
            error_msg = (
                f"Failed to retrieve account links for project {project_id}: {e!s}"
            )
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_issue_link_types(self) -> list[dict[str, Any]]:
        """Get all issue link types from Jira.

        Returns:
            List of issue link type dictionaries with id, name, inward, and outward

        Raises:
            JiraApiError: If the API request fails

        """
        try:
            link_types = self.jira.issue_link_types()
            result = [
                {
                    "id": link_type.id,
                    "name": link_type.name,
                    "inward": link_type.inward,
                    "outward": link_type.outward,
                }
                for link_type in link_types
            ]

            if not link_types:
                logger.warning("No issue link types found in Jira")

            return result  # noqa: TRY300
        except Exception as e:
            error_msg = f"Failed to get issue link types: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_work_logs_for_issue(self, issue_key: str) -> list[dict[str, Any]]:
        """Get all work logs for a specific issue.

        Args:
            issue_key: The key of the issue to get work logs for

        Returns:
            List of work log dictionaries with complete metadata

        Raises:
            JiraResourceNotFoundError: If the issue is not found
            JiraApiError: If the API request fails

        """
        try:
            # Get work logs using the JIRA library's worklog method
            work_logs = self.jira.worklogs(issue_key)

            result = []
            for work_log in work_logs:
                work_log_data = {
                    "id": work_log.id,
                    "issue_key": issue_key,
                    "author": {
                        "name": getattr(work_log.author, "name", None),
                        "display_name": getattr(work_log.author, "displayName", None),
                        "email": getattr(work_log.author, "emailAddress", None),
                        "account_id": getattr(work_log.author, "accountId", None),
                    },
                    "started": work_log.started,
                    "time_spent": work_log.timeSpent,
                    "time_spent_seconds": work_log.timeSpentSeconds,
                    "comment": getattr(work_log, "comment", None),
                    "created": work_log.created,
                    "updated": work_log.updated,
                }

                # Add update author if different from original author
                if hasattr(work_log, "updateAuthor") and work_log.updateAuthor:
                    work_log_data["update_author"] = {
                        "name": getattr(work_log.updateAuthor, "name", None),
                        "display_name": getattr(
                            work_log.updateAuthor,
                            "displayName",
                            None,
                        ),
                        "email": getattr(work_log.updateAuthor, "emailAddress", None),
                        "account_id": getattr(work_log.updateAuthor, "accountId", None),
                    }

                result.append(work_log_data)

            logger.debug(
                "Retrieved %s work logs for issue %s",
                len(result),
                issue_key,
            )
            return result  # noqa: TRY300

        except Exception as e:
            error_msg = f"Failed to get work logs for issue {issue_key}: {e!s}"
            logger.exception(error_msg)
            if (
                "issue does not exist" in str(e).lower()
                or "issue not found" in str(e).lower()
            ):
                msg = f"Issue {issue_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def get_all_work_logs_for_project(
        self,
        project_key: str,
        *,
        include_empty: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Get all work logs for all issues in a project."""
        try:
            logger.info(
                "Fetching work logs for all issues in project '%s'...",
                project_key,
            )

            # Get all issues for the project with worklog field expanded
            all_issues = self.get_all_issues_for_project(
                project_key,
                expand_changelog=False,
            )

            work_logs_by_issue = {}
            issues_with_logs = 0
            total_work_logs = 0

            for issue in all_issues:
                issue_key = issue.key

                # Check if issue has work logs in the basic fields first
                has_work_logs = (
                    hasattr(issue.fields, "worklog")
                    and issue.fields.worklog
                    and issue.fields.worklog.total > 0
                )

                if has_work_logs or include_empty:
                    # Apply adaptive rate limiting before request
                    self.rate_limiter.wait_if_needed(f"get_work_logs_{project_key}")

                    try:
                        request_start = time.time()
                        work_logs = self.get_work_logs_for_issue(issue_key)
                        response_time = time.time() - request_start

                        # Record successful response for rate limiting adaptation
                        self.rate_limiter.record_response(response_time, HTTP_OK)

                        if work_logs or include_empty:
                            work_logs_by_issue[issue_key] = work_logs
                            if work_logs:
                                issues_with_logs += 1
                                total_work_logs += len(work_logs)

                    except JiraResourceNotFoundError:
                        # Issue was deleted between listing and fetching work logs
                        logger.warning(
                            "Issue %s not found when fetching work logs",
                            issue_key,
                        )
                        # Record 404 response
                        self.rate_limiter.record_response(
                            time.time() - request_start,
                            HTTP_BAD_REQUEST_MIN + 4, # 404
                        )
                        continue
                    except JiraApiError as e:
                        logger.warning(
                            "Failed to get work logs for issue %s: %s",
                            issue_key,
                            e,
                        )
                        # Record error response (assuming 500 for API errors)
                        self.rate_limiter.record_response(
                            time.time() - request_start,
                            HTTP_BAD_REQUEST_MIN + 5, # 500
                        )
                        continue

            logger.info(
                "Work log extraction complete for project '%s': "
                "%s issues with work logs, %s total work logs",
                project_key,
                issues_with_logs,
                total_work_logs,
            )

            return work_logs_by_issue  # noqa: TRY300

        except Exception as e:
            error_msg = f"Failed to get work logs for project {project_key}: {e!s}"
            logger.exception(error_msg)
            if (
                "project does not exist" in str(e).lower()
                or "project not found" in str(e).lower()
            ):
                msg = f"Project {project_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def get_work_log_details(self, issue_key: str, work_log_id: str) -> dict[str, Any]:
        """Get detailed information for a specific work log.

        Args:
            issue_key: The key of the issue containing the work log
            work_log_id: The ID of the work log to get details for

        Returns:
            Dictionary containing detailed work log information

        Raises:
            JiraResourceNotFoundError: If the issue or work log is not found
            JiraApiError: If the API request fails

        """
        try:
            # Use the JIRA library's worklog method to get specific work log
            work_log = self.jira.worklog(issue_key, work_log_id)

            work_log_data = {
                "id": work_log.id,
                "issue_key": issue_key,
                "author": {
                    "name": getattr(work_log.author, "name", None),
                    "display_name": getattr(work_log.author, "displayName", None),
                    "email": getattr(work_log.author, "emailAddress", None),
                    "account_id": getattr(work_log.author, "accountId", None),
                },
                "started": work_log.started,
                "time_spent": work_log.timeSpent,
                "time_spent_seconds": work_log.timeSpentSeconds,
                "comment": getattr(work_log, "comment", None),
                "created": work_log.created,
                "updated": work_log.updated,
            }

            # Add update author if different from original author
            if hasattr(work_log, "updateAuthor") and work_log.updateAuthor:
                work_log_data["update_author"] = {
                    "name": getattr(work_log.updateAuthor, "name", None),
                    "display_name": getattr(work_log.updateAuthor, "displayName", None),
                    "email": getattr(work_log.updateAuthor, "emailAddress", None),
                    "account_id": getattr(work_log.updateAuthor, "accountId", None),
                }

            # Add visibility restrictions if present
            if hasattr(work_log, "visibility") and work_log.visibility:
                work_log_data["visibility"] = {
                    "type": getattr(work_log.visibility, "type", None),
                    "value": getattr(work_log.visibility, "value", None),
                }

            return work_log_data  # noqa: TRY300

        except Exception as e:
            error_msg = (
                f"Failed to get work log {work_log_id} for issue {issue_key}: {e!s}"
            )
            logger.exception(error_msg)
            if (
                "issue does not exist" in str(e).lower()
                or "issue not found" in str(e).lower()
                or "worklog does not exist" in str(e).lower()
                or "worklog not found" in str(e).lower()
            ):
                msg = f"Issue {issue_key} or work log {work_log_id} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def get_tempo_work_logs(  # noqa: PLR0913
        self,
        issue_key: str | None = None,
        project_key: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        user_key: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Get Tempo work logs with enhanced metadata and attributes.

        Args:
            issue_key: Filter by specific issue key
            project_key: Filter by specific project key
            date_from: Start date in YYYY-MM-DD format
            date_to: End date in YYYY-MM-DD format
            user_key: Filter by specific user key
            limit: Maximum number of results per request (default: 1000)

        Returns:
            List of Tempo work log dictionaries with enhanced metadata

        Raises:
            JiraApiError: If the API request fails

        """
        try:
            # Build query parameters
            params = {"limit": limit}
            if issue_key:
                params["issue"] = issue_key
            if project_key:
                params["project"] = project_key
            if date_from:
                params["dateFrom"] = date_from
            if date_to:
                params["dateTo"] = date_to
            if user_key:
                params["user"] = user_key

            # Use Tempo Timesheets API v3 endpoint
            path = "/rest/tempo-timesheets/3/worklogs"
            logger.info(
                "Fetching Tempo work logs with params: %s",
                {k: v for k, v in params.items() if k != "limit"},
            )

            response = self.jira._session.get(  # noqa: SLF001
                f"{self.base_url}{path}",
                params=params,
            )

            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo work logs: HTTP {response.status_code}"
                logger.error(msg)
                raise JiraApiError(msg)  # noqa: TRY301

            work_logs = response.json()
            logger.info("Successfully retrieved %s Tempo work logs", len(work_logs))

            # Enhance work logs with additional metadata
            enhanced_work_logs = []
            for work_log in work_logs:
                enhanced_work_log = {
                    "tempo_worklog_id": work_log.get("tempoWorklogId"),
                    "jira_worklog_id": work_log.get("jiraWorklogId"),
                    "issue_key": work_log.get("issue", {}).get("key"),
                    "issue_id": work_log.get("issue", {}).get("id"),
                    "author": {
                        "username": work_log.get("author", {}).get("name"),
                        "display_name": work_log.get("author", {}).get("displayName"),
                        "account_id": work_log.get("author", {}).get("accountId"),
                    },
                    "time_spent_seconds": work_log.get("timeSpentSeconds"),
                    "billable_seconds": work_log.get("billableSeconds"),
                    "date_started": work_log.get("dateStarted"),
                    "time_started": work_log.get("timeStarted"),
                    "comment": work_log.get("comment"),
                    "created": work_log.get("created"),
                    "updated": work_log.get("updated"),
                    "work_attributes": work_log.get("workAttributes", []),
                    "account": work_log.get("account", {}),
                    "approval_status": work_log.get("approvalStatus"),
                    "external_hours": work_log.get("externalHours"),
                    "external_id": work_log.get("externalId"),
                    "origin_task_id": work_log.get("originTaskId"),
                }
                enhanced_work_logs.append(enhanced_work_log)

            return enhanced_work_logs  # noqa: TRY300

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo work logs: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_work_attributes(self) -> list[dict[str, Any]]:
        """Get all Tempo work attributes (custom fields for work logs).

        Returns:
            List of work attribute dictionaries

        Raises:
            JiraApiError: If the API request fails

        """
        try:
            path = "/rest/tempo-timesheets/3/work-attributes"
            logger.info("Fetching Tempo work attributes")

            response = self.jira._session.get(  # noqa: SLF001
                f"{self.base_url}{path}",
            )

            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo work attributes: HTTP {response.status_code}"
                logger.error(msg)
                raise JiraApiError(msg)  # noqa: TRY301

            attributes = response.json()
            logger.info(
                "Successfully retrieved %s Tempo work attributes",
                len(attributes),
            )

            return attributes  # noqa: TRY300

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo work attributes: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_all_work_logs_for_project(
        self,
        project_key: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all Tempo work logs for a project with pagination handling."""
        try:
            logger.info(
                "Fetching all Tempo work logs for project '%s' from %s to %s",
                project_key,
                date_from or "beginning",
                date_to or "end",
            )

            all_work_logs = []
            limit = 1000
            offset = 0

            while True:
                # Apply adaptive rate limiting before request
                self.rate_limiter.wait_if_needed(f"get_tempo_work_logs_{project_key}")

                # Build query parameters
                params = {
                    "project": project_key,
                    "limit": limit,
                    "offset": offset,
                }
                if date_from:
                    params["dateFrom"] = date_from
                if date_to:
                    params["dateTo"] = date_to

                path = "/rest/tempo-timesheets/3/worklogs"

                request_start = time.time()
                response = self.jira._session.get(  # noqa: SLF001
                    f"{self.base_url}{path}",
                    params=params,
                )
                response_time = time.time() - request_start

                # Record response for rate limiting adaptation
                self.rate_limiter.record_response(response_time, response.status_code)

                if response.status_code != HTTP_OK:
                    msg = (
                        f"Failed to retrieve Tempo work logs for project {project_key}: HTTP {response.status_code}"
                    )
                    logger.error(msg)
                    raise JiraApiError(msg)  # noqa: TRY301

                work_logs_batch = response.json()

                if not work_logs_batch:
                    break

                # Process batch and add to results
                for work_log in work_logs_batch:
                    enhanced_work_log = {
                        "tempo_worklog_id": work_log.get("tempoWorklogId"),
                        "jira_worklog_id": work_log.get("jiraWorklogId"),
                        "issue_key": work_log.get("issue", {}).get("key"),
                        "issue_id": work_log.get("issue", {}).get("id"),
                        "author": {
                            "username": work_log.get("author", {}).get("name"),
                            "display_name": work_log.get("author", {}).get(
                                "displayName",
                            ),
                            "account_id": work_log.get("author", {}).get("accountId"),
                        },
                        "time_spent_seconds": work_log.get("timeSpentSeconds"),
                        "billable_seconds": work_log.get("billableSeconds"),
                        "date_started": work_log.get("dateStarted"),
                        "time_started": work_log.get("timeStarted"),
                        "comment": work_log.get("comment"),
                        "created": work_log.get("created"),
                        "updated": work_log.get("updated"),
                        "work_attributes": work_log.get("workAttributes", []),
                        "account": work_log.get("account", {}),
                        "approval_status": work_log.get("approvalStatus"),
                        "external_hours": work_log.get("externalHours"),
                        "external_id": work_log.get("externalId"),
                        "origin_task_id": work_log.get("originTaskId"),
                    }
                    all_work_logs.append(enhanced_work_log)

                # Check if we've reached the end
                if len(work_logs_batch) < limit:
                    break

                offset += limit

            logger.info(
                "Tempo work log extraction complete for project '%s': %s total work logs",
                project_key,
                len(all_work_logs),
            )
            return all_work_logs  # noqa: TRY300

        except Exception as e:
            error_msg = f"Failed to retrieve all Tempo work logs for project {project_key}: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_work_log_by_id(self, tempo_worklog_id: str) -> dict[str, Any]:
        """Get a specific Tempo work log by its Tempo ID.

        Args:
            tempo_worklog_id: The Tempo work log ID

        Returns:
            Dictionary containing detailed Tempo work log information

        Raises:
            JiraResourceNotFoundError: If the work log is not found
            JiraApiError: If the API request fails

        """
        try:
            path = f"/rest/tempo-timesheets/3/worklogs/{tempo_worklog_id}"
            logger.debug("Fetching Tempo work log with ID: %s", tempo_worklog_id)

            response = self.jira._session.get(  # noqa: SLF001
                f"{self.base_url}{path}",
            )

            if response.status_code == HTTP_NOT_FOUND:
                msg = f"Tempo work log {tempo_worklog_id} not found"
                raise JiraResourceNotFoundError(msg)  # noqa: TRY301
            if response.status_code != HTTP_OK:
                msg = (
                    f"Failed to retrieve Tempo work log {tempo_worklog_id}: HTTP {response.status_code}"
                )
                logger.error(msg)
                raise JiraApiError(msg)  # noqa: TRY301

            work_log = response.json()

            # Return enhanced work log data
            return {
                "tempo_worklog_id": work_log.get("tempoWorklogId"),
                "jira_worklog_id": work_log.get("jiraWorklogId"),
                "issue_key": work_log.get("issue", {}).get("key"),
                "issue_id": work_log.get("issue", {}).get("id"),
                "author": {
                    "username": work_log.get("author", {}).get("name"),
                    "display_name": work_log.get("author", {}).get("displayName"),
                    "account_id": work_log.get("author", {}).get("accountId"),
                },
                "time_spent_seconds": work_log.get("timeSpentSeconds"),
                "billable_seconds": work_log.get("billableSeconds"),
                "date_started": work_log.get("dateStarted"),
                "time_started": work_log.get("timeStarted"),
                "comment": work_log.get("comment"),
                "created": work_log.get("created"),
                "updated": work_log.get("updated"),
                "work_attributes": work_log.get("workAttributes", []),
                "account": work_log.get("account", {}),
                "approval_status": work_log.get("approvalStatus"),
                "external_hours": work_log.get("externalHours"),
                "external_id": work_log.get("externalId"),
                "origin_task_id": work_log.get("originTaskId"),
            }

        except Exception as e:
            if "not found" in str(e).lower():
                msg = f"Tempo work log {tempo_worklog_id} not found"
                raise JiraResourceNotFoundError(msg) from e
            error_msg = f"Failed to retrieve Tempo work log {tempo_worklog_id}: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_user_work_logs(
        self,
        user_key: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all Tempo work logs for a specific user."""
        try:
            return self.get_tempo_work_logs(
                user_key=user_key,
                date_from=date_from,
                date_to=date_to,
            )

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo work logs for user {user_key}: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_time_entries(  # noqa: C901
        self,
        project_keys: list[str] | None = None,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        user_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get Tempo time entries with enhanced metadata for migration."""
        try:
            logger.info(
                "Fetching Tempo time entries for projects: %s, date range: %s to %s, user: %s",
                project_keys,
                date_from,
                date_to,
                user_key,
            )

            all_time_entries = []

            if project_keys:
                # Get work logs for specific projects
                for project_key in project_keys:
                    try:
                        project_work_logs = self.get_tempo_all_work_logs_for_project(
                            project_key=project_key,
                            date_from=date_from,
                            date_to=date_to,
                        )

                        # Filter by user if specified
                        if user_key:
                            project_work_logs = [
                                log
                                for log in project_work_logs
                                if log.get("author", {}).get("key") == user_key
                            ]

                        all_time_entries.extend(project_work_logs)
                        logger.debug(
                            "Retrieved %d entries for project %s",
                            len(project_work_logs),
                            project_key,
                        )

                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "Failed to get Tempo entries for project %s: %s",
                            project_key,
                            e,
                        )
                        continue
            # Get work logs using general method (may be limited by Tempo API)
            elif user_key:
                all_time_entries = self.get_tempo_user_work_logs(
                    user_key=user_key,
                    date_from=date_from,
                    date_to=date_to,
                )
            else:
                all_time_entries = self.get_tempo_work_logs(
                    date_from=date_from,
                    date_to=date_to,
                )

            # Enhance entries with migration metadata
            enhanced_entries = []
            for entry in all_time_entries:
                enhanced_entry = entry.copy()

                # Add migration-specific metadata
                enhanced_entry["_migration_metadata"] = {
                    "source_type": "tempo",
                    "extraction_timestamp": datetime.now(tz=UTC).isoformat(),
                    "tempo_worklog_id": entry.get("tempo_worklog_id"),
                    "jira_worklog_id": entry.get("worklogId"),
                    "issue_key": entry.get("issue", {}).get("key"),
                    "project_key": entry.get("issue", {}).get("projectKey"),
                }

                # Ensure consistent field naming for migration
                if "timeSpentSeconds" in entry:
                    enhanced_entry["timeSpent"] = entry["timeSpentSeconds"]

                if "dateStarted" in entry:
                    enhanced_entry["started"] = entry["dateStarted"]
                elif "started" not in entry and "created" in entry:
                    enhanced_entry["started"] = entry["created"]

                enhanced_entries.append(enhanced_entry)

            logger.success(
                "Retrieved %d Tempo time entries total",
                len(enhanced_entries),
            )
            return enhanced_entries  # noqa: TRY300

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo time entries: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    # ===== ENHANCED PERFORMANCE FEATURES =====

    def get_performance_stats(self) -> dict[str, Any]:
        """Get comprehensive performance statistics."""
        return self.performance_optimizer.get_comprehensive_stats()

    # ===== BATCH OPERATIONS =====

    def batch_get_issues(self, issue_keys: list[str]) -> dict[str, Issue]:
        """Retrieve multiple issues in batches for optimal performance."""
        if not issue_keys:
            return {}

        return self.performance_optimizer.batch_processor.process_batches(
            issue_keys,
            self._fetch_issues_batch,
        )

    def _fetch_issues_batch(self, issue_keys: list[str], **kwargs: object) -> dict[str, Issue]:  # noqa: ARG002
        """Fetch a batch of issues from Jira API."""
        if not issue_keys:
            return {}

        # Use JQL to fetch multiple issues at once
        jql = f"key in ({','.join(issue_keys)})"

        try:
            issues = self.jira.search_issues(
                jql,
                maxResults=len(issue_keys),
                expand="changelog",
            )
            return {issue.key: issue for issue in issues}
        except Exception:
            logger.exception(
                "Batch issue fetch failed for %d issues",
                len(issue_keys),
            )
            return {}

    def batch_get_projects(self, project_keys: list[str]) -> dict[str, dict]:
        """Retrieve multiple projects in batches for optimal performance."""
        if not project_keys:
            return {}

        # Get all projects and filter to requested keys
        all_projects = self.get_projects()
        return {
            project["key"]: project
            for project in all_projects
            if project["key"] in project_keys
        }

    @rate_limited()
    def stream_all_issues_for_project(
        self,
        project_key: str,
        fields: str | None = None,
        batch_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream all issues for a project with memory-efficient pagination."""
        effective_batch_size = batch_size or self.batch_size

        paginator = StreamingPaginator(
            batch_size=effective_batch_size,
            rate_limiter=self.rate_limiter,
        )

        return paginator.paginate_jql_search(
            jira_client=self.jira,
            jql=f"project = {project_key}",
            fields=fields,
        )

    def batch_get_users_by_keys(self, user_keys: list[str]) -> dict[str, dict]:
        """Retrieve multiple users in batches."""
        if not user_keys:
            return {}

        # Get all users and filter to requested keys
        all_users = self.get_users()
        user_dict = {
            user.get("key", user.get("accountId", "")): user for user in all_users
        }

        return {key: user_dict[key] for key in user_keys if key in user_dict}

    @cached(ttl=3600)  # Cache for 1 hour
    def get_project_metadata_enhanced(self, project_key: str) -> dict[str, Any]:
        """Get comprehensive project metadata with caching."""
        try:
            project = self.jira.project(project_key)

            # Get additional metadata
            issue_types = self.jira.createmeta_issuetypes(project.key)
            statuses = self.jira.project_status(project.key)

            return {
                "project": {
                    "id": project.id,
                    "key": project.key,
                    "name": project.name,
                    "description": getattr(project, "description", ""),
                    "lead": (
                        getattr(project, "lead", {}).get("name", "Unknown")
                        if hasattr(project, "lead")
                        else "Unknown"
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
            error_msg = (
                f"Failed to get enhanced project metadata for {project_key}: {e}"
            )
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e
