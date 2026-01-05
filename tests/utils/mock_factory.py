"""Factory functions for creating consistent mock objects for testing."""

from typing import Any
from unittest.mock import MagicMock

try:
    from src.clients.docker_client import DockerClient
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient
    from src.clients.rails_console_client import RailsConsoleClient
    from src.clients.ssh_client import SSHClient
except ModuleNotFoundError:
    # Provide lightweight stubs so unit tests can run without heavy optional deps
    class DockerClient:  # type: ignore[no-redef]
        pass

    class JiraClient:  # type: ignore[no-redef]
        pass

    class OpenProjectClient:  # type: ignore[no-redef]
        pass

    class RailsConsoleClient:  # type: ignore[no-redef]
        pass

    class SSHClient:  # type: ignore[no-redef]
        pass


def create_mock_jira_client() -> JiraClient:
    """Create a mock JiraClient with common behavior.

    Returns:
        JiraClient: A configured mock JiraClient

    """
    mock_client = MagicMock(spec=JiraClient)

    # Configure common mock behaviors
    mock_client.get_projects.return_value = []
    mock_client.get_custom_fields.return_value = []
    mock_client.get_all_issues_for_project.return_value = []

    # Provide a generic .get method used by URL-encoding and refresh tests
    mock_client.get = MagicMock()

    # Provide jira attribute for timezone detection
    mock_client.jira = MagicMock()
    mock_client.jira.server_info.return_value = {"serverTitle": "Test Jira"}

    return mock_client


def create_mock_openproject_client() -> OpenProjectClient:
    """Create a mock OpenProjectClient with common behavior.

    Returns:
        OpenProjectClient: A configured mock OpenProjectClient

    """
    mock_client = MagicMock()

    # Configure common mock behaviors
    mock_client.get_projects.return_value = []
    mock_client.create_user.return_value = {"id": 1, "login": "test.user"}

    # Add rails_client attribute
    mock_client.rails_client = create_mock_rails_client()

    return mock_client


def create_mock_docker_client() -> DockerClient:
    """Create a mock DockerClient with common behavior.

    Returns:
        DockerClient: A configured mock DockerClient

    """
    mock_client = MagicMock()

    # Configure common mock behaviors
    mock_client.is_connected.return_value = True
    mock_client.list_containers.return_value = []

    return mock_client


def create_mock_ssh_client() -> SSHClient:
    """Create a mock SSHClient with common behavior.

    Returns:
        SSHClient: A configured mock SSHClient

    """
    mock_client = MagicMock()

    # Configure common mock behaviors
    mock_client.is_connected.return_value = True
    mock_client.execute_command.return_value = (0, "", "")

    return mock_client


def create_mock_rails_client() -> RailsConsoleClient:
    """Create a mock RailsConsoleClient with common behavior.

    Returns:
        RailsConsoleClient: A configured mock RailsConsoleClient

    """
    mock_client = MagicMock()

    # Configure common mock behaviors
    mock_client.is_connected.return_value = True
    mock_client.execute.return_value = ""

    return mock_client


def create_mock_jira_project(
    key: str = "PROJ",
    name: str = "Test Project",
    lead: str = "admin",
    description: str = "Test project description",
    project_id: int = 10001,
) -> dict[str, Any]:
    """Create a mock Jira project dictionary.

    Args:
        key: Project key
        name: Project name
        lead: Project lead username
        description: Project description
        project_id: Project ID

    Returns:
        Dict[str, Any]: A dictionary representing a Jira project

    """
    return {
        "id": str(project_id),
        "key": key,
        "name": name,
        "projectTypeKey": "software",
        "description": description,
        "lead": {"name": lead},
        "url": f"https://jira.example.com/projects/{key}",
        "projectCategory": {"name": "Test Category"},
        "simplified": False,
        "style": "classic",
        "isPrivate": False,
    }


def create_mock_jira_issue(
    key: str = "PROJ-1",
    summary: str = "Test Issue",
    description: str = "Test issue description",
    issue_type: str = "Task",
    status: str = "Open",
    assignee: str | None = "john.doe",
    reporter: str = "jane.doe",
    priority: str = "Medium",
    project_key: str = "PROJ",
    project_id: int = 10001,
    issue_id: int = 10001,
) -> dict[str, Any]:
    """Create a mock Jira issue dictionary.

    Args:
        key: Issue key
        summary: Issue summary
        description: Issue description
        issue_type: Issue type name
        status: Status name
        assignee: Assignee username
        reporter: Reporter username
        priority: Priority name
        project_key: Project key
        project_id: Project ID
        issue_id: Issue ID

    Returns:
        Dict[str, Any]: A dictionary representing a Jira issue

    """
    assignee_obj = None if assignee is None else {"name": assignee}

    return {
        "id": str(issue_id),
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "issuetype": {"name": issue_type},
            "status": {"name": status},
            "assignee": assignee_obj,
            "reporter": {"name": reporter},
            "priority": {"name": priority},
            "project": {
                "id": str(project_id),
                "key": project_key,
            },
            "created": "2023-01-01T00:00:00.000+0000",
            "updated": "2023-01-02T00:00:00.000+0000",
            "resolutiondate": None,
        },
    }


def create_mock_openproject_project(
    project_id: int = 1,
    name: str = "Test Project",
    identifier: str = "test-project",
    description: str = "Test project description",
) -> dict[str, Any]:
    """Create a mock OpenProject project dictionary.

    Args:
        id: Project ID
        name: Project name
        identifier: Project identifier
        description: Project description

    Returns:
        Dict[str, Any]: A dictionary representing an OpenProject project

    """
    return {
        "id": project_id,
        "name": name,
        "identifier": identifier,
        "description": {"raw": description},
        "createdAt": "2023-01-01T00:00:00Z",
        "updatedAt": "2023-01-01T00:00:00Z",
        "_links": {
            "self": {
                "href": f"/api/v3/projects/{project_id}",
                "title": name,
            },
        },
    }


def create_mock_openproject_work_package(
    wp_id: int = 1,
    subject: str = "Test Work Package",
    description: str = "Test work package description",
    type_name: str = "Task",
    status: str = "New",
    project_id: int = 1,
) -> dict[str, Any]:
    """Create a mock OpenProject work package dictionary.

    Args:
        id: Work package ID
        subject: Work package subject
        description: Work package description
        type_name: Type name
        status: Status name
        project_id: Project ID

    Returns:
        Dict[str, Any]: A dictionary representing an OpenProject work package

    """
    return {
        "id": wp_id,
        "subject": subject,
        "description": {"raw": description},
        "_embedded": {
            "type": {
                "name": type_name,
            },
            "status": {
                "name": status,
            },
            "project": {
                "id": project_id,
            },
        },
        "createdAt": "2023-01-01T00:00:00Z",
        "updatedAt": "2023-01-01T00:00:00Z",
        "_links": {
            "self": {
                "href": f"/api/v3/work_packages/{wp_id}",
                "title": subject,
            },
        },
    }
