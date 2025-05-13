#!/usr/bin/env python3
"""
Script to update imports from OpenProjectRailsClient to OpenProjectClient.
"""

import argparse
import re
from pathlib import Path


def update_file(file_path: str, dry_run: bool = False) -> bool:
    """
    Update imports in a single file.

    Args:
        file_path: Path to the file to update
        dry_run: If True, don't make changes, just print what would be changed

    Returns:
        True if changes were made or would be made, False otherwise
    """
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    # Find the import statement
    import_pattern = re.compile(r"from src\.clients\.openproject_rails_client import OpenProjectRailsClient")
    if not import_pattern.search(content):
        return False

    # Replace import statement
    new_content = import_pattern.sub("from src.clients.openproject_client import OpenProjectClient", content)

    # Replace class references
    new_content = re.sub(r"OpenProjectRailsClient\(", "OpenProjectClient(", new_content)
    new_content = re.sub(r"OpenProjectRailsClient\.", "OpenProjectClient.", new_content)
    new_content = re.sub(
        r"isinstance\((.+?), OpenProjectRailsClient\)", r"isinstance(\1, OpenProjectClient)", new_content
    )

    if content == new_content:
        print(f"No changes needed in {file_path}")
        return False

    if dry_run:
        print(f"Would update {file_path}")
        return True

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"Updated {file_path}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Update imports from OpenProjectRailsClient to OpenProjectClient")
    parser.add_argument("--dry-run", action="store_true", help="Don't make changes, just print what would be changed")
    args = parser.parse_args()

    root_dir = Path(__file__).parent.parent

    # Define directories to search
    dirs_to_search = [
        root_dir / "src" / "migrations",
        root_dir / "tests",
        root_dir / "src",
    ]

    # Find all Python files and update them
    changed_files = 0
    for dir_path in dirs_to_search:
        for py_file in dir_path.glob("**/*.py"):
            if update_file(str(py_file), args.dry_run):
                changed_files += 1

    if args.dry_run:
        print(f"Would update {changed_files} files")
    else:
        print(f"Updated {changed_files} files")


if __name__ == "__main__":
    main()
