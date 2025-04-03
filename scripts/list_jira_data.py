#!/usr/bin/env python3
"""
Script to list Jira data (projects, issue types, statuses, etc.) to help with migration planning.
"""

import sys
import os
import logging
import json
import argparse
from dotenv import load_dotenv

# Add parent directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config_loader import ConfigLoader
from src.jira_client import JiraClient


def setup_logging():
    """
    Set up logging configuration.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )


def save_to_json(data, filename):
    """
    Save data to a JSON file.

    Args:
        data: Data to save
        filename: Filename to save to
    """
    # Create directory if it doesn't exist
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    with open(filename, "w") as f:
        json.dump(data, f, indent=2, default=str)


def list_projects(jira_client, args):
    """
    List Jira projects.

    Args:
        jira_client: Jira client
        args: Command-line arguments
    """
    logger = logging.getLogger(__name__)

    logger.info("Listing Jira projects...")
    projects = jira_client.get_projects()

    logger.info(f"Found {len(projects)} projects")

    # Print project details
    project_data = []
    for project in projects:
        project_info = {
            "key": project.key,
            "name": project.name,
            "lead": getattr(project, "lead", {}).get("displayName", "Unknown"),
            "id": project.id,
        }
        project_data.append(project_info)
        logger.info(f" - {project.key}: {project.name}")

    # Save to JSON if requested
    if args.output:
        save_to_json(project_data, os.path.join(args.output, "jira_projects.json"))
        logger.info(
            f"Saved project data to {os.path.join(args.output, 'jira_projects.json')}"
        )


def list_issue_types(jira_client, args):
    """
    List Jira issue types.

    Args:
        jira_client: Jira client
        args: Command-line arguments
    """
    logger = logging.getLogger(__name__)

    logger.info("Listing Jira issue types...")
    issue_types = jira_client.client.issue_types()

    logger.info(f"Found {len(issue_types)} issue types")

    # Print issue type details
    issue_type_data = []
    for issue_type in issue_types:
        issue_type_info = {
            "id": issue_type.id,
            "name": issue_type.name,
            "description": issue_type.description,
            "subtask": issue_type.subtask,
        }
        issue_type_data.append(issue_type_info)
        logger.info(
            f" - {issue_type.name} ({'subtask' if issue_type.subtask else 'standard'})"
        )

    # Save to JSON if requested
    if args.output:
        save_to_json(
            issue_type_data, os.path.join(args.output, "jira_issue_types.json")
        )
        logger.info(
            f"Saved issue type data to {os.path.join(args.output, 'jira_issue_types.json')}"
        )


def list_statuses(jira_client, args):
    """
    List Jira statuses.

    Args:
        jira_client: Jira client
        args: Command-line arguments
    """
    logger = logging.getLogger(__name__)

    logger.info("Listing Jira statuses...")
    statuses = jira_client.client.statuses()

    logger.info(f"Found {len(statuses)} statuses")

    # Print status details
    status_data = []
    for status in statuses:
        status_info = {
            "id": status.id,
            "name": status.name,
            "description": getattr(status, "description", ""),
        }
        status_data.append(status_info)
        logger.info(f" - {status.name}")

    # Save to JSON if requested
    if args.output:
        save_to_json(status_data, os.path.join(args.output, "jira_statuses.json"))
        logger.info(
            f"Saved status data to {os.path.join(args.output, 'jira_statuses.json')}"
        )


def list_priorities(jira_client, args):
    """
    List Jira priorities.

    Args:
        jira_client: Jira client
        args: Command-line arguments
    """
    logger = logging.getLogger(__name__)

    logger.info("Listing Jira priorities...")
    priorities = jira_client.client.priorities()

    logger.info(f"Found {len(priorities)} priorities")

    # Print priority details
    priority_data = []
    for priority in priorities:
        priority_info = {
            "id": priority.id,
            "name": priority.name,
            "description": getattr(priority, "description", ""),
        }
        priority_data.append(priority_info)
        logger.info(f" - {priority.name}")

    # Save to JSON if requested
    if args.output:
        save_to_json(priority_data, os.path.join(args.output, "jira_priorities.json"))
        logger.info(
            f"Saved priority data to {os.path.join(args.output, 'jira_priorities.json')}"
        )


def list_custom_fields(jira_client, args):
    """
    List Jira custom fields.

    Args:
        jira_client: Jira client
        args: Command-line arguments
    """
    logger = logging.getLogger(__name__)

    logger.info("Listing Jira custom fields...")

    # Get all field definitions
    fields = jira_client.client.fields()

    # Filter for custom fields
    custom_fields = [field for field in fields if field["custom"]]

    logger.info(f"Found {len(custom_fields)} custom fields")

    # Print custom field details
    for field in custom_fields:
        logger.info(
            f" - {field['name']}: {field['id']} ({field['schema'].get('type', 'unknown type')})"
        )

    # Save to JSON if requested
    if args.output:
        save_to_json(
            custom_fields, os.path.join(args.output, "jira_custom_fields.json")
        )
        logger.info(
            f"Saved custom field data to {os.path.join(args.output, 'jira_custom_fields.json')}"
        )


def main():
    """
    Main function to list Jira data.
    """
    # Define command-line arguments
    parser = argparse.ArgumentParser(
        description="List Jira data for migration planning"
    )
    parser.add_argument(
        "--type",
        choices=[
            "projects",
            "issue-types",
            "statuses",
            "priorities",
            "custom-fields",
            "all",
        ],
        default="all",
        help="Type of data to list",
    )
    parser.add_argument("--output", help="Directory to save JSON output")
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Loading configuration...")
    # Load environment variables
    load_dotenv()

    # Load config
    config_loader = ConfigLoader()
    jira_config = config_loader.get_jira_config()

    # Check if Jira configuration is complete
    if not jira_config.get("url") or not jira_config.get("api_token"):
        logger.error("Jira configuration is incomplete. Please check your .env file.")
        sys.exit(1)

    # Initialize Jira client
    jira_client = JiraClient(jira_config)

    # Test connection
    if not jira_client.test_connection():
        logger.error("Failed to connect to Jira")
        sys.exit(1)

    # Create output directory if specified
    if args.output:
        os.makedirs(args.output, exist_ok=True)

    # Call appropriate function based on type argument
    if args.type == "projects" or args.type == "all":
        list_projects(jira_client, args)

    if args.type == "issue-types" or args.type == "all":
        list_issue_types(jira_client, args)

    if args.type == "statuses" or args.type == "all":
        list_statuses(jira_client, args)

    if args.type == "priorities" or args.type == "all":
        list_priorities(jira_client, args)

    if args.type == "custom-fields" or args.type == "all":
        list_custom_fields(jira_client, args)


if __name__ == "__main__":
    main()
