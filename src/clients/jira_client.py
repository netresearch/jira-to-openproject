"""
Jira API client for the migration project.
Provides access to Jira resources through the API.
"""

import sys
import time
from typing import Any

from jira import JIRA, Issue
from jira.client import ResultList
from requests import Response

from src import config

# Get logger
logger = config.logger


class JiraClient:
    """
    Jira client for API interactions.

    This class provides methods for interacting with the Jira API,
    including project/issue operations and Tempo plugin integration.
    """

    _instance = None

    @classmethod
    def get_instance(cls):
        """
        Get singleton instance of JiraClient.

        Returns:
            JiraClient: The singleton instance
        """
        if cls._instance is None:
            cls._instance = JiraClient()
        return cls._instance

    def __init__(self):
        """Initialize the Jira client."""
        # Only initialize once
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        # Get connection details from config
        self.jira_url = config.jira_config.get("url", "")
        self.jira_username = config.jira_config.get("username", "")
        self.jira_api_token = config.jira_config.get("api_token", "")
        self.verify_ssl = config.jira_config.get("verify_ssl", True)

        # ScriptRunner configuration
        self.scriptrunner_enabled = config.jira_config.get("scriptrunner", {}).get(
            "enabled", False
        )
        self.scriptrunner_custom_field_options_endpoint = config.jira_config.get(
            "scriptrunner", {}
        ).get("custom_field_options_endpoint", "")

        # Initialize client
        self.jira = None
        self.request_count = 0
        self.period_start = time.time()
        self.base_url = self.jira_url.rstrip("/")

        # Cache fields
        self.server_info = {}
        self.captcha_challenge = False

        self.project_cache = None
        self.issue_type_cache = None
        self.field_options_cache = {}

        self.__connect()
        self._patch_jira_client()

    def __connect(self):
        """Connect to the Jira API and set the connected status."""
        try:
            # Try to connect using token auth (Jira Cloud and Server PAT)
            logger.info("Attempting to connect to Jira using token authentication")
            self.jira = JIRA(server=self.jira_url, token_auth=self.jira_api_token)
            # Verify connection by getting server info or current user
            server_info = self.jira.server_info()
            logger.success(
                f"Successfully connected to Jira server: {server_info.get('baseUrl')} ({server_info.get('version')})"
            )
            return True  # Success - return True
        except Exception as e:
            logger.warning(f"Token authentication failed: {str(e)}")

        try:
            # Fall back to basic auth (Jira Server)
            logger.info("Attempting to connect to Jira using basic authentication")
            self.jira = JIRA(
                server=self.jira_url,
                basic_auth=(self.jira_username, self.jira_api_token),
            )
            server_info = self.jira.server_info()
            logger.success(
                f"Successfully connected to Jira server: {server_info.get('baseUrl')} ({server_info.get('version')})"
            )
            return True  # Success - return True
        except Exception as e2:
            logger.warning(f"Basic authentication failed: {str(e2)}")

        # If all methods failed
        logger.error(
            f"All authentication methods failed for Jira connection to {self.jira_url}"
        )
        return False  # Failure - return False

    def get_projects(self) -> list[dict[str, Any]]:
        """Get all projects from Jira."""
        try:
            projects = self.jira.projects()
            return [
                {"key": project.key, "name": project.name, "id": project.id}
                for project in projects
            ]
        except Exception as e:
            logger.error(f"Failed to get projects: {str(e)}")
            return []

    def get_issue_types(self) -> list[dict[str, Any]]:
        """Get all issue types from Jira."""
        try:
            issue_types = self.jira.issue_types()
            return [
                {
                    "id": issue_type.id,
                    "name": issue_type.name,
                    "description": issue_type.description,
                }
                for issue_type in issue_types
            ]
        except Exception as e:
            logger.error(f"Failed to get issue types: {str(e)}")
            return []

    def get_all_issues_for_project(
        self, project_key: str, expand_changelog: bool = True
    ) -> list[Issue]:
        """Gets all issues for a specific project, handling pagination.

        Args:
            project_key: The key of the project.
            expand_changelog: Whether to expand the changelog for history.

        Returns:
            A list of Jira issue objects.
        """
        all_issues = []
        start_at = 0
        max_results = 100  # Fetch in batches of 100 (adjust as needed)
        # Surround project key with quotes to handle reserved words
        jql = f'project = "{project_key}" ORDER BY created ASC'
        # Specify fields needed, or use None to get all (can be slower)
        fields = None  # Get all fields for simplicity, though might be less efficient
        expand = "changelog" if expand_changelog else None

        logger.notice(f"Fetching all issues for project '{project_key}'...")

        while True:
            try:
                logger.debug(
                    f"Fetching issues for {project_key}: startAt={start_at}, maxResults={max_results}"
                )

                issues_page: ResultList[Issue] = self.jira.search_issues(
                    jql,
                    startAt=start_at,
                    maxResults=max_results,
                    fields=fields,
                    expand=expand,
                    json_result=False,  # Get jira.Issue objects
                )

                if not issues_page:
                    logger.debug(
                        f"No more issues found for {project_key} at startAt={start_at}"
                    )
                    break  # Exit loop if no more issues are returned

                all_issues.extend(issues_page)
                logger.debug(
                    f"Fetched {len(issues_page)} issues (total: {len(all_issues)}) for {project_key}"
                )

                # Check if this was the last page
                if len(issues_page) < max_results:
                    break

                start_at += len(issues_page)
                # Optional small delay between pages
                # time.sleep(0.1)

            except Exception as e:
                logger.exception(
                    f"Failed to get issues page for project {project_key} at startAt={start_at}: {str(e)}",
                    exc_info=True,
                )
                return all_issues

        logger.info(
            f"Finished fetching {len(all_issues)} issues for project '{project_key}'."
        )
        return all_issues

    def get_issue_details(self, issue_key: str) -> dict[str, Any] | None:
        """Get detailed information about a specific issue."""
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
            logger.error(f"Failed to get issue details for {issue_key}: {str(e)}")
            return None

    def get_users(self) -> list[dict[str, Any]]:
        """Get all users from Jira."""
        try:
            users = self.jira.search_users(
                user=".", includeInactive=True, startAt=0, maxResults=1000
            )

            logger.info(f"Retrieved {len(users)} users from Jira API")

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
            logger.error(f"Failed to get users: {str(e)}")
            return []

    def get_issue_count(self, project_key: str) -> int:
        """
        Get the total number of issues in a project.

        Args:
            project_key: The key of the Jira project to count issues from

        Returns:
            The total number of issues in the project
        """
        try:
            # Use JQL to count issues in the project - surround with quotes to handle reserved words
            jql = f'project="{project_key}"'

            # Using fields="key" and expand='' minimizes data transfer
            # when we only need to get the total count
            issues = self.jira.search_issues(
                jql,
                maxResults=1,
                fields="key",
                expand="",  # Explicitly disable field expansion for faster retrieval
            )

            # Get total from the response
            return issues.total
        except Exception as e:
            logger.error(
                f"Failed to get issue count for project {project_key}: {str(e)}"
            )
            return 0

    def get_issue_watchers(self, issue_key: str) -> list[dict[str, Any]]:
        """
        Get the watchers for a specific Jira issue.

        Args:
            issue_key: The key of the issue to get watchers for (e.g., 'PROJECT-123')

        Returns:
            List of watcher dictionaries containing at least the 'name' field
        """
        try:
            # Use the JIRA library's watchers() method
            result = self.jira.watchers(issue_key)

            if not result:
                logger.debug(f"No watchers found for issue {issue_key}")
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
            logger.error(f"Failed to get watchers for issue {issue_key}: {str(e)}")
            return []

    def get_all_statuses(self) -> list[dict[str, Any]]:
        """
        Get all statuses from Jira.

        Returns:
            List of status dictionaries with id, name, and category information
        """
        try:
            # Use the JIRA library to get statuses
            statuses = []

            # Get all status categories first
            status_categories = {}
            try:
                # We'll use issue search to extract available statuses since direct REST API may fail
                # Search for a few recent issues to find statuses in use
                issues = self.jira.search_issues("order by created DESC", maxResults=50)

                logger.debug(f"Retrieving statuses from {len(issues)} sample issues")

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
                                    getattr(status, "statusCategory", None), "id", None
                                ),
                                "key": getattr(
                                    getattr(status, "statusCategory", None), "key", None
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

            except Exception as e:
                logger.warning(
                    f"Error retrieving statuses from sample issues: {str(e)}"
                )

                # Fallback: Try to use the status_categories endpoint
                try:
                    path = "/rest/api/2/statuscategory"

                    response = self._make_request(path)
                    if not response or response.status_code != 200:
                        logger.warning(
                            f"Failed to get status categories: HTTP "
                            f"{response.status_code if response else 'No response'}"
                        )
                        return []

                    categories = response.json()
                    logger.debug(
                        f"Retrieved {len(categories)} status categories from API"
                    )

                    for category in categories:
                        category_id = category.get("id")
                        status_categories[category_id] = category

                except Exception as e2:
                    logger.warning(
                        f"Fallback to status_categories endpoint failed: {str(e2)}"
                    )

            logger.info(f"Retrieved {len(statuses)} statuses from Jira")

            return statuses
        except Exception as e:
            logger.error(f"Failed to get statuses: {str(e)}")
            return []

    def _handle_response(self, response):
        """
        Check response for CAPTCHA challenge and raise appropriate exception if found.

        Args:
            response: The HTTP response to check

        Returns:
            The original response if no CAPTCHA challenge is detected

        Raises:
            Exception with CAPTCHA information if a CAPTCHA challenge is detected
        """
        # Check for CAPTCHA challenge header
        if "X-Authentication-Denied-Reason" in response.headers:
            header_value = response.headers["X-Authentication-Denied-Reason"]
            if "CAPTCHA_CHALLENGE" in header_value:
                # Extract login URL if present
                login_url = self.jira_url + "/login.jsp"  # Default
                if "; login-url=" in header_value:
                    login_url = header_value.split("; login-url=")[1].strip()

                logger.error("CAPTCHA challenge detected from Jira!")
                logger.error(
                    f"Please open {login_url} in your web browser and log in to resolve the CAPTCHA challenge"
                )
                logger.error("After resolving the CAPTCHA, restart this application")
                logger.debug(f"Jira client request count: {self.request_count}")

                sys.exit(1)

        return

    def get_status_categories(self) -> list[dict[str, Any]]:
        """
        Get all status categories from Jira.

        Returns:
            List of status category dictionaries
        """
        try:
            # Use the REST API to get all status categories
            path = "/rest/api/2/statuscategory"

            # Make the request using our generic method
            response = self._make_request(path)
            if not response:
                logger.error("Failed to get status categories (request failed)")
                return []

            if response.status_code != 200:
                logger.error(
                    f"Failed to get status categories: HTTP {response.status_code}"
                )
                return []

            categories = response.json()
            logger.info(f"Retrieved {len(categories)} status categories from Jira API")

            return categories
        except Exception as e:
            logger.error(f"Failed to get status categories: {str(e)}")
            return []

    def _get_field_metadata_via_createmeta(
        self, field_id: str
    ) -> dict[str, Any] | None:
        """
        Get field metadata using the issue/createmeta endpoint.

        Args:
            field_id: The ID of the custom field to retrieve metadata for

        Returns:
            Dictionary containing field metadata or None if not found
        """
        logger.debug(
            f"Attempting to get field metadata for {field_id} using createmeta endpoint"
        )

        try:
            # We no longer need to check the cache here as it's done in the calling method

            # Get a list of projects
            projects = self.project_cache or self.get_projects()
            if not projects:
                logger.warning("No projects found to retrieve field metadata")
                return None

            # Get a list of issue types
            issue_types = self.issue_type_cache or self.get_issue_types()
            if not issue_types:
                logger.warning("No issue types found to retrieve field metadata")
                return None

            # Try with each project/issue type combination until we find field metadata
            for project in projects:
                project_key = project.get("key")
                for issue_type in issue_types:
                    issue_type_id = issue_type.get("id")

                    # Use the createmeta endpoint with expansion for fields
                    path = f"/rest/api/2/issue/createmeta/{project_key}/issuetypes/{issue_type_id}"
                    logger.debug(
                        f"Trying to get field metadata using project {project_key}"
                        f" and issue type {issue_type.get('name')}"
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
            return self.field_options_cache.get(field_id)

        except Exception as e:
            logger.warning(
                f"Error getting field metadata via createmeta endpoint: {str(e)}"
            )
            return None

    def get_field_metadata(self, field_id: str) -> dict[str, Any]:
        """
        Get metadata for a specific custom field, including allowed values.
        Automatically uses ScriptRunner when available for better performance.

        Args:
            field_id: ID of the custom field

        Returns:
            Dictionary containing field metadata
        """
        # Check if we already have this field in cache
        if field_id in self.field_options_cache:
            return self.field_options_cache[field_id]

        # Not in cache, we need to fetch it
        result = {}

        # Try ScriptRunner first if enabled
        if (
            self.scriptrunner_enabled
            and self.scriptrunner_custom_field_options_endpoint
        ):
            scriptrunner_cache_key = "_scriptrunner_all_fields"

            # If we don't have the full ScriptRunner data cached yet, fetch it
            if scriptrunner_cache_key not in self.field_options_cache:
                try:
                    response = self._make_request(
                        self.scriptrunner_custom_field_options_endpoint
                    )
                    if response and response.status_code == 200:
                        # Cache all fields data
                        self.field_options_cache[scriptrunner_cache_key] = (
                            response.json()
                        )
                except Exception as e:
                    logger.error(f"Error fetching ScriptRunner data: {e}")

            # Now try to get the specific field from the cached ScriptRunner data
            if scriptrunner_cache_key in self.field_options_cache:
                all_fields_data = self.field_options_cache[scriptrunner_cache_key]
                field_data = all_fields_data.get(field_id)

                if field_data and field_data.get("options"):
                    options = field_data.get("options", [])
                    result = {"allowedValues": [{"value": value} for value in options]}

        # If we couldn't get data from ScriptRunner, use the standard method
        if not result:
            result = self._get_field_metadata_via_createmeta(field_id) or {}

        # Cache the result and return it
        self.field_options_cache[field_id] = result
        return result

    def _patch_jira_client(self) -> None:
        """
        Patch the JIRA client to catch CAPTCHA challenges.
        This adds CAPTCHA detection to all API requests made through the JIRA library.
        """
        # Store original _session.request method
        original_request = self.jira._session.request

        # Create patched method that checks for CAPTCHA
        def patched_request(method: str, url: str, **kwargs: Any) -> Response | None:
            try:
                self.request_count += 1
                # dump requestcount
                logger.debug(f"Jira client request count: {self.request_count}")
                response = original_request(method, url, **kwargs)
            except Exception as e:
                logger.error(f"Error during API request to {url}: {str(e)}")

            self._handle_response(response)
            return response

        # Replace the method with our patched version
        self.jira._session.request = patched_request
        logger.debug("JIRA client patched to catch CAPTCHA challenges")

    def _make_request(
        self,
        path: str,
        method: str = "GET",
        content_type: str = "application/json",
        **kwargs: Any,
    ) -> Response | None:
        """
        Generic method to make API requests with proper error handling and CAPTCHA detection.

        Args:
            path: API path relative to base_url
            method: HTTP method (GET, POST, etc.)
            content_type: Content type for request headers
            **kwargs: Additional arguments to pass to jira._session.request

        Returns:
            Response object if successful, or None if failed
        """

        # Construct full URL
        url = f"{self.base_url}{path}"

        # Get authentication headers
        headers = {}
        # Add content headers if requested
        if content_type:
            headers.update(
                {
                    "Content-Type": content_type,
                    "Accept": content_type,
                }
            )

        # Add any headers passed in kwargs
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))

        try:
            response = self.jira._session.request(
                method, url, headers=headers, **kwargs
            )

            return response
        except Exception as e:
            logger.error(f"Error during API request to {url}: {str(e)}")
            return None

    # Tempo API methods
    def get_tempo_accounts(self, expand: bool = False) -> list[dict[str, Any]] | None:
        """
        Retrieves all Tempo accounts.

        Args:
            expand: Whether to expand account details (passed as query param).

        Returns:
            A list of Tempo accounts or None if an error occurred.
        """
        path = "/rest/tempo-accounts/1/account"
        params = {}
        if expand:
            params["expand"] = "true"

        logger.info("Fetching Tempo accounts")
        response = self._make_request(path, params=params)
        accounts = response.json() if response and response.status_code == 200 else None

        if accounts is not None:
            logger.info(f"Successfully retrieved {len(accounts)} Tempo accounts.")
        else:
            logger.error("Failed to retrieve Tempo accounts.")
        return accounts

    def get_tempo_customers(self) -> list[dict[str, Any]] | None:
        """
        Retrieves all Tempo customers (often used for Companies).

        Returns:
            A list of Tempo customers or None if an error occurred.
        """
        path = "/rest/tempo-accounts/1/customer"
        logger.info("Fetching Tempo customers (Companies)")
        response = self._make_request(path)
        customers = (
            response.json() if response and response.status_code == 200 else None
        )

        if customers is not None:
            logger.info(f"Successfully retrieved {len(customers)} Tempo customers.")
        else:
            logger.error("Failed to retrieve Tempo customers.")
        return customers

    def get_tempo_account_links_for_project(
        self, project_id: int
    ) -> list[dict[str, Any]] | None:
        """
        Retrieves Tempo account links for a specific Jira project.
        NOTE: This endpoint might differ or require specific Tempo versions/configurations.
              Adjust the endpoint based on Tempo API documentation if needed.

        Args:
            project_id: The Jira project ID.

        Returns:
            A list of account links or None if an error occurred.
        """
        # Use the Tempo account-by-project endpoint for project-specific account lookup
        path = f"/rest/tempo-accounts/1/account/project/{project_id}"

        logger.debug(f"Fetching Tempo account links for project '{project_id}'")
        response = self._make_request(path)
        links = response.json() if response and response.status_code == 200 else None

        if links is not None:
            logger.debug(
                f"Successfully retrieved {len(links)} account links for project {project_id}."
            )
        else:
            logger.warning(
                f"Failed to retrieve account links for project {project_id}. "
                f"This might be expected if the endpoint is incorrect or no links exist."
            )
        return links

    def get_issue_link_types(self) -> list[dict[str, Any]] | None:
        """
        Retrieves all issue link types configured in Jira.

        Returns:
            A list of issue link types or None if an error occurred.
        """
        try:
            logger.info("Fetching Jira issue link types")
            result = self.jira.issue_link_types()
            logger.info(f"Successfully retrieved {len(result)} issue link types")

            # Convert issue link type objects to dictionaries
            result = [
                {
                    "id": link_type.id,
                    "name": link_type.name,
                    "inward": link_type.inward,
                    "outward": link_type.outward,
                    "self": getattr(link_type, "self", None),
                }
                for link_type in result
            ]

            return result
        except Exception as e:
            logger.error(f"Failed to retrieve issue link types: {str(e)}")
