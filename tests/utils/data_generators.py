"""Functions to generate test data for various test scenarios."""

import json
import random
import string
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def generate_random_string(length: int = 10) -> str:
    """Generate a random string of fixed length.

    Args:
        length: Length of the string to generate

    Returns:
        str: Random string

    """
    letters = string.ascii_letters + string.digits
    return "".join(random.choice(letters) for _ in range(length))


def generate_random_jira_key() -> str:
    """Generate a random Jira project key.

    Returns:
        str: Random 3-5 character uppercase Jira project key

    """
    length = random.randint(3, 5)
    return "".join(random.choice(string.ascii_uppercase) for _ in range(length))


def generate_uuid() -> str:
    """Generate a random UUID.

    Returns:
        str: Random UUID

    """
    return str(uuid.uuid4())


def generate_timestamp(days_ago: int = 0) -> str:
    """Generate an ISO format timestamp.

    Args:
        days_ago: Number of days to subtract from current date

    Returns:
        str: ISO format timestamp

    """
    date = datetime.now() - timedelta(days=days_ago)
    return date.isoformat()


def generate_jira_project(
    project_key: str | None = None,
    name: str | None = None,
    num_custom_fields: int = 3,
) -> dict[str, Any]:
    """Generate a Jira project with random data.

    Args:
        project_key: Optional project key (generated if None)
        name: Optional project name (generated if None)
        num_custom_fields: Number of custom fields to generate

    Returns:
        Dict[str, Any]: Dictionary representing a Jira project

    """
    if project_key is None:
        project_key = generate_random_jira_key()

    if name is None:
        name = f"Project {project_key}"

    project_id = random.randint(10000, 99999)

    custom_fields = {}
    for i in range(num_custom_fields):
        field_id = f"customfield_{10000 + i}"
        custom_fields[field_id] = f"Custom Field {i}"

    return {
        "id": str(project_id),
        "key": project_key,
        "name": name,
        "projectTypeKey": "software",
        "description": f"This is a test project {project_key}",
        "lead": {"name": "admin"},
        "url": f"https://jira.example.com/projects/{project_key}",
        "projectCategory": {"name": "Test Category"},
        "customFields": custom_fields,
        "simplified": False,
        "style": "classic",
        "isPrivate": False,
    }


def generate_jira_issue_data(
    project_key: str,
    num_issues: int = 10,
    issue_types: list[str] | None = None,
    statuses: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Generate multiple Jira issues for a project.

    Args:
        project_key: Jira project key
        num_issues: Number of issues to generate
        issue_types: List of issue types to use (defaults to standard types)
        statuses: List of statuses to use (defaults to standard statuses)

    Returns:
        List[Dict[str, Any]]: List of dictionaries representing Jira issues

    """
    if issue_types is None:
        issue_types = ["Bug", "Task", "Story", "Epic"]

    if statuses is None:
        statuses = ["Open", "In Progress", "Resolved", "Closed"]

    results = []
    for i in range(1, num_issues + 1):
        issue_type = random.choice(issue_types)
        status = random.choice(statuses)

        issue = {
            "id": str(random.randint(10000, 99999)),
            "key": f"{project_key}-{i}",
            "fields": {
                "summary": f"Test issue {i} for {project_key}",
                "description": f"This is a test issue {i} for project {project_key}",
                "issuetype": {"name": issue_type},
                "status": {"name": status},
                "assignee": (
                    {"name": "john.doe"} if random.choice([True, False]) else None
                ),
                "reporter": {"name": "jane.doe"},
                "priority": {"name": random.choice(["High", "Medium", "Low"])},
                "project": {
                    "key": project_key,
                    "id": str(random.randint(10000, 99999)),
                },
                "created": generate_timestamp(random.randint(10, 100)),
                "updated": generate_timestamp(random.randint(0, 10)),
                "resolutiondate": (
                    generate_timestamp(0) if status in ["Resolved", "Closed"] else None
                ),
            },
        }
        results.append(issue)

    return results


def generate_op_project_data(
    identifier: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Generate OpenProject project data.

    Args:
        identifier: Optional project identifier (generated if None)
        name: Optional project name (generated if None)

    Returns:
        Dict[str, Any]: Dictionary representing an OpenProject project

    """
    if identifier is None:
        identifier = generate_random_string(8).lower()

    if name is None:
        name = f"Project {identifier.capitalize()}"

    project_id = random.randint(1, 999)

    return {
        "id": project_id,
        "name": name,
        "identifier": identifier,
        "description": {"raw": f"This is a test project {name}"},
        "createdAt": generate_timestamp(10),
        "updatedAt": generate_timestamp(5),
        "_links": {
            "self": {
                "href": f"/api/v3/projects/{project_id}",
                "title": name,
            },
        },
    }


def generate_config_file(
    output_path: Path,
    config_data: dict[str, Any],
) -> Path:
    """Generate a JSON configuration file for testing.

    Args:
        output_path: Path where the config file should be saved
        config_data: Data to write to the config file

    Returns:
        Path: Path to the generated config file

    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w") as f:
        json.dump(config_data, f, indent=2)

    return output_path


def generate_test_migration_config(
    output_dir: Path,
    jira_project_key: str = "TEST",
    op_identifier: str = "test",
) -> tuple[Path, dict[str, Any]]:
    """Generate a test migration configuration file.

    Args:
        output_dir: Directory to save the config file
        jira_project_key: Jira project key to use in the config
        op_identifier: OpenProject identifier to use in the config

    Returns:
        Tuple[Path, Dict[str, Any]]: Path to the config file and the config data

    """
    config_data = {
        "jira": {
            "url": "https://jira-test.example.com",
            "username": "test-user",
            "password": "test-password",
            "project_key": jira_project_key,
        },
        "openproject": {
            "url": "https://openproject-test.example.com",
            "api_token": "test-token",
            "project_identifier": op_identifier,
        },
        "migration": {
            "include_attachments": True,
            "include_comments": True,
            "map_users": True,
            "default_user": "admin",
        },
    }

    output_path = output_dir / "test_migration_config.json"
    return generate_config_file(output_path, config_data), config_data
