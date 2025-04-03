"""
Jira API client for the migration project.
Provides access to Jira resources through the API.
"""

from jira import JIRA
import requests
import time
import base64
from typing import Dict, List, Any, Optional
from .. import config

# Get logger
logger = config.logger


class JiraClient:
    """Client for interacting with the Jira API."""

    # Singleton instance
    _instance = None

    def __new__(cls, *args, **kwargs):
        """Implement singleton pattern."""
        if cls._instance is None:
            logger.debug("Creating new JiraClient instance")
            cls._instance = super(JiraClient, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the Jira client."""
        # Only initialize once
        if getattr(self, "_initialized", False):
            logger.debug("Using existing JiraClient instance")
            return

        # Get configuration
        self.jira_config = config.jira_config
        self.migration_config = config.migration_config

        # Extract required values
        self.jira_url = self.jira_config.get("url")
        self.jira_username = self.jira_config.get("username")
        self.jira_api_token = self.jira_config.get("api_token")

        # Performance settings
        self.rate_limit_requests = int(self.migration_config.get("rate_limit_requests", 1000))
        self.rate_limit_period = int(self.migration_config.get("rate_limit_period", 60))

        # Initialize client
        self.jira = None
        self.connected = False # Initialize connected status
        self.session = requests.Session()
        self.request_count = 0
        self.period_start = time.time()
        self.base_url = self.jira_url.rstrip("/")

        # Attempt to connect immediately upon initialization
        self.connect()

        # Mark as initialized
        self._initialized = True

    def connect(self):
        """Connect to the Jira API and set the connected status."""
        # Check if we're already connected to avoid duplicate messages
        if self.connected:
            return True

        self.connected = False # Assume failure initially
        try:
            # Try to connect using token auth (Jira Cloud and Server PAT)
            logger.info("Attempting to connect to Jira using token authentication")
            self.jira = JIRA(server=self.jira_url, token_auth=self.jira_api_token)
            # Verify connection by getting server info or current user
            server_info = self.jira.server_info()
            logger.success(f"Successfully connected to Jira server: {server_info.get('baseUrl')} ({server_info.get('version')})")
            self.connected = True
            return True # Success - return True
        except Exception as e:
            logger.warning(f"Token authentication failed: {str(e)}")

        try:
            # Fall back to basic auth (Jira Server)
            logger.info("Attempting to connect to Jira using basic authentication")
            self.jira = JIRA(
                server=self.jira_url, basic_auth=(self.jira_username, self.jira_api_token)
            )
            server_info = self.jira.server_info()
            logger.success(f"Successfully connected to Jira server: {server_info.get('baseUrl')} ({server_info.get('version')})")
            self.connected = True
            return True # Success - return True
        except Exception as e2:
            logger.warning(f"Basic authentication failed: {str(e2)}")

        # If all methods failed
        logger.error(f"All authentication methods failed for Jira connection to {self.jira_url}")
        return False # Failure - return False

    def _rate_limit(self):
        """Implement rate limiting for API requests."""
        current_time = time.time()
        time_passed = current_time - self.period_start

        # Reset counter if period has passed
        if time_passed > self.rate_limit_period:
            self.request_count = 0
            self.period_start = current_time
            return

        # If we've hit the rate limit, sleep until the period is over
        if self.request_count >= self.rate_limit_requests:
            sleep_time = self.rate_limit_period - time_passed
            if sleep_time > 0:
                logger.debug(f"Rate limit reached, sleeping for {sleep_time:.2f}s")
                time.sleep(sleep_time)
                self.request_count = 0
                self.period_start = time.time()

    def get_projects(self) -> List[Dict[str, Any]]:
        """Get all projects from Jira."""
        self._rate_limit()
        try:
            projects = self.jira.projects()
            self.request_count += 1
            return [
                {"key": project.key, "name": project.name, "id": project.id}
                for project in projects
            ]
        except Exception as e:
            logger.error(f"Failed to get projects: {str(e)}")
            return []

    def get_issue_types(self) -> List[Dict[str, Any]]:
        """Get all issue types from Jira."""
        self._rate_limit()
        try:
            issue_types = self.jira.issue_types()
            self.request_count += 1
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

    def get_issues(
        self, project_key: str, start_at: int = 0, max_results: int = 50
    ) -> List[Dict[str, Any]]:
        """Get issues for a specific project with pagination."""
        self._rate_limit()
        try:
            issues = self.jira.search_issues(
                f"project={project_key} ORDER BY created ASC",
                startAt=start_at,
                maxResults=max_results,
            )
            self.request_count += 1
            return [
                {
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
                }
                for issue in issues
            ]
        except Exception as e:
            logger.error(f"Failed to get issues for project {project_key}: {str(e)}")
            return []

    def get_issue_details(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a specific issue."""
        self._rate_limit()
        try:
            issue = self.jira.issue(issue_key)
            self.request_count += 1

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

    def get_users(self) -> List[Dict[str, Any]]:
        """Get all users from Jira."""
        self._rate_limit()
        try:
            # Ensure we're connected before trying to access users
            if not self.connected or not self.jira:
                if not self.connect():
                    logger.error("Failed to connect to Jira")
                    return []

            users = self.jira.search_users(
                user=".", includeInactive=True, startAt=0, maxResults=1000
            )

            self.request_count += 1
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
        self._rate_limit()
        try:
            # Use JQL to count issues in the project
            jql = f"project={project_key}"

            # Start with a minimal query - we only need the count
            issues = self.jira.search_issues(jql, maxResults=1, fields="key")
            self.request_count += 1

            # Get total from the response
            return issues.total
        except Exception as e:
            logger.error(f"Failed to get issue count for project {project_key}: {str(e)}")
            return 0

    def get_field_metadata(self, field_id: str) -> Dict[str, Any]:
        """
        Get metadata for a specific custom field, including allowed values.

        Args:
            field_id: The ID of the custom field to retrieve metadata for

        Returns:
            Dictionary containing field metadata
        """
        self._rate_limit()
        try:
            # Extract field ID number if it contains 'customfield_' prefix
            if field_id.startswith('customfield_'):
                field_id_num = field_id[12:]  # Remove 'customfield_' prefix
            else:
                field_id_num = field_id

            # Use REST API to get field metadata
            endpoint = f"/rest/api/2/field/{field_id}"

            # Direct REST API call to get field metadata
            url = f"{self.base_url}{endpoint}"

            # Set up authentication header
            auth_header = None
            if self.jira_api_token:
                if self.jira_username:
                    # Basic auth with username/token
                    auth_string = base64.b64encode(f"{self.jira_username}:{self.jira_api_token}".encode()).decode()
                    auth_header = {"Authorization": f"Basic {auth_string}"}
                else:
                    # Bearer token
                    auth_header = {"Authorization": f"Bearer {self.jira_api_token}"}

            # Make the request
            response = self.session.get(url, headers=auth_header)
            self.request_count += 1

            if response.status_code != 200:
                logger.warning(f"Failed to get metadata for field {field_id}: HTTP {response.status_code}")

                # Try alternative approach - get context metadata which may include allowed values
                # Only works for some Jira installations
                try:
                    # Alternative approach to get allowed values - create a temporary issue metadata query
                    # This gives us the field configuration including allowed values
                    meta_endpoint = f"/rest/api/2/issue/createmeta?expand=projects.issuetypes.fields"
                    meta_url = f"{self.base_url}{meta_endpoint}"

                    meta_response = self.session.get(meta_url, headers=auth_header)
                    self.request_count += 1

                    if meta_response.status_code == 200:
                        meta_data = meta_response.json()

                        # Look for our field in any project/issuetype combination
                        for project in meta_data.get('projects', []):
                            for issuetype in project.get('issuetypes', []):
                                fields = issuetype.get('fields', {})
                                if field_id in fields:
                                    logger.info(f"Found field {field_id} metadata in createmeta")
                                    return fields[field_id]

                    logger.warning(f"Could not find field {field_id} in createmeta")
                except Exception as e:
                    logger.warning(f"Failed to get field {field_id} from createmeta: {str(e)}")

                # Return empty dict if we couldn't get the data
                return {}

            # Process and return the result
            field_data = response.json()

            # Check if we need to get allowed values for select lists
            if field_data.get('schema', {}).get('type') in ['option', 'array'] or 'select' in field_data.get('schema', {}).get('custom', '').lower():
                # Try to get allowed values from context options
                try:
                    context_endpoint = f"/rest/api/2/field/{field_id}/context"
                    context_url = f"{self.base_url}{context_endpoint}"

                    context_response = self.session.get(context_url, headers=auth_header)
                    self.request_count += 1

                    if context_response.status_code == 200:
                        contexts = context_response.json().get('values', [])

                        if contexts:
                            # Get the first context ID to retrieve options
                            context_id = contexts[0].get('id')

                            options_endpoint = f"/rest/api/2/field/{field_id}/context/{context_id}/option"
                            options_url = f"{self.base_url}{options_endpoint}"

                            options_response = self.session.get(options_url, headers=auth_header)
                            self.request_count += 1

                            if options_response.status_code == 200:
                                field_data['allowedValues'] = options_response.json().get('values', [])
                                logger.info(f"Retrieved {len(field_data['allowedValues'])} allowed values for field {field_id}")
                except Exception as e:
                    logger.warning(f"Failed to get context options for field {field_id}: {str(e)}")

            return field_data

        except Exception as e:
            logger.error(f"Failed to get field metadata for {field_id}: {str(e)}")
            return {}
