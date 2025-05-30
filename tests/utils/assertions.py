"""Custom assertion helpers for testing."""

import os
from typing import Any


def assert_projects_equivalent(op_project: dict[str, Any], jira_project: dict[str, Any]) -> None:
    """Assert that an OpenProject project correctly represents a Jira project.

    Args:
        op_project: OpenProject project dictionary
        jira_project: Jira project dictionary

    Raises:
        AssertionError: If projects are not equivalent

    """
    assert op_project["name"] == jira_project["name"]
    assert op_project["identifier"].lower() == jira_project["key"].lower()
    assert op_project["description"]["raw"] is not None


def assert_work_package_equivalent(
    work_package: dict[str, Any],
    jira_issue: dict[str, Any],
) -> None:
    """Assert that an OpenProject work package correctly represents a Jira issue.

    Args:
        work_package: OpenProject work package dictionary
        jira_issue: Jira issue dictionary

    Raises:
        AssertionError: If the work package does not match the issue

    """
    assert work_package["subject"] == jira_issue["fields"]["summary"]
    assert work_package["description"]["raw"] is not None

    # Check embedded properties
    embedded = work_package.get("_embedded", {})
    assert embedded["type"]["name"] is not None
    assert embedded["status"]["name"] is not None

    # Additional validations as needed
    # Note: Exact field mapping may vary based on your migration logic


def assert_file_exists(path: str) -> None:
    """Assert that a file exists.

    Args:
        path: Path to check

    Raises:
        AssertionError: If the file does not exist

    """
    assert os.path.exists(path), f"File does not exist: {path}"
    assert os.path.isfile(path), f"Path exists but is not a file: {path}"


def assert_dir_exists(path: str) -> None:
    """Assert that a directory exists.

    Args:
        path: Path to check

    Raises:
        AssertionError: If the directory does not exist

    """
    assert os.path.exists(path), f"Directory does not exist: {path}"
    assert os.path.isdir(path), f"Path exists but is not a directory: {path}"


def assert_file_not_empty(path: str) -> None:
    """Assert that a file exists and is not empty.

    Args:
        path: Path to check

    Raises:
        AssertionError: If the file does not exist or is empty

    """
    assert_file_exists(path)
    assert os.path.getsize(path) > 0, f"File exists but is empty: {path}"


def assert_dict_subset(subset: dict[str, Any], full_dict: dict[str, Any]) -> None:
    """Assert that all key-value pairs in subset exist in full_dict.

    Args:
        subset: Dictionary that should be a subset
        full_dict: Dictionary that should contain all keys from subset

    Raises:
        AssertionError: If any key-value pair from subset is not in full_dict

    """
    for key, value in subset.items():
        assert key in full_dict, f"Key '{key}' not found in dictionary"

        if isinstance(value, dict) and isinstance(full_dict[key], dict):
            # Recursively check nested dictionaries
            assert_dict_subset(value, full_dict[key])
        else:
            assert full_dict[key] == value, f"Value mismatch for key '{key}'"


def assert_lists_equal_unordered(list1: list[Any], list2: list[Any]) -> None:
    """Assert that two lists contain the same elements, regardless of order.

    Args:
        list1: First list
        list2: Second list

    Raises:
        AssertionError: If lists don't contain the same elements

    """
    assert len(list1) == len(list2), f"Lists have different lengths: {len(list1)} vs {len(list2)}"

    # Convert to sets if elements are hashable, otherwise use a different approach
    try:
        assert set(list1) == set(list2)
    except TypeError:
        # Elements are not hashable, check each element
        for item in list1:
            assert item in list2, f"Item {item} not found in second list"
        for item in list2:
            assert item in list1, f"Item {item} not found in first list"


def assert_migration_complete(migration_result: dict[str, Any]) -> None:
    """Assert that a migration completed successfully.

    Args:
        migration_result: Migration result dictionary

    Raises:
        AssertionError: If the migration did not complete successfully

    """
    assert migration_result["success"] is True, f"Migration failed: {migration_result.get('error', 'Unknown error')}"
    assert "error" not in migration_result, f"Migration had errors: {migration_result.get('error')}"

    # Assert other expected result properties depending on your implementation
    assert "duration" in migration_result, "Migration result missing duration"
