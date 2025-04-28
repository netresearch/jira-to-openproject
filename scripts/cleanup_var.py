#!/usr/bin/env python3
"""
Script to clean up var directories or remove old directories after migration.
"""

import argparse
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from src.config import var_dirs

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("cleanup_var")


def clean_var_directory(dir_name: Optional[str] = None, confirm: bool = True) -> bool:
    """
    Clean up files in the specified var directory or all var directories.

    Args:
        dir_name: Name of the directory to clean (data, logs, backups, output, temp)
                 If None, clean all directories
        confirm: Whether to ask for confirmation before deletion
    """
    # Determine which directories to clean
    dirs_to_clean = {}
    if dir_name:
        if dir_name in var_dirs and dir_name != "root":
            dirs_to_clean[dir_name] = var_dirs[dir_name]
        else:
            logger.error(f"Unknown directory: {dir_name}")
            return False
    else:
        # Exclude the root var directory
        dirs_to_clean = {k: v for k, v in var_dirs.items() if k != "root"}

    # Confirm deletion if required
    if confirm:
        dirs_str = ", ".join(dirs_to_clean.keys())
        print(f"\nYou are about to delete all contents from: {dirs_str}")
        response = input("Are you sure you want to proceed? (y/n): ")
        if response.lower() != "y":
            print("Operation cancelled.")
            return False

    # Clean each directory
    for name, path in dirs_to_clean.items():
        if os.path.exists(path):
            logger.info(f"Cleaning directory: {name}")
            for item in Path(path).glob("*"):
                if item.is_file():
                    item.unlink()
                    logger.info(f"Deleted file: {item}")
                elif item.is_dir():
                    shutil.rmtree(item)
                    logger.info(f"Deleted directory: {item}")
            logger.info(f"Directory {name} cleaned successfully")
        else:
            logger.warning(f"Directory {name} does not exist")

    return True


def main():
    """Main function to handle directory cleanup."""
    parser = argparse.ArgumentParser(
        description="Clean up var directories or remove old directories."
    )

    # Add arguments
    parser.add_argument(
        "--clean",
        choices=list(k for k in var_dirs.keys() if k != "root") + ["all"],
        help="Clean specified var directory or 'all' for all directories",
    )
    parser.add_argument(
        "--no-confirm", action="store_true", help="Skip confirmation prompts"
    )

    args = parser.parse_args()

    if not args.clean and not args.remove_old:
        parser.print_help()
        print("\nPlease specify at least one operation.")
        return False

    try:
        # Clean var directories
        if args.clean:
            dir_name = None if args.clean == "all" else args.clean
            clean_var_directory(dir_name, not args.no_confirm)

        logger.info("Cleanup completed successfully!")

    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}")
        return False

    return True


if __name__ == "__main__":
    main()
