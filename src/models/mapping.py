"""Data mapping module for Jira to OpenProject migration.

Defines the mapping strategies between Jira and OpenProject data models.
"""

# Add the logger import
from src.display import configure_logging
from src.type_definitions import JiraData, OpenProjectData, StatusMapping, TypeMapping

# Get logger from config
logger = configure_logging("INFO", None)


class JiraToOPMapping:
    """Mapping between Jira and OpenProject data models."""

    @staticmethod
    def map_issue_type(jira_issue_type: JiraData) -> OpenProjectData:
        """Map a Jira issue type to an OpenProject work package type.

        Args:
            jira_issue_type: A dictionary containing Jira issue type data

        Returns:
            Dictionary with mapped OpenProject work package type data

        """
        # Use pattern matching instead of dictionary mapping
        match jira_issue_type["name"].lower():
            case "epic":
                op_name = "Epic"
            case "story":
                op_name = "User Story"
            case "bug":
                op_name = "Bug"
            case "task":
                op_name = "Task"
            case "sub-task":
                op_name = "Task"
            case _:
                op_name = jira_issue_type["name"]

        return {
            "name": op_name,
            "description": jira_issue_type.get("description", ""),
            "color": "#0000FF",  # Default color
            "is_milestone": False,
            "position": 1,  # Default position
            "is_default": False,
            "is_in_roadmap": True,
            "jira_id": jira_issue_type[
                "id"
            ],  # Store the original Jira ID for reference
        }

    @staticmethod
    def map_workflow_status(jira_status: JiraData) -> OpenProjectData:
        """Map a Jira workflow status to an OpenProject status.

        Args:
            jira_status: A dictionary containing Jira status data

        Returns:
            Dictionary with mapped OpenProject status data

        """
        # Use pattern matching instead of dictionary mapping
        match jira_status["name"].lower():
            case "to do" | "backlog":
                op_name = "New"
            case "in progress":
                op_name = "In progress"
            case "done" | "resolved":
                op_name = "Closed"
            case "selected for development":
                op_name = "Ready for development"
            case "code review":
                op_name = "In development"
            case "testing":
                op_name = "In testing"
            case _:
                op_name = jira_status["name"]

        is_closed = jira_status["name"].lower() in ["done", "resolved", "closed"]

        return {
            "name": op_name,
            "description": f"Mapped from Jira status: {jira_status['name']}",
            "is_closed": is_closed,
            "color": "#0000FF",  # Default color
            "jira_id": jira_status["id"],  # Store the original Jira ID for reference
        }

    @staticmethod
    def map_project(jira_project: JiraData) -> OpenProjectData:
        """Map a Jira project to an OpenProject project.

        Args:
            jira_project: A dictionary containing Jira project data

        Returns:
            Dictionary with mapped OpenProject project data

        """
        # Create a lowercase, URL-friendly identifier
        identifier = jira_project["key"].lower().replace(" ", "-")

        return {
            "name": jira_project["name"],
            "identifier": identifier,
            "description": jira_project.get("description", ""),
            "is_public": True,
            "status": "active",
            "jira_id": jira_project["id"],  # Store the original Jira ID for reference
            "jira_key": jira_project[
                "key"
            ],  # Store the original Jira key for reference
        }

    @staticmethod
    def map_issue(
        jira_issue: JiraData,
        type_mapping: TypeMapping,
        status_mapping: StatusMapping,
    ) -> OpenProjectData:
        """Map a Jira issue to an OpenProject work package.

        Args:
            jira_issue: A dictionary containing Jira issue data
            type_mapping: Dictionary mapping Jira issue type IDs to OpenProject type IDs
            status_mapping: Dictionary mapping Jira status IDs to OpenProject status IDs

        Returns:
            Dictionary with mapped OpenProject work package data

        """
        # Get corresponding OpenProject type ID
        jira_type_id = jira_issue["issue_type"]["id"]
        op_type_id = type_mapping.get(jira_type_id)

        # Get corresponding OpenProject status ID
        jira_status_id = jira_issue["status"]["id"]
        op_status_id = status_mapping.get(jira_status_id)

        return {
            "subject": jira_issue["summary"],
            "description": jira_issue.get("description", ""),
            "type_id": op_type_id,
            "status_id": op_status_id,
            "created_at": jira_issue["created"],
            "updated_at": jira_issue["updated"],
            "jira_id": jira_issue["id"],  # Store the original Jira ID for reference
            "jira_key": jira_issue["key"],  # Store the original Jira key for reference
        }

    @staticmethod
    def map_user(jira_user: JiraData) -> OpenProjectData:
        """Map a Jira user to an OpenProject user.

        Args:
            jira_user: A dictionary containing Jira user data

        Returns:
            Dictionary with mapped OpenProject user data

        """
        # Extract the username from email or use the Jira username
        email = jira_user.get("email", "")
        username = email.split("@")[0] if email and "@" in email else jira_user["name"]

        # Use f-string debug format for complex logic
        display_name = jira_user.get("display_name", "")
        has_space = " " in display_name

        # Use pattern matching for name extraction
        match display_name.split(" ") if has_space else [display_name]:
            case [first, *rest] if rest:
                lastname = rest[-1]
                firstname = first
            case [only]:
                firstname = only
                lastname = ""
            case _:
                firstname = ""
                lastname = ""

        # Debug info with f-string = operator
        logger.debug(
            "email=%s, username=%s, firstname=%s, lastname=%s",
            email,
            username,
            firstname,
            lastname,
        )

        return {
            "login": username,
            "firstname": firstname,
            "lastname": lastname,
            "mail": email,
            "status": "active" if jira_user.get("active", True) else "locked",
            "jira_name": jira_user[
                "name"
            ],  # Store the original Jira username for reference
        }

    @staticmethod
    def map_comment(jira_comment: JiraData) -> OpenProjectData:
        """Map a Jira comment to an OpenProject comment.

        Args:
            jira_comment: A dictionary containing Jira comment data

        Returns:
            Dictionary with mapped OpenProject comment data

        """
        return {
            "text": jira_comment["body"],
            "created_at": jira_comment["created"],
            "author": jira_comment["author"],
            "jira_id": jira_comment["id"],  # Store the original Jira ID for reference
        }
