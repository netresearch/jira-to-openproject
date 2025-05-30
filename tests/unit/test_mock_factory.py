"""Tests for the mock_factory utility module."""

from unittest.mock import MagicMock

import pytest

from tests.utils.mock_factory import (
    create_mock_jira_client,
    create_mock_jira_issue,
    create_mock_jira_project,
    create_mock_openproject_client,
    create_mock_openproject_project,
)


@pytest.mark.unit
def test_create_mock_jira_client() -> None:
    """Test creating a mock Jira client."""
    client = create_mock_jira_client()

    # Verify it's a mock
    assert isinstance(client, MagicMock)

    # Verify expected mock behaviors
    assert client.get_projects() == []


@pytest.mark.unit
def test_create_mock_openproject_client() -> None:
    """Test creating a mock OpenProject client."""
    client = create_mock_openproject_client()

    # Verify it's a mock
    assert isinstance(client, MagicMock)

    # Verify expected mock behaviors
    assert client.get_projects() == []


@pytest.mark.unit
def test_create_mock_jira_project_default() -> None:
    """Test creating a mock Jira project with default values."""
    project = create_mock_jira_project()

    # Check default values
    assert project["key"] == "PROJ"
    assert project["name"] == "Test Project"
    assert project["description"] == "Test project description"
    assert project["lead"]["name"] == "admin"
    assert project["id"] == "10001"


@pytest.mark.unit
def test_create_mock_jira_project_custom() -> None:
    """Test creating a mock Jira project with custom values."""
    project = create_mock_jira_project(
        key="CUSTOM",
        name="Custom Project",
        lead="customadmin",
        description="Custom description",
        project_id=20001,
    )

    # Check custom values
    assert project["key"] == "CUSTOM"
    assert project["name"] == "Custom Project"
    assert project["description"] == "Custom description"
    assert project["lead"]["name"] == "customadmin"
    assert project["id"] == "20001"


@pytest.mark.unit
def test_create_mock_jira_issue_default() -> None:
    """Test creating a mock Jira issue with default values."""
    issue = create_mock_jira_issue()

    # Check default values
    assert issue["key"] == "PROJ-1"
    assert issue["fields"]["summary"] == "Test Issue"
    assert issue["fields"]["description"] == "Test issue description"
    assert issue["fields"]["issuetype"]["name"] == "Task"
    assert issue["fields"]["status"]["name"] == "Open"
    assert issue["fields"]["assignee"]["name"] == "john.doe"
    assert issue["fields"]["reporter"]["name"] == "jane.doe"
    assert issue["fields"]["priority"]["name"] == "Medium"
    assert issue["fields"]["project"]["key"] == "PROJ"


@pytest.mark.unit
def test_create_mock_jira_issue_custom() -> None:
    """Test creating a mock Jira issue with custom values."""
    issue = create_mock_jira_issue(
        key="CUSTOM-123",
        summary="Custom Issue",
        description="Custom description",
        issue_type="Bug",
        status="In Progress",
        assignee=None,
        reporter="custom.user",
        priority="High",
        project_key="CUSTOM",
        project_id=20001,
        issue_id=30001,
    )

    # Check custom values
    assert issue["key"] == "CUSTOM-123"
    assert issue["fields"]["summary"] == "Custom Issue"
    assert issue["fields"]["description"] == "Custom description"
    assert issue["fields"]["issuetype"]["name"] == "Bug"
    assert issue["fields"]["status"]["name"] == "In Progress"
    assert issue["fields"]["assignee"] is None
    assert issue["fields"]["reporter"]["name"] == "custom.user"
    assert issue["fields"]["priority"]["name"] == "High"
    assert issue["fields"]["project"]["key"] == "CUSTOM"
    assert issue["fields"]["project"]["id"] == "20001"
    assert issue["id"] == "30001"


@pytest.mark.unit
def test_create_mock_openproject_project_default() -> None:
    """Test creating a mock OpenProject project with default values."""
    project = create_mock_openproject_project()

    # Check default values
    assert project["id"] == 1
    assert project["name"] == "Test Project"
    assert project["identifier"] == "test-project"
    assert project["description"]["raw"] == "Test project description"
    assert "_links" in project
    assert "self" in project["_links"]


@pytest.mark.unit
def test_create_mock_openproject_project_custom() -> None:
    """Test creating a mock OpenProject project with custom values."""
    project = create_mock_openproject_project(
        project_id=100,
        name="Custom Project",
        identifier="custom-project",
        description="Custom description",
    )

    # Check custom values
    assert project["id"] == 100
    assert project["name"] == "Custom Project"
    assert project["identifier"] == "custom-project"
    assert project["description"]["raw"] == "Custom description"
