#!/usr/bin/env python3
"""Script to clean up var directories or remove old directories after migration."""

import argparse
import logging
import shutil
from typing import TYPE_CHECKING

from rich.console import Console

from src.config import var_dirs
from src.types import DirType

if TYPE_CHECKING:
    from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("cleanup_var")
console = Console()


def clean_var_directory(dir_name: DirType | None = None, confirm: bool = True) -> bool:
    """Clean up files in the specified var directory or all var directories.

    Args:
        dir_name: Name of the directory to clean (data, logs, backups, output, temp)
                 If None, clean all directories
        confirm: Whether to ask for confirmation before deletion

    """
    # Determine which directories to clean
    dirs_to_clean: dict[DirType, Path] = {}
    if dir_name:
        if dir_name in var_dirs and dir_name != "root":
            dirs_to_clean[dir_name] = var_dirs[dir_name]
        else:
            msg = f"Unknown directory: {dir_name}"
            raise ValueError(msg)
    else:
        # Exclude the root var directory
        dirs_to_clean = {k: v for k, v in var_dirs.items() if k != "root"}

    # Confirm deletion if required
    if confirm:
        dirs_str = ", ".join(dirs_to_clean.keys())
        console.print(f"\nYou are about to delete all contents from: {dirs_str}")
        response = console.input("Are you sure you want to proceed? (y/n): ")
        if response.lower() != "y":
            console.print("Operation cancelled.")
            return False

    # Clean each directory
    for name, path in dirs_to_clean.items():
        if path.exists():
            logger.info("Cleaning directory: %s", name)
            for item in path.glob("*"):
                if item.is_file():
                    item.unlink()
                    logger.info("Deleted file: %s", item)
                elif item.is_dir():
                    shutil.rmtree(item)
                    logger.info("Deleted directory: %s", item)
            logger.info("Directory %s cleaned successfully", name)
        else:
            logger.warning("Directory %s does not exist", name)

    return True


def main() -> bool:
    """Main function to handle directory cleanup."""
    parser = argparse.ArgumentParser(
        description="Clean up var directories or remove old directories.",
    )

    # Add arguments
    parser.add_argument(
        "--clean",
        choices=[k for k in var_dirs if k != "root"] + ["all"],
        help="Clean specified var directory or 'all' for all directories",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip confirmation prompts",
    )

    args = parser.parse_args()

    if not args.clean:
        parser.print_help()
        console.print("\nPlease specify at least one operation.")
        return False

    try:
        # Clean var directories
        if args.clean:
            dir_name = None if args.clean == "all" else args.clean
            clean_var_directory(dir_name, not args.no_confirm)

        logger.info("Cleanup completed successfully!")

    except Exception:
        logger.exception("Error during cleanup")
        return False

    return True


if __name__ == "__main__":
    main()
