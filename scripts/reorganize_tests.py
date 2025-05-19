#!/usr/bin/env python3
"""Script to reorganize test files into appropriate directories."""

import os
import re
import shutil
from pathlib import Path

# Define categories and their patterns
CATEGORIES = {
    "unit": [
        # Files that test isolated components with mocks
        r"test_base_migration\.py",
        r"test_environment\.py",
        r"test_data_handler\.py",
    ],
    "functional": [
        # Files that test interactions between components
        r"test_company_migration\.py",
        r"test_custom_field_migration\.py",
        r"test_issue_type_migration\.py",
        r"test_link_type_migration\.py",
        r"test_project_migration\.py",
        r"test_status_migration\.py",
        r"test_user_migration\.py",
        r"test_workflow_migration\.py",
        r"test_work_package_migration\.py",
        r"test_account_migration\.py",
        r"test_project_hierarchy\.py",
        r"test_bulk_user_creation\.py",
    ],
    "integration": [
        # Files that test integration with external services
        r"test_jira_client\.py",
        r"test_openproject_client\.py",
        r"test_rails_console_client\.py",
        r"test_ssh_client\.py",
        r"test_docker_client\.py",
        r"test_file_manager\.py",
        r"test_main\.py",
    ],
}


def categorize_file(filename: str) -> str:
    """Determine the appropriate category for a test file.

    Args:
        filename: The test filename

    Returns:
        str: The category ("unit", "functional", "integration", or "unknown")
    """
    basename = os.path.basename(filename)

    for category, patterns in CATEGORIES.items():
        for pattern in patterns:
            if re.match(pattern, basename):
                return category

    return "unknown"


def move_file(source: Path, category: str) -> None:
    """Move a file to the appropriate category directory.

    Args:
        source: Source file path
        category: Target category
    """
    if category == "unknown":
        print(f"Skipping {source} - unable to determine category")
        return

    target_dir = Path("tests") / category
    target_file = target_dir / source.name

    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)

    # Check if file already exists
    if target_file.exists():
        print(f"Warning: {target_file} already exists, skipping")
        return

    # Move the file
    print(f"Moving {source} -> {target_file}")
    shutil.move(str(source), str(target_file))


def main() -> None:
    """Main function to reorganize test files."""
    # Ensure we're in the project root
    if not Path("tests").exists():
        print("Error: 'tests' directory not found. Run from project root.")
        return

    # Get all test files in the top-level tests directory
    test_dir = Path("tests")
    test_files = list(test_dir.glob("test_*.py"))

    # Skip if no files found
    if not test_files:
        print("No test files found in the top-level tests directory.")
        return

    print(f"Found {len(test_files)} test files to reorganize.")

    # Move each file to its appropriate directory
    for file_path in test_files:
        category = categorize_file(file_path.name)
        move_file(file_path, category)

    print("Done reorganizing test files.")


if __name__ == "__main__":
    main()
