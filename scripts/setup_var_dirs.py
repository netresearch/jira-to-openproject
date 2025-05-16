#!/usr/bin/env python3
"""Script to set up the var directory structure for data, logs, backups, and output."""

import logging

from src.config import var_dirs

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("setup_var_dirs")


def create_var_directories() -> bool:
    """Create the var directory structure."""
    logger.info("Creating var directory structure...")

    # All directories are already created by the config module
    # Just log information about them
    logger.info(f"Created main var directory: {var_dirs['root']}")

    # Log each subdirectory
    for name, path in var_dirs.items():
        if name != "root":
            logger.info(f"Created {name} directory: {path}")

    return True


def main() -> bool:
    """Main function to set up the var directory structure."""
    try:
        # Create directories
        create_var_directories()

        logger.info("Var directory setup complete!")
        print(f"\nVar directory structure created at: {var_dirs['root']}")
        print("The following directories are now available:")
        for name, path in var_dirs.items():
            if name != "root":
                print(f"- {name}: {path}")
        print("\nYou may now update your code to use these new paths.")
        print("After confirming everything works, you can clean up the old directories.")

    except Exception as e:
        logger.exception(f"Error setting up var directories: {e!s}")
        return False

    return True


if __name__ == "__main__":
    main()
