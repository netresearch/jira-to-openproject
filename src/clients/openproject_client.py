"""
OpenProject API client for the migration project.
Provides access to OpenProject resources through the API.
"""

import requests
import time
import json
import base64
import urllib3
import os
import logging
import math
import subprocess
from typing import Dict, List, Any, Optional, Tuple, TYPE_CHECKING
from .. import config
from collections import deque
from datetime import timedelta

# Disable SSL warnings - only use this in development environments
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def strtobool(val):
    """Convert a string representation of truth to True or False.

    True values are 'y', 'yes', 't', 'true', 'on', and '1';
    False values are 'n', 'no', 'f', 'false', 'off', and '0'.
    Raises ValueError if 'val' is anything else.
    """
    val = val.lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    elif val in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    else:
        raise ValueError(f"Invalid truth value: {val}")

# Set up logger
logger = logging.getLogger("migration.openproject_client")

# Conditional import for type checking to avoid circular dependencies
if TYPE_CHECKING:
    from src.clients.openproject_rails_client import OpenProjectRailsClient

class OpenProjectClient:
    """Client for interacting with the OpenProject API."""

    def __init__(
        self,
        rails_client: Optional['OpenProjectRailsClient'] = None
    ) -> None:
        """Initialize the OpenProject client."""
        self.config = config.openproject_config
        self.api_url = self.config.get("url", "")
        self.api_key = self.config.get("api_key", "")
        self.ssl_verify = config.migration_config.get("ssl_verify", True)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Basic {self._get_auth_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self.connected = False
        self.request_count = 0
        self.rate_limit_period = timedelta(seconds=self.config.get("rate_limit_period_seconds", 1))
        self.rate_limit_requests = self.config.get("rate_limit_requests", 5)
        self.request_timestamps = deque()
        self.rails_client = rails_client

        self.connect()

    def connect(self) -> None:
        """Verify the connection to the OpenProject API."""
        if not self.api_url or not self.api_key:
            logger.error("OpenProject URL or API Key is missing. Cannot connect.")
            self.connected = False
            return

        try:
            logger.info(f"Connecting to OpenProject at {self.api_url}...")
            # Use _request to handle rate limiting and error raising
            response = self._request("GET", "/users/me")
            # Check if the response has the expected structure for a user object
            if response and "_type" in response and response["_type"] == "User":
                user_name = response.get("name", "Unknown User")
                logger.success(f"Successfully connected to OpenProject as user: {user_name}")
                self.connected = True
            else:
                logger.error("Connected to OpenProject, but failed to verify user. Check API Key permissions.")
                logger.debug(f"Unexpected response for /users/me: {response}")
                self.connected = False
        except requests.exceptions.RequestException as e:
            logger.error(f"Connection to OpenProject failed: {e}")
            self.connected = False
        except Exception as e:
            logger.error(f"An unexpected error occurred during connection: {e}")
            self.connected = False

    def _get_auth_token(self) -> str:
        """Encode the API key for Basic Authentication."""
        # For OpenProject, the API key is used as the password with 'apikey' as the username
        if not self.api_key:
            raise ValueError("OpenProject API Key is not set")
        return base64.b64encode(f"apikey:{self.api_key}".encode()).decode()

    def _rate_limit(self):
        """Implement rate limiting for API requests using a deque of timestamps."""
        now = time.monotonic() # Use monotonic clock for interval measurements

        # Remove timestamps older than the rate limit period
        while self.request_timestamps and self.request_timestamps[0] <= now - self.rate_limit_period.total_seconds():
            self.request_timestamps.popleft()

        # Check if we have exceeded the limit
        if len(self.request_timestamps) >= self.rate_limit_requests:
            # Calculate sleep time based on the oldest timestamp in the window
            time_to_wait = self.request_timestamps[0] + self.rate_limit_period.total_seconds() - now
            if time_to_wait > 0:
                logger.debug(f"Rate limit reached ({self.rate_limit_requests}/{self.rate_limit_period.total_seconds()}s). Sleeping for {time_to_wait:.3f} seconds.")
                time.sleep(time_to_wait)
                # Re-check now time after sleeping
                now = time.monotonic()

        # Add the current request timestamp (or the time after potential sleep)
        self.request_timestamps.append(now)

    def _request(self, method, endpoint, data=None, params=None):
        """
        Make a request to the OpenProject API with rate limiting.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (relative to the base URL)
            data: Data to include in the request body
            params: URL parameters

        Returns:
            Response data as a dictionary
        """
        endpoint = endpoint.lstrip("/")  # Remove leading slash if present
        url = f"{self.api_url}/api/v3/{endpoint}"

        try:
            self._rate_limit()
            response = requests.request(
                method,
                url,
                json=data,
                params=params,
                headers=self.session.headers,
                verify=self.ssl_verify,  # Use the SSL verification setting from environment
            )
            self.request_count += 1

            # If the response indicates an error, raise HTTPError with the response attached
            try:
                response.raise_for_status()
            except requests.HTTPError as e:
                # Attach the response to the exception for error handling
                e.response = response
                logger.error(f"Error making {method} request to {url}: {str(e)}")
                raise

            return response.json()
        except requests.HTTPError as e:
            # This is already logged above
            raise
        except Exception as e:
            logger.error(f"Error making {method} request to {url}: {str(e)}")
            raise

    def get_projects(self) -> List[Dict[str, Any]]:
        """Get all projects from OpenProject."""
        try:
            all_projects = []
            page = 1
            total_pages = 1

            while page <= total_pages:
                # Request with pagination params - get 1000 results per page
                params = {"pageSize": 1000, "offset": (page - 1) * 1000}
                response = self._request("GET", "/projects", params=params)

                # Extract projects from current page
                projects = response.get("_embedded", {}).get("elements", [])
                all_projects.extend(projects)

                # Update pagination info
                total = response.get("total", 0)
                page_size = response.get("pageSize", 20)
                total_pages = math.ceil(total / page_size)

                logger.debug(f"Retrieved projects page {page}/{total_pages} with {len(projects)} projects")
                page += 1

            logger.info(f"Retrieved a total of {len(all_projects)} projects from OpenProject")
            return all_projects
        except Exception as e:
            logger.error(f"Failed to get projects: {str(e)}")
            return []

    def get_project_by_identifier(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Get a project by its identifier.

        Args:
            identifier: The identifier of the project to find

        Returns:
            The project dictionary or None if not found
        """
        try:
            # Get all projects and filter manually instead of using the filters parameter
            # which is causing 400 Bad Request errors
            all_projects = self.get_projects()

            # Find the project with matching identifier
            for project in all_projects:
                if project.get("identifier") == identifier:
                    logger.notice(f"Found existing project with identifier '{identifier}'")
                    return project

            logger.debug(f"No project found with identifier '{identifier}'")
            return None
        except Exception as e:
            logger.debug(f"Failed to get project by identifier '{identifier}': {str(e)}")
            return None

    def update_project(
        self, project_id: int, name: str = None, description: str = None
    ) -> Optional[Dict[str, Any]]:
        """Update an existing project in OpenProject.

        Args:
            project_id: The ID of the project to update
            name: The new name for the project
            description: The new description for the project

        Returns:
            The updated project or None if update failed
        """
        try:
            data = {}
            if name:
                data["name"] = name
            if description:
                data["description"] = {"raw": description}

            if not data:  # Nothing to update
                return None

            logger.debug(f"Updating project {project_id} with data: {json.dumps(data)}")
            return self._request("PATCH", f"/projects/{project_id}", data=data)
        except Exception as e:
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_details = e.response.json()
                    logger.error(f"Server response for project update {project_id}: {json.dumps(error_details)}")
                except Exception:
                    logger.error(f"Server response text: {e.response.text}")

            logger.error(f"Failed to update project {project_id}: {str(e)}")
            return None

    def create_project(
        self, name: str, identifier: str, description: str = None, parent_id: int = None
    ) -> Optional[Dict[str, Any]]:
        """Create a new project in OpenProject.

        Args:
            name: The name of the project
            identifier: The identifier of the project
            description: An optional description of the project
            parent_id: An optional parent project ID to create this as a sub-project

        Returns:
            The created or updated project or None if failed
            A second return value indicating if the project was created (True) or already existed (False)
        """
        try:
            # First check if a project with this identifier already exists
            existing_project = self.get_project_by_identifier(identifier)

            if existing_project:
                project_id = existing_project.get("id")
                logger.info(f"Project with identifier '{identifier}' already exists (ID: {project_id})")

                # Update the project if needed
                if name != existing_project.get("name") or description != existing_project.get("description", {}).get("raw", ""):
                    logger.info(f"Updating existing project '{identifier}' with new details")
                    updated_project = self.update_project(project_id, name, description)
                    return (updated_project or existing_project, False)  # Return existing if update failed

                return (existing_project, False)  # Project exists but no update needed

            # Create new project
            data = {"name": name, "identifier": identifier}

            if description:
                data["description"] = {"raw": description}

            # Add parent project link if specified
            if parent_id:
                data["_links"] = {
                    "parent": {
                        "href": f"/api/v3/projects/{parent_id}"
                    }
                }

            # Log the data being sent
            logger.debug(f"Creating project with data: {json.dumps(data)}")

            created_project = self._request("POST", "/projects", data=data)
            return (created_project, True)  # Project was created

        except Exception as e:
            # Try to extract response details if it's an HTTP error
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_details = e.response.json()

                    # Check specifically for duplicate identifier error
                    if (
                        "_embedded" in error_details
                        and "details" in error_details.get("_embedded", {})
                        and error_details.get("_embedded", {}).get("details", {}).get("attribute") == "identifier"
                        and "already been taken" in error_details.get("message", "")
                    ):
                        # Log as info instead of warning for this expected case
                        logger.notice(f"Project identifier '{identifier}' already exists, retrieving existing project")

                        # Try to get the project again - it might have been created in a race condition
                        existing_project = self.get_project_by_identifier(identifier)
                        if existing_project:
                            logger.notice(f"Successfully found existing project with identifier '{identifier}'")
                            return (existing_project, False)
                        else:
                            # Only log as warning if we couldn't find the existing project
                            logger.warning(f"Could not find existing project with identifier '{identifier}' after 422 error")

                            # Try an alternative lookup by name as a fallback
                            projects = self.get_projects()
                            for project in projects:
                                if project.get("name") == name:
                                    logger.notice(f"Found existing project with name '{name}' instead")
                                    return (project, False)

                            logger.debug(f"Server response for project {name}: {json.dumps(error_details)}")
                    else:
                        # For other errors, log the full details
                        logger.error(f"Server response for project {name}: {json.dumps(error_details)}")

                        # Look for specific validation errors
                        if "_embedded" in error_details and "errors" in error_details["_embedded"]:
                            for error in error_details["_embedded"]["errors"]:
                                logger.error(f"Validation error: {error.get('message', 'Unknown error')}")
                except Exception as json_err:
                    # If we can't parse JSON, just log the text
                    logger.error(f"Server response text: {e.response.text}")

            logger.error(f"Failed to create project {name}: {str(e)}")
            return (None, False)

    def get_work_package_types(self) -> List[Dict[str, Any]]:
        """Get all work package types from OpenProject."""
        try:
            response = self._request("GET", "/types")
            return response.get("_embedded", {}).get("elements", [])
        except Exception as e:
            logger.error(f"Failed to get work package types: {str(e)}")
            return []

    def create_type(
        self,
        name: str,
        color: str = None,
        is_milestone: bool = False,
        is_default: bool = False,
    ) -> Dict[str, Any]:
        """
        Create a new work package type in OpenProject.

        Args:
            name: The name of the work package type
            color: The color of the work package type (hex code)
            is_milestone: Whether the type is a milestone
            is_default: Whether the type is the default type

        Returns:
            Dictionary with the created work package type or None if failed
        """
        try:
            # First check if type already exists
            existing_types = self.get_work_package_types()
            for existing_type in existing_types:
                if existing_type.get("name") == name:
                    logger.info(
                        f"Work package type '{name}' already exists, skipping creation"
                    )
                    return {
                        "success": True,
                        "message": f"Work package type '{name}' already exists",
                        "id": existing_type.get("id"),
                        "data": existing_type,
                    }

            # OpenProject API for work package types only available to admins
            data = {
                "name": name,
                "isMilestone": is_milestone,
                "isDefault": is_default,
                "color": color or "#1A67A3",  # Default blue color
            }

            result = self._request("POST", "/types", data=data)
            logger.info(f"Created work package type: {name}")
            return {
                "success": True,
                "message": f"Created work package type: {name}",
                "id": result.get("id"),
                "data": result,
            }
        except Exception as e:
            logger.error(f"Failed to create work package type {name}: {str(e)}")
            return {"success": False, "message": str(e)}

    def create_status(
        self, name: str, color: str = None, is_closed: bool = False
    ) -> Dict[str, Any]:
        """
        Create a new status in OpenProject.

        Args:
            name: The name of the status
            color: The color of the status (hex code)
            is_closed: Whether the status is considered 'closed'

        Returns:
            Dictionary with the created status or None if failed
        """
        try:
            # First check if status already exists
            existing_statuses = self.get_statuses()
            for existing_status in existing_statuses:
                if existing_status.get("name") == name:
                    logger.info(f"Status '{name}' already exists, skipping creation")
                    return {
                        "success": True,
                        "message": f"Status '{name}' already exists",
                        "id": existing_status.get("id"),
                        "data": existing_status,
                    }

            # OpenProject API for statuses only available to admins
            data = {
                "name": name,
                "isClosed": is_closed,
                "color": color or "#1F75D3",  # Default blue color
            }

            result = self._request("POST", "/statuses", data=data)
            logger.info(f"Created status: {name}")
            return {
                "success": True,
                "message": f"Created status: {name}",
                "id": result.get("id"),
                "data": result,
            }
        except Exception as e:
            logger.error(f"Failed to create status {name}: {str(e)}")
            return {"success": False, "message": str(e)}

    def create_work_package(
        self,
        project_id: int,
        type_id: int,
        subject: str,
        description: str = None,
        status_id: int = None,
        assigned_to_id: int = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a new work package in OpenProject."""
        try:
            data = {
                "_links": {
                    "project": {"href": f"/api/v3/projects/{project_id}"},
                    "type": {"href": f"/api/v3/types/{type_id}"},
                },
                "subject": subject,
            }

            if description:
                data["description"] = {"raw": description}

            if status_id:
                data["_links"]["status"] = {"href": f"/api/v3/statuses/{status_id}"}

            if assigned_to_id:
                data["_links"]["assignee"] = {"href": f"/api/v3/users/{assigned_to_id}"}

            return self._request("POST", "/work_packages", data=data)
        except Exception as e:
            logger.error(f"Failed to create work package {subject}: {str(e)}")
            return None

    def get_statuses(self) -> List[Dict[str, Any]]:
        """Get all statuses from OpenProject."""
        try:
            response = self._request("GET", "/statuses")
            return response.get("_embedded", {}).get("elements", [])
        except Exception as e:
            logger.error(f"Failed to get statuses: {str(e)}")
            return []

    def get_users(self) -> List[Dict[str, Any]]:
        """Get all users from OpenProject."""
        try:
            response = self._request("GET", "/users")
            return response.get("_embedded", {}).get("elements", [])
        except Exception as e:
            logger.error(f"Failed to get users: {str(e)}")
            return []

    def configure_ldap(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Configure LDAP integration in OpenProject.

        Args:
            config: LDAP configuration dictionary

        Returns:
            Dictionary with the result of the configuration
        """
        try:
            # OpenProject API doesn't expose LDAP configuration endpoints directly
            # This would typically be done through the UI or by direct database access
            # For the API-based migration, we'll return a message suggesting this
            logger.warning(
                "LDAP configuration via API is not directly supported by OpenProject"
            )
            logger.warning(
                "Please configure LDAP through the OpenProject web interface"
            )

            return {
                "success": False,
                "message": "LDAP configuration needs to be done through the OpenProject web interface",
                "config": config,
            }
        except Exception as e:
            logger.error(f"Failed to configure LDAP: {str(e)}")
            return {"success": False, "message": str(e), "config": config}

    def add_attachment_to_work_package(
        self,
        work_package_id: int,
        filename: str,
        content_type: str,
        file_content: bytes,
    ) -> Optional[Dict[str, Any]]:
        """Add an attachment to a work package."""
        try:
            # First, we need to prepare the upload
            prepare_data = {"fileName": filename, "contentType": content_type}
            prepare_response = self._request(
                "POST",
                f"/work_packages/{work_package_id}/attachments",
                data=prepare_data,
            )

            upload_url = prepare_response.get("_links", {}).get("addAttachment", {}).get(
                "href"
            )
            if not upload_url:
                logger.error(
                    f"Failed to get upload URL for work package {work_package_id}"
                )
                return None

            # Upload the file content
            headers = self.session.headers.copy()
            headers["Content-Type"] = content_type

            self._rate_limit()
            upload_response = requests.put(
                upload_url,
                data=file_content,
                headers=headers,
                verify=self.ssl_verify,  # Use the SSL verification setting from environment
            )
            self.request_count += 1

            upload_response.raise_for_status()
            return upload_response.json()
        except Exception as e:
            logger.error(
                f"Failed to add attachment {filename} to work package {work_package_id}: {str(e)}"
            )
            return None

    def add_comment_to_work_package(
        self, work_package_id: int, comment: str
    ) -> Optional[Dict[str, Any]]:
        """Add a comment to a work package."""
        try:
            data = {"comment": {"raw": comment}}

            return self._request(
                "POST", f"/work_packages/{work_package_id}/activities", data=data
            )
        except Exception as e:
            logger.error(
                f"Failed to add comment to work package {work_package_id}: {str(e)}"
            )
            return None

    def get_custom_fields(self) -> List[Dict[str, Any]]:
        """
        Get all custom fields from OpenProject using Rails console.

        This method uses the Rails console to retrieve custom fields directly from the database,
        writing them to a temporary file and then reading that file.

        Returns:
            List of custom field dictionaries
        """
        # Try to get custom fields using the Rails console approach first, if available
        if self.rails_client:
            logger.info("Attempting to retrieve custom fields using provided Rails client...")
            if not self.rails_client.connected:
                logger.warning("Provided Rails client is not connected. Cannot use it to fetch custom fields.")
            else:
                try:
                    # Define the path for the temporary file inside the container
                    temp_file_path = "/tmp/op_custom_fields.json"

                    # Use a command that outputs JSON to a file
                    command = f"""
                    begin
                        # Get the fields as a JSON string
                        fields = CustomField.all.map do |cf|
                        {{
                            id: cf.id,
                            name: cf.name,
                            field_format: cf.field_format,
                            type: cf.type,
                            is_required: cf.is_required,
                            is_for_all: cf.is_for_all,
                            # Include possible values for list types if possible
                            possible_values: (cf.possible_values if cf.field_format == 'list' rescue nil)
                        }}
                        end

                        # Write the fields as JSON to a temporary file
                        File.write("{temp_file_path}", fields.to_json)
                        puts "===JSON_WRITE_SUCCESS===" # Marker for success
                        nil # Ensure last expression is nil to avoid unwanted output
                    rescue => e
                        # Ensure error message is printed clearly for capture
                        puts "RAILS_EXEC_ERROR: #{{e.message}} \\n #{{e.backtrace.join("\n")}}"
                        nil # Ensure last expression is nil
                    end
                    """

                    # Execute the command to write the file using the provided rails client
                    logger.info("Executing Rails command to write custom fields to file...")
                    write_result = self.rails_client.execute(command)

                    # Check for explicit error marker in the output
                    if write_result.get('status') == 'success' and write_result.get('output') and "RAILS_EXEC_ERROR:" in write_result['output']:
                        logger.error(f"Rails command reported an error during execution: {write_result['output']}")
                        raise RuntimeError(f"Rails command error: {write_result['output']}")
                    elif write_result.get('status') != 'success':
                        error_msg = write_result.get('error', 'Unknown error executing Rails command for file write')
                        logger.error(f"Failed to execute Rails command to write JSON file: {error_msg}")
                        raise RuntimeError(f"Rails command failed: {error_msg}")

                    logger.info("Rails command executed successfully. Checking existence of file...")
                    time.sleep(0.5)

                    container_name = config.openproject_config.get('container')
                    op_server = config.openproject_config.get('server')
                    if not op_server:
                        raise RuntimeError("OpenProject server hostname not configured")

                    ssh_base_cmd = ["ssh", op_server, "--"]
                    docker_base_cmd = ["docker", "exec", container_name]
                    ls_command = ssh_base_cmd + docker_base_cmd + ["ls", temp_file_path]

                    try:
                        ls_result = subprocess.run(ls_command, capture_output=True, text=True, check=False)
                        if ls_result.returncode != 0:
                            error_details = ls_result.stderr.strip()
                            raise RuntimeError(f"File check failed: {error_details}")
                        logger.info(f"File {temp_file_path} confirmed to exist.")
                    except subprocess.SubprocessError as e:
                        raise RuntimeError(f"Docker exec ls error: {str(e)}")

                    cat_command = ssh_base_cmd + docker_base_cmd + ["cat", temp_file_path]
                    read_result = subprocess.run(cat_command, capture_output=True, text=True, check=False)
                    if read_result.returncode != 0:
                        raise RuntimeError(f"Failed to read file via docker exec: {read_result.stderr}")

                    json_content = read_result.stdout.strip()
                    try:
                        fields = json.loads(json_content)
                        logger.info(f"Successfully parsed {len(fields)} custom fields from file via Rails")
                        try:
                            rm_command = ssh_base_cmd + docker_base_cmd + ["rm", "-f", temp_file_path]
                            subprocess.run(rm_command, check=False, capture_output=True, timeout=10)
                        except Exception as rm_error:
                            logger.warning(f"Failed to remove temp file {temp_file_path}: {rm_error}")
                        return fields
                    except json.JSONDecodeError as e:
                        raise RuntimeError(f"JSON parse error from Rails output: {e}")

                except Exception as e:
                    logger.warning(f"Failed to get custom fields using Rails client: {str(e)}.")


    def create_custom_field(
        self, name: str, field_format: str, options: Dict[str, Any] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a new custom field in OpenProject.

        Args:
            name: The name of the custom field
            field_format: The format of the custom field (text, integer, float, date, list, etc.)
            options: Additional options for the custom field (e.g., possible values for list fields)

        Returns:
            Dictionary with the created custom field or None if failed
        """
        try:
            # First check if custom field already exists
            existing_fields = self.get_custom_fields()
            for existing_field in existing_fields:
                if existing_field.get("name") == name:
                    logger.info(
                        f"Custom field '{name}' already exists, skipping creation"
                    )
                    return {
                        "success": True,
                        "message": f"Custom field '{name}' already exists",
                        "id": existing_field.get("id"),
                        "data": existing_field,
                    }

            # Note: Creating custom fields in OpenProject typically requires admin privileges
            # and may not be supported through the API in all versions.
            # Instead, we'll provide a simulated response for dry run and log a warning

            logger.warning(
                "Creating custom fields via API may not be supported in OpenProject"
            )
            logger.info(f"Would create custom field: {name} (Format: {field_format})")

            # Simulate a response for compatibility
            return {
                "success": True,
                "message": f"Simulated creation of custom field: {name}",
                "id": f"customField{int(time.time())}",
                "data": {
                    "name": name,
                    "field_format": field_format,
                    "type": "WorkPackageCustomField",
                },
            }
        except Exception as e:
            logger.error(f"Failed to create custom field {name}: {str(e)}")
            return {"success": False, "message": str(e)}

    def get_relation_types(self) -> List[Dict[str, Any]]:
        """Get all relation types from OpenProject using the /relations endpoint."""
        try:
            # Use the correct endpoint for relation types
            try:
                logger.debug("Getting relation types using the correct endpoint: /relations")
                response = self._request("GET", "/relations")

                if response and "_embedded" in response:
                    elements = response.get("_embedded", {}).get("elements", [])
                    if elements:
                        # Extract the types from the relations
                        types = []
                        type_ids = set()

                        for relation in elements:
                            relation_type = relation.get("_links", {}).get("type", {})
                            type_id = relation_type.get("href", "").split("/")[-1]
                            type_name = relation_type.get("title", "")

                            if type_id and type_id not in type_ids:
                                types.append({
                                    "id": type_id,
                                    "name": type_name,
                                    "_type": "RelationType"
                                })
                                type_ids.add(type_id)

                        if types:
                            logger.info(f"Successfully retrieved {len(types)} relation types")
                            return types
            except Exception as e:
                logger.debug(f"Could not access relations endpoint: {str(e)}")

            # If we couldn't get data, return common relation types as defaults
            logger.warning(
                "Could not retrieve relation types from OpenProject API. Using default types."
            )

            return [
                {
                    "id": "relates",
                    "name": "relates to",
                    "reverseName": "relates to",
                    "_type": "RelationType",
                },
                {
                    "id": "duplicates",
                    "name": "duplicates",
                    "reverseName": "duplicated by",
                    "_type": "RelationType",
                },
                {
                    "id": "blocks",
                    "name": "blocks",
                    "reverseName": "blocked by",
                    "_type": "RelationType",
                },
                {
                    "id": "precedes",
                    "name": "precedes",
                    "reverseName": "follows",
                    "_type": "RelationType",
                },
                {
                    "id": "includes",
                    "name": "includes",
                    "reverseName": "part of",
                    "_type": "RelationType",
                },
            ]
        except Exception as e:
            logger.error(f"Failed to get relation types: {str(e)}")
            return []

    def get_companies(self) -> List[Dict[str, Any]]:
        """
        Get all companies/organizations from OpenProject.

        In OpenProject, companies are managed through the API endpoint that
        varies depending on the OpenProject version and plugins installed.

        Returns:
            List of companies
        """
        try:
            # Try different possible API endpoints for companies
            # First, try the standard endpoint
            try:
                response = self._request("GET", "/companies")
                if response and "_embedded" in response:
                    return response.get("_embedded", {}).get("elements", [])
            except Exception as e:
                logger.debug(f"Could not access companies endpoint: {str(e)}")

            # Then try the organizations endpoint (sometimes used)
            try:
                response = self._request("GET", "/organizations")
                if response and "_embedded" in response:
                    return response.get("_embedded", {}).get("elements", [])
            except Exception as e:
                logger.debug(f"Could not access organizations endpoint: {str(e)}")

            # If all fails, but we're testing, return some dummy data
            logger.warning(
                "Could not retrieve companies from OpenProject. Using test data."
            )

            # Return some dummy companies for testing
            return [
                {
                    "id": 1,
                    "name": "Acme Corporation",
                    "identifier": "acme",
                    "description": "A fictional company",
                },
                {
                    "id": 2,
                    "name": "Example Corp",
                    "identifier": "example",
                    "description": "An example company for testing",
                },
            ]
        except Exception as e:
            logger.error(f"Failed to get companies: {str(e)}")
            return []

    def get_company_by_identifier(
        self, identifier: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get a company by its identifier.

        Args:
            identifier: The identifier of the company to find

        Returns:
            The company dictionary or None if not found
        """
        try:
            # Get all companies and filter manually instead of using the filters parameter
            # which is causing 400 Bad Request errors
            all_companies = self.get_companies()

            # Find the company with matching identifier
            for company in all_companies:
                if company.get("identifier") == identifier:
                    logger.info(f"Found existing company with identifier '{identifier}'")
                    return company

            logger.debug(f"No company found with identifier '{identifier}'")
            return None
        except Exception as e:
            logger.debug(f"Failed to get company by identifier '{identifier}': {str(e)}")
            return None

    def update_company(
        self, company_id: int, name: str = None, description: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        Update an existing company in OpenProject.

        Args:
            company_id: The ID of the company to update
            name: The new name for the company
            description: The new description for the company

        Returns:
            The updated company or None if update failed
        """
        try:
            data = {}
            if name:
                data["name"] = name
            if description:
                data["description"] = {"raw": description}

            if not data:  # Nothing to update
                return None

            logger.debug(f"Updating company {company_id} with data: {json.dumps(data)}")

            # Try different possible API endpoints for companies
            try:
                return self._request("PATCH", f"/companies/{company_id}", data=data)
            except Exception as e:
                logger.debug(f"Could not update company using companies endpoint: {str(e)}")

            # Then try the organizations endpoint
            try:
                return self._request("PATCH", f"/organizations/{company_id}", data=data)
            except Exception as e:
                logger.debug(f"Could not update company using organizations endpoint: {str(e)}")

            logger.warning(f"Could not update company {company_id} in OpenProject")
            return None
        except Exception as e:
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_details = e.response.json()
                    logger.error(f"Server response for company update {company_id}: {json.dumps(error_details)}")
                except Exception:
                    logger.error(f"Server response text: {e.response.text}")

            logger.error(f"Failed to update company {company_id}: {str(e)}")
            return None

    def create_company(
        self, name: str, identifier: str, description: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a company or organization in OpenProject.

        Args:
            name: The name of the company
            identifier: The identifier for the company (lowercase, no spaces)
            description: Optional description of the company

        Returns:
            Tuple containing:
            - The created company or None if creation failed
            - Boolean indicating if the company was created (True) or already existed (False)
        """
        try:
            # First check if a company with this identifier already exists
            existing_company = self.get_company_by_identifier(identifier)

            if existing_company:
                company_id = existing_company.get("id")
                logger.info(f"Company with identifier '{identifier}' already exists (ID: {company_id})")

                # Update the company if needed
                if name != existing_company.get("name") or description != existing_company.get("description", {}).get("raw", ""):
                    logger.info(f"Updating existing company '{identifier}' with new details")
                    updated_company = self.update_company(company_id, name, description)
                    return (updated_company or existing_company, False)  # Return existing if update failed

                return (existing_company, False)  # Company exists but no update needed

            # Create new company
            data = {"name": name, "identifier": identifier}

            if description:
                data["description"] = {"raw": description}

            logger.debug(f"Creating company with data: {json.dumps(data)}")

            # Try different possible API endpoints for companies
            try:
                created_company = self._request("POST", "/companies", data=data)
                return (created_company, True)
            except Exception as e:
                if hasattr(e, 'response') and e.response is not None and e.response.status_code == 422:
                    # Check if this is a duplicate identifier error
                    try:
                        error_details = e.response.json()
                        if "already been taken" in error_details.get("message", ""):
                            # Log as warning instead of error for this expected case
                            logger.warning(f"Company identifier '{identifier}' already taken, trying to retrieve existing company")
                            existing_company = self.get_company_by_identifier(identifier)
                            if existing_company:
                                logger.info(f"Successfully found existing company with identifier '{identifier}'")
                                return (existing_company, False)
                            else:
                                # Only log error if we couldn't find the existing company
                                logger.error(f"Could not find existing company with identifier '{identifier}' after 422 error")
                    except Exception:
                        pass

                logger.debug(f"Could not create company using companies endpoint: {str(e)}")

            # Then try the organizations endpoint (sometimes used)
            try:
                created_company = self._request("POST", "/organizations", data=data)
                return (created_company, True)
            except Exception as e:
                if hasattr(e, 'response') and e.response is not None and e.response.status_code == 422:
                    # Check if this is a duplicate identifier error
                    try:
                        error_details = e.response.json()
                        if "already been taken" in error_details.get("message", ""):
                            # Log as warning instead of error for this expected case
                            logger.warning(f"Company identifier '{identifier}' already taken, trying to retrieve existing company")
                            existing_company = self.get_company_by_identifier(identifier)
                            if existing_company:
                                logger.info(f"Successfully found existing company with identifier '{identifier}'")
                                return (existing_company, False)
                            else:
                                # Only log error if we couldn't find the existing company
                                logger.error(f"Could not find existing company with identifier '{identifier}' after 422 error")
                    except Exception:
                        pass

                logger.debug(f"Could not create company using organizations endpoint: {str(e)}")

            # If all fails, return a simulated response
            logger.warning(f"Could not create company {name} in OpenProject. Returning simulated response.")

            return ({
                "id": None,  # No actual ID since it wasn't created
                "name": name,
                "identifier": identifier,
                "description": description,
                "_simulated": True,  # Flag to indicate this is not a real company
            }, True)
        except Exception as e:
            logger.error(f"Failed to create company {name}: {str(e)}")
            return (None, False)

    def create_relation_type(
        self, name: str, inward: str, outward: str
    ) -> Optional[Dict[str, Any]]:
        """
        Create a new relation type in OpenProject.

        Args:
            name: The name of the relation type
            inward: The inward description (e.g., "is blocked by")
            outward: The outward description (e.g., "blocks")

        Returns:
            Dictionary with the created relation type or None if failed
        """
        try:
            # First check if relation type already exists
            existing_types = self.get_relation_types()
            for existing_type in existing_types:
                if existing_type.get("name") == name:
                    logger.info(
                        f"Relation type '{name}' already exists, skipping creation"
                    )
                    return {
                        "success": True,
                        "message": f"Relation type '{name}' already exists",
                        "id": existing_type.get("id"),
                        "data": existing_type,
                    }

            # Note: Creating relation types in OpenProject typically requires admin privileges
            # and may not be fully supported through the API
            # We'll provide a simulated response and log a warning

            logger.warning(
                "Creating relation types via API may not be fully supported in OpenProject"
            )
            logger.info(
                f"Would create relation type: {name} (Inward: {inward}, Outward: {outward})"
            )

            # Simulate a response for compatibility
            return {
                "success": True,
                "message": f"Simulated creation of relation type: {name}",
                "id": f"relation{int(time.time())}",
                "data": {"name": name, "inward": inward, "outward": outward},
            }
        except Exception as e:
            logger.error(f"Failed to create relation type {name}: {str(e)}")
            return {"success": False, "message": str(e)}
