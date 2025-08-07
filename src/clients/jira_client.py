"""Jira API client for the migration project.

Provides a clean, exception-based interface for Jira resource access.
Enhanced with performance optimizations including batch operations,
caching, and parallel processing.
"""

import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from jira import JIRA, Issue
from requests import Response

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

# Get logger
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

    def __init__(self, **kwargs) -> None:
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

        except ConfigurationValidationError as e:
            logger.exception(f"JiraClient configuration validation failed: {e}")
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

        # Try to connect using token auth (Jira Cloud and Server PAT)
        try:
            logger.info("Attempting to connect to Jira using token authentication")
            self.jira = JIRA(server=self.jira_url, token_auth=self.jira_token)
            server_info = self.jira.server_info()
            logger.success(
                "Successfully connected to Jira server: %s (%s)",
                server_info.get("baseUrl"),
                server_info.get("version"),
            )
            return  # Success
        except Exception as e:
            error_msg = f"Token authentication failed: {e!s}"
            logger.warning(error_msg)
            connection_errors.append(error_msg)

        # Try basic authentication
        try:
            self.jira = JIRA(
                server=self.jira_url,
                basic_auth=(self.jira_username, self.jira_token),
                options={"verify": self.verify_ssl},
            )
            self._patch_jira_client()
            logger.debug(
                "Successfully connected using basic authentication",
            )
            return  # Success
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
        """Get all projects from Jira.

        Returns:
            List of project dictionaries with key, name, and ID

        Raises:
            JiraApiError: If the API request fails

        """
        if not self.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            projects = self.jira.projects()
            result = [
                {"key": project.key, "name": project.name, "id": project.id}
                for project in projects
            ]

            if not projects:
                logger.warning("No projects found in Jira")

            return result
        except Exception as e:
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

            return result
        except Exception as e:
            error_msg = f"Failed to get issue types: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_all_issues_for_project(
        self,
        project_key: str,
        expand_changelog: bool = True,
    ) -> list[Issue]:
        """Gets all issues for a specific project, handling pagination.

        Args:
            project_key: The key of the project.
            expand_changelog: Whether to expand the changelog for history.

        Returns:
            A list of Jira issue objects.

        Raises:
            JiraApiError: If the API request fails
            JiraResourceNotFoundError: If the project is not found

        """
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

            return issue_data
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

            # Convert user objects to dictionaries
            return [
                {
                    "key": getattr(user, "key", None),
                    "name": getattr(user, "name", None),
                    "displayName": getattr(user, "displayName", None),
                    "emailAddress": getattr(user, "emailAddress", ""),
                    "active": getattr(user, "active", True),
                }
                for user in users
            ]

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
                return {
                    "accountId": getattr(user, "accountId", None),
                    "displayName": getattr(user, "displayName", None),
                    "emailAddress": getattr(user, "emailAddress", ""),
                    "active": getattr(user, "active", True),
                    "key": getattr(user, "key", None),
                    "name": getattr(user, "name", None),
                }

            return None

        except Exception as e:
            # Log at debug level for not found cases, exception level for others
            if "404" in str(e) or "not found" in str(e).lower():
                logger.debug("User not found: %s", user_key)
                return None
            error_msg = f"Failed to get user info for {user_key}: {e!s}"
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
            return issues.total
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
            if not response or response.status_code != 200:
                msg = f"Failed to get status categories: HTTP {response.status_code if response else 'No response'}"
                raise JiraApiError(
                    msg,
                )

            categories = response.json()
            logger.debug("Retrieved %s status categories from API", len(categories))

            # Use the status endpoint
            path = "/rest/api/2/status"
            response = self._make_request(path)
            if not response or response.status_code != 200:
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
        if response.status_code >= 400:
            error_msg = f"HTTP Error {response.status_code}: {response.reason}"
            try:
                error_json = response.json()
                if "errorMessages" in error_json:
                    error_msg = (
                        f"{error_msg} - {', '.join(error_json['errorMessages'])}"
                    )
                elif "errors" in error_json:
                    error_msg = f"{error_msg} - {error_json['errors']}"
            except Exception:
                pass

            if response.status_code == 404:
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

            if response.status_code != 200:
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

                    # Use the createmeta endpoint with expansion for fields
                    path = f"/rest/api/2/issue/createmeta/{project_key}/issuetypes/{issue_type_id}"
                    logger.debug(
                        f"Trying to get field metadata using project {project_key}"
                        f" and issue type {issue_type.get('name')}",
                    )

                    meta_response = self._make_request(path)
                    if not meta_response or meta_response.status_code != 200:
                        continue

                    meta_data = meta_response.json()

                    # Check for fields in the response structure
                    if "values" in meta_data:
                        values = meta_data.get("values", [])
                        if isinstance(values, list):
                            for value in values:
                                field_id_from_response = value.get("fieldId")
                                if field_id_from_response:
                                    # Store in cache for future use
                                    self.field_options_cache[field_id_from_response] = (
                                        value
                                    )

                                    # If this is the field we're looking for, return it immediately
                                    if field_id_from_response == field_id:
                                        return value

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
            custom_fields = [
                field
                for field in response
                if field.get("id", "").startswith("customfield_")
            ]

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
        """Generic method to make API requests with proper error handling and CAPTCHA detection.

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
    def get_tempo_accounts(self, expand: bool = False) -> list[dict[str, Any]]:
        """Retrieve all Tempo accounts.

        Args:
            expand: Whether to expand account details (passed as query param).

        Returns:
            A list of Tempo accounts

        Raises:
            JiraApiError: If the API request fails

        """
        path = "/rest/tempo-accounts/1/account"
        params = {
            "expand": "true",
            "skipArchived": "false",
        }

        logger.info("Fetching Tempo accounts")
        try:
            response = self._make_request(path, params=params)
            if response.status_code != 200:
                msg = f"Failed to retrieve Tempo accounts: HTTP {response.status_code}"
                raise JiraApiError(msg)

            accounts = response.json()
            logger.info("Successfully retrieved %s Tempo accounts.", len(accounts))
            return accounts
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
            if response.status_code != 200:
                msg = f"Failed to retrieve Tempo customers: HTTP {response.status_code}"
                raise JiraApiError(msg)

            customers = response.json()
            logger.info("Successfully retrieved %s Tempo customers.", len(customers))
            return customers
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
            if response.status_code == 404:
                logger.warning("No account links found for project %s.", project_id)
                return []

            if response.status_code != 200:
                msg = f"Failed to retrieve account links: HTTP {response.status_code}"
                raise JiraApiError(msg)

            links = response.json()
            logger.debug(
                "Successfully retrieved %s account links for project %s.",
                len(links),
                project_id,
            )
            return links
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

            return result
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
            return result

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
        include_empty: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Get all work logs for all issues in a project.

        Args:
            project_key: The key of the project to get work logs for
            include_empty: Whether to include issues with no work logs

        Returns:
            Dictionary mapping issue keys to their work logs

        Raises:
            JiraResourceNotFoundError: If the project is not found
            JiraApiError: If the API request fails

        """
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
                        self.rate_limiter.record_response(response_time, 200)

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
                            404,
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
                            500,
                        )
                        continue

            logger.info(
                "Work log extraction complete for project '%s': "
                "%s issues with work logs, %s total work logs",
                project_key,
                issues_with_logs,
                total_work_logs,
            )

            return work_logs_by_issue

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

            return work_log_data

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

    def get_tempo_work_logs(
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

            response = self.jira._session.get(
                f"{self.base_url}{path}",
                params=params,
            )

            if response.status_code != 200:
                msg = f"Failed to retrieve Tempo work logs: HTTP {response.status_code}"
                logger.error(msg)
                raise JiraApiError(msg)

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

            return enhanced_work_logs

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

            response = self.jira._session.get(
                f"{self.base_url}{path}",
            )

            if response.status_code != 200:
                msg = f"Failed to retrieve Tempo work attributes: HTTP {response.status_code}"
                logger.error(msg)
                raise JiraApiError(msg)

            attributes = response.json()
            logger.info(
                "Successfully retrieved %s Tempo work attributes",
                len(attributes),
            )

            return attributes

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo work attributes: {e!s}"
            logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_all_work_logs_for_project(
        self,
        project_key: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all Tempo work logs for a project with pagination handling.

        Args:
            project_key: The key of the project to get work logs for
            date_from: Optional start date in YYYY-MM-DD format
            date_to: Optional end date in YYYY-MM-DD format

        Returns:
            List of all Tempo work logs for the project

        Raises:
            JiraApiError: If the API request fails

        """
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
                response = self.jira._session.get(
                    f"{self.base_url}{path}",
                    params=params,
                )
                response_time = time.time() - request_start

                # Record response for rate limiting adaptation
                self.rate_limiter.record_response(response_time, response.status_code)

                if response.status_code != 200:
                    msg = f"Failed to retrieve Tempo work logs for project {project_key}: HTTP {response.status_code}"
                    logger.error(msg)
                    raise JiraApiError(msg)

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
            return all_work_logs

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

            response = self.jira._session.get(
                f"{self.base_url}{path}",
            )

            if response.status_code == 404:
                msg = f"Tempo work log {tempo_worklog_id} not found"
                raise JiraResourceNotFoundError(msg)
            if response.status_code != 200:
                msg = f"Failed to retrieve Tempo work log {tempo_worklog_id}: HTTP {response.status_code}"
                logger.error(msg)
                raise JiraApiError(msg)

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
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all Tempo work logs for a specific user.

        Args:
            user_key: The user key to get work logs for
            date_from: Optional start date in YYYY-MM-DD format
            date_to: Optional end date in YYYY-MM-DD format

        Returns:
            List of Tempo work logs for the user

        Raises:
            JiraApiError: If the API request fails

        """
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

    def get_tempo_time_entries(
        self,
        project_keys: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        user_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get Tempo time entries with enhanced metadata for migration.

        This method provides a migration-friendly interface to Tempo work logs,
        combining work logs from multiple projects if specified.

        Args:
            project_keys: List of project keys to extract entries for (None for all)
            date_from: Start date for extraction (YYYY-MM-DD format)
            date_to: End date for extraction (YYYY-MM-DD format)
            user_key: Specific user key to filter by

        Returns:
            List of Tempo time entry dictionaries with migration metadata

        Raises:
            JiraApiError: If the API request fails

        """
        try:
            logger.info(
                f"Fetching Tempo time entries for projects: {project_keys}, "
                f"date range: {date_from} to {date_to}, user: {user_key}",
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
                            f"Retrieved {len(project_work_logs)} entries for project {project_key}",
                        )

                    except Exception as e:
                        logger.warning(
                            f"Failed to get Tempo entries for project {project_key}: {e}",
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
                    "extraction_timestamp": datetime.now().isoformat(),
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
                f"Retrieved {len(enhanced_entries)} Tempo time entries total",
            )
            return enhanced_entries

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

    def _fetch_issues_batch(self, issue_keys: list[str], **kwargs) -> dict[str, Issue]:
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
        except Exception as e:
            logger.exception(
                f"Batch issue fetch failed for {len(issue_keys)} issues: {e}",
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
