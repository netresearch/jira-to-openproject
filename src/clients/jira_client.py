"""Jira API client for the migration project.

Provides a clean, exception-based interface for Jira resource access.
Enhanced with performance optimizations including batch operations,
caching, and parallel processing.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from requests import Response

if TYPE_CHECKING:
    from jira.exceptions import JIRAError as AtlassianJIRAError

    from jira import JIRA, Issue
else:
    # At runtime, avoid importing jira to prevent stub issues
    AtlassianJIRAError = Exception  # type: ignore[misc,assignment]
from src import config
from src.display import configure_logging
from src.utils.config_validation import ConfigurationValidationError, SecurityValidator
from src.utils.performance_optimizer import (
    PerformanceOptimizer,
    StreamingPaginator,
    rate_limited,
)
from src.utils.rate_limiter import create_jira_rate_limiter

HTTP_OK = 200
HTTP_BAD_REQUEST_MIN = 400
HTTP_NOT_FOUND = 404

try:
    from src.config import logger
except Exception:
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


def _import_real_jira_module() -> Any:
    """Import the real ``jira`` module even if a local test stub shadows it.

    Extracted as a module-level helper so tests can patch it when they need
    to inject a fake ``JIRA`` class.
    """
    try:
        import importlib
        import importlib.util
        import site
        import sys
        from pathlib import Path as _Path

        # If a shadow stub is loaded from this repo, purge it. The stub lives
        # at <repo-root>/jira/__init__.py — locate that path relative to this
        # source file rather than hard-coding an absolute developer path.
        repo_root = _Path(__file__).resolve().parents[2]
        local_stub_init = repo_root / "jira" / "__init__.py"
        if "jira" in sys.modules:
            mod = sys.modules["jira"]
            mod_file = getattr(mod, "__file__", "") or ""
            if mod_file:
                try:
                    is_local_stub = _Path(mod_file).resolve() == local_stub_init.resolve()
                except OSError:
                    is_local_stub = False
                if is_local_stub:
                    # Remove stub and any submodules to force a clean import
                    for key in list(sys.modules.keys()):
                        if key == "jira" or key.startswith("jira."):
                            sys.modules.pop(key, None)

        # Prefer virtualenv site-packages path. site.getsitepackages() can
        # raise AttributeError on bare/embedded interpreters; OSError covers
        # the rare case where the path lookup itself fails.
        candidates: list[_Path] = []
        try:
            candidates.extend(_Path(p) for p in site.getsitepackages())
        except AttributeError, OSError:
            pass
        try:
            usp = site.getusersitepackages()
            if usp:
                candidates.append(_Path(usp))
        except AttributeError, OSError:
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
    except ImportError, ModuleNotFoundError, AttributeError, OSError:
        # ImportError / ModuleNotFoundError: jira package missing or corrupt.
        # AttributeError: site module missing expected accessors.
        # OSError: filesystem error walking site-packages.
        # In every case, fall back to the standard import path; that either
        # succeeds or raises ImportError, which the caller already expects.
        import importlib

        return importlib.import_module("jira")


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

        # Service composition (Phase 3a/3b/3c/3d/3e/3f/3g of ADR-002 — see ADR for the
        # decomposition plan).
        from src.clients.jira_agile_service import JiraAgileService
        from src.clients.jira_group_service import JiraGroupService
        from src.clients.jira_project_service import JiraProjectService
        from src.clients.jira_reporting_service import JiraReportingService
        from src.clients.jira_tempo_service import JiraTempoService
        from src.clients.jira_user_service import JiraUserService
        from src.clients.jira_workflow_service import JiraWorkflowService
        from src.clients.jira_worklog_service import JiraWorklogService

        self.projects = JiraProjectService(self)
        self.workflows = JiraWorkflowService(self)
        self.agile = JiraAgileService(self)
        self.users = JiraUserService(self)
        self.worklogs = JiraWorklogService(self)
        self.tempo = JiraTempoService(self)
        self.groups = JiraGroupService(self)
        self.reporting = JiraReportingService(self)

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

        jira_mod = _import_real_jira_module()

        # Try to connect using token auth (Jira Cloud and Server PAT)
        try:
            logger.info("Attempting to connect to Jira using token authentication")
            self.jira = jira_mod.JIRA(
                server=self.jira_url,
                token_auth=self.jira_token,
                options={"verify": self.verify_ssl},
            )
            server_info = self.jira.server_info()
            logger.success(
                "Successfully connected to Jira server: %s (%s)",
                server_info.get("baseUrl"),
                server_info.get("version"),
            )
            # Explicit auth verification: /rest/api/2/myself must be 200 for valid PAT
            try:
                resp = self.jira._session.get(f"{self.base_url}/rest/api/2/myself")
                if resp.status_code != HTTP_OK:
                    logger.error(
                        "Auth verification failed on /myself: HTTP %s, headers=%s",
                        resp.status_code,
                        {k: v for k, v in resp.headers.items() if k.lower() in ("www-authenticate", "x-ausername")},
                    )
                    auth_msg = f"/myself auth check failed with HTTP {resp.status_code}"
                    raise JiraAuthenticationError(auth_msg)
                logger.info("Auth verification successful on /myself")
            except JiraAuthenticationError:
                raise
            except Exception as e:
                logger.warning("Auth verification error on /myself: %s", e)
            return
        except Exception as e:
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
            return
        except Exception as e2:
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
        """Thin delegator over ``self.projects.get_projects``."""
        return self.projects.get_projects()

    def get_issue_types(self) -> list[dict[str, Any]]:
        """Thin delegator over ``self.projects.get_issue_types``."""
        return self.projects.get_issue_types()

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
        # Include renderedFields to fetch comments, along with optional changelog
        expand_parts = []
        if expand_changelog:
            expand_parts.append("changelog")
        expand_parts.append("renderedFields")  # Includes comments
        expand = ",".join(expand_parts)

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

            return issue_data
        except Exception as e:
            error_msg = f"Failed to get issue details for {issue_key}: {e!s}"
            logger.exception(error_msg)
            if "issue does not exist" in str(e).lower() or "issue not found" in str(e).lower():
                msg = f"Issue {issue_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def get_users(self) -> list[dict[str, Any]]:
        """Thin delegator over ``self.users.get_users``."""
        return self.users.get_users()

    def get_user_info(self, user_key: str) -> dict[str, Any] | None:
        """Thin delegator over ``self.users.get_user_info``."""
        return self.users.get_user_info(user_key)

    def download_user_avatar(self, avatar_url: str) -> tuple[bytes, str] | None:
        """Thin delegator over ``self.users.download_user_avatar``."""
        return self.users.download_user_avatar(avatar_url)

    def get_groups(self) -> list[dict[str, Any]]:
        """Thin delegator over ``self.groups.get_groups``."""
        return self.groups.get_groups()

    def get_group_members(self, group_name: str) -> list[dict[str, Any]]:
        """Thin delegator over ``self.groups.get_group_members``."""
        return self.groups.get_group_members(group_name)

    def get_project_roles(self, project_key: str) -> list[dict[str, Any]]:
        """Thin delegator over ``self.projects.get_project_roles``."""
        return self.projects.get_project_roles(project_key)

    def get_project_permission_scheme(self, project_key: str) -> dict[str, Any]:
        """Thin delegator over ``self.projects.get_project_permission_scheme``."""
        return self.projects.get_project_permission_scheme(project_key)

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
            return issues.total
        except Exception as e:
            error_msg = f"Failed to get issue count for project {project_key}: {e!s}"
            logger.exception(error_msg)
            if "project does not exist" in str(e).lower() or "project not found" in str(e).lower():
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
            if "issue does not exist" in str(e).lower() or "issue not found" in str(e).lower():
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
                raise JiraApiError(
                    msg,
                )

            categories = response.json()
            logger.debug("Retrieved %s status categories from API", len(categories))

            # Use the status endpoint
            path = "/rest/api/2/status"
            response = self._make_request(path)
            if not response or response.status_code != HTTP_OK:
                msg = f"Failed to get statuses: HTTP {response.status_code if response else 'No response'}"
                raise JiraApiError(
                    msg,
                )

            statuses = response.json()
            logger.info("Retrieved %s statuses from Jira API", len(statuses))

            return statuses

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
                from urllib.parse import quote

                url = f"{base_url}/rest/api/2/issue/{quote(issue_key)}/properties/{quote(property_key)}"
                resp = self._session.get(url, timeout=15) if hasattr(self, "_session") else None
                if resp and resp.status_code == 200:
                    data = resp.json()
                    # property payload can be under 'value'
                    if isinstance(data, dict):
                        return data.get("value", data)  # type: ignore[return-value]
                return None
        except Exception:
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
                    error_msg = f"{error_msg} - {', '.join(error_json['errorMessages'])}"
                elif "errors" in error_json:
                    error_msg = f"{error_msg} - {error_json['errors']}"
            except Exception:
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
                raise JiraApiError(msg)

            if response.status_code != HTTP_OK:
                msg = f"Failed to get status categories: HTTP {response.status_code}"
                raise JiraApiError(msg)

            categories = response.json()
            logger.info("Retrieved %s status categories from Jira API", len(categories))

            return categories
        except Exception as e:
            error_msg = f"Failed to get status categories: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

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
                raise JiraApiError(msg)

            # Get a list of issue types
            issue_types = self.issue_type_cache or self.get_issue_types()
            if not issue_types:
                msg = "No issue types found to retrieve field metadata"
                raise JiraApiError(msg)

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
            raise JiraResourceNotFoundError(msg)

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
            custom_fields = [field for field in response if field.get("id", "").startswith("customfield_")]

            logger.debug("Retrieved %d custom fields from Jira", len(custom_fields))
            return custom_fields

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
        original_request = self.jira._session.request

        # Create patched method that checks for CAPTCHA
        def patched_request(method: str, url: str, **kwargs: object) -> Response:
            try:
                self.request_count += 1
                # dump requestcount
                logger.debug("Jira client request count: %s", self.request_count)
                response = original_request(method, url, **kwargs)

                # Check for CAPTCHA or other errors
                self._handle_response(response)

                return response
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
        self.jira._session.request = patched_request
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
            return self.jira._session.request(method, url, headers=headers, **kwargs)

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
    def get_tempo_accounts(self, *, expand: bool = False) -> list[dict[str, Any]]:
        """Delegate to ``self.tempo.get_tempo_accounts``."""
        return self.tempo.get_tempo_accounts(expand=expand)

    def get_tempo_customers(self) -> list[dict[str, Any]]:
        """Delegate to ``self.tempo.get_tempo_customers``."""
        return self.tempo.get_tempo_customers()

    def get_tempo_account_links_for_project(
        self,
        project_id: int,
    ) -> list[dict[str, Any]]:
        """Delegate to ``self.tempo.get_tempo_account_links_for_project``."""
        return self.tempo.get_tempo_account_links_for_project(project_id)

    def get_issue_link_types(self) -> list[dict[str, Any]]:
        """Delegate to ``self.worklogs.get_issue_link_types``."""
        return self.worklogs.get_issue_link_types()

    def get_work_logs_for_issue(self, issue_key: str) -> list[dict[str, Any]]:
        """Delegate to ``self.worklogs.get_work_logs_for_issue``."""
        return self.worklogs.get_work_logs_for_issue(issue_key)

    def get_all_work_logs_for_project(
        self,
        project_key: str,
        *,
        include_empty: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Delegate to ``self.worklogs.get_all_work_logs_for_project``."""
        return self.worklogs.get_all_work_logs_for_project(
            project_key,
            include_empty=include_empty,
        )

    def get_work_log_details(self, issue_key: str, work_log_id: str) -> dict[str, Any]:
        """Delegate to ``self.worklogs.get_work_log_details``."""
        return self.worklogs.get_work_log_details(issue_key, work_log_id)

    def get_tempo_work_logs(
        self,
        issue_key: str | None = None,
        project_key: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        user_key: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Delegate to ``self.tempo.get_tempo_work_logs``."""
        return self.tempo.get_tempo_work_logs(
            issue_key=issue_key,
            project_key=project_key,
            date_from=date_from,
            date_to=date_to,
            user_key=user_key,
            limit=limit,
        )

    def get_tempo_work_attributes(self) -> list[dict[str, Any]]:
        """Delegate to ``self.tempo.get_tempo_work_attributes``."""
        return self.tempo.get_tempo_work_attributes()

    # ---------------------------------------------------------------------- #
    # Workflow configuration helpers                                        #
    # ---------------------------------------------------------------------- #

    def get_workflow_schemes(self) -> list[dict[str, Any]]:
        """Thin delegator over ``self.workflows.get_workflow_schemes``."""
        return self.workflows.get_workflow_schemes()

    def get_workflow_transitions(self, workflow_name: str) -> list[dict[str, Any]]:
        """Thin delegator over ``self.workflows.get_workflow_transitions``."""
        return self.workflows.get_workflow_transitions(workflow_name)

    def get_workflow_statuses(self, workflow_name: str) -> list[dict[str, Any]]:
        """Thin delegator over ``self.workflows.get_workflow_statuses``."""
        return self.workflows.get_workflow_statuses(workflow_name)

    # ---------------------------------------------------------------------- #
    # Jira Software (Agile) helpers                                         #
    # ---------------------------------------------------------------------- #

    def get_boards(self) -> list[dict[str, Any]]:
        """Thin delegator over ``self.agile.get_boards``."""
        return self.agile.get_boards()

    def get_board_configuration(self, board_id: int) -> dict[str, Any]:
        """Thin delegator over ``self.agile.get_board_configuration``."""
        return self.agile.get_board_configuration(board_id)

    def get_board_sprints(self, board_id: int) -> list[dict[str, Any]]:
        """Thin delegator over ``self.agile.get_board_sprints``."""
        return self.agile.get_board_sprints(board_id)

    # ---------------------------------------------------------------------- #
    # Reporting helpers (filters & dashboards)                               #
    # ---------------------------------------------------------------------- #

    def get_filters(self) -> list[dict[str, Any]]:
        """Thin delegator over ``self.reporting.get_filters``."""
        return self.reporting.get_filters()

    def get_dashboards(self) -> list[dict[str, Any]]:
        """Thin delegator over ``self.reporting.get_dashboards``."""
        return self.reporting.get_dashboards()

    def get_dashboard_details(self, dashboard_id: int) -> dict[str, Any]:
        """Thin delegator over ``self.reporting.get_dashboard_details``."""
        return self.reporting.get_dashboard_details(dashboard_id)

    def get_tempo_all_work_logs_for_project(
        self,
        project_key: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Delegate to ``self.tempo.get_tempo_all_work_logs_for_project``."""
        return self.tempo.get_tempo_all_work_logs_for_project(
            project_key,
            date_from=date_from,
            date_to=date_to,
        )

    def get_tempo_work_log_by_id(self, tempo_worklog_id: str) -> dict[str, Any]:
        """Delegate to ``self.tempo.get_tempo_work_log_by_id``."""
        return self.tempo.get_tempo_work_log_by_id(tempo_worklog_id)

    def get_tempo_user_work_logs(
        self,
        user_key: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Delegate to ``self.tempo.get_tempo_user_work_logs``."""
        return self.tempo.get_tempo_user_work_logs(
            user_key,
            date_from=date_from,
            date_to=date_to,
        )

    def get_tempo_time_entries(
        self,
        project_keys: list[str] | None = None,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        user_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Delegate to ``self.tempo.get_tempo_time_entries``."""
        return self.tempo.get_tempo_time_entries(
            project_keys,
            date_from=date_from,
            date_to=date_to,
            user_key=user_key,
        )

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

    def _fetch_issues_batch(self, issue_keys: list[str], **kwargs: object) -> dict[str, Issue]:
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
        return {project["key"]: project for project in all_projects if project["key"] in project_keys}

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
        """Thin delegator over ``self.users.batch_get_users_by_keys``."""
        return self.users.batch_get_users_by_keys(user_keys)

    def get_project_metadata_enhanced(self, project_key: str) -> dict[str, Any]:
        """Thin delegator over ``self.projects.get_project_metadata_enhanced``."""
        return self.projects.get_project_metadata_enhanced(project_key)
