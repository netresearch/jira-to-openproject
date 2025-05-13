#!/usr/bin/env python3
"""Script to test connection to both Jira and OpenProject APIs.
"""

import logging

from dotenv import load_dotenv

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.config_loader import ConfigLoader


def setup_logging() -> None:
    """Set up logging configuration.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )


def main() -> None:
    """Main function to test connections.
    """
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Loading configuration...")
    # Load environment variables
    load_dotenv()

    # Load config
    config_loader = ConfigLoader()

    # Test Jira connection
    logger.info("Testing Jira connection...")
    jira_config = config_loader.get_jira_config()
    if not jira_config.get("url") or not jira_config.get("api_token"):
        logger.error("Jira configuration is incomplete. Please check your .env file.")
    else:
        jira_client = JiraClient()
        logger.info("Jira connection successful!")
        # List projects
        projects = jira_client.get_projects()
        logger.info(f"Found {len(projects)} projects in Jira")
        for project in projects[:5]:  # Show first 5 projects
            logger.info(f" - {project['key']}: {project['name']}")
        if len(projects) > 5:
            logger.info(f" - ... and {len(projects) - 5} more")

    # Test OpenProject connection
    logger.info("Testing OpenProject connection...")
    openproject_config = config_loader.get_openproject_config()
    if not openproject_config.get("url") or not openproject_config.get("api_token"):
        logger.error("OpenProject configuration is incomplete. Please check your .env file.")
    else:
        openproject_client = OpenProjectClient()
        try:
            projects = openproject_client.get_projects()
            logger.info("OpenProject connection successful!")
            logger.info(f"Found {len(projects)} projects in OpenProject")
            for project in projects[:5]:  # Show first 5 projects
                logger.info(f" - {project.get('identifier')}: {project.get('name')}")
            if len(projects) > 5:
                logger.info(f" - ... and {len(projects) - 5} more")
        except Exception as e:
            logger.error(f"Failed to connect to OpenProject: {e!s}")
            logger.error("Check your API key and URL configuration")


if __name__ == "__main__":
    main()
