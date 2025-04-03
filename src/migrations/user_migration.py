"""
User migration module for Jira to OpenProject migration.
Handles the migration of users and their accounts from Jira to OpenProject.
"""

import os
import sys
import json
import pandas as pd
from typing import Dict, List, Any, Optional
import requests
import re

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src import config
from src.display import ProgressTracker, console

# Get logger from config
logger = config.logger


class UserMigration:
    """
    Handles the migration of users from Jira to OpenProject.

    Since both systems use LDAP/AD for authentication, the focus is on:
    1. Identifying the users in Jira
    2. Ensuring they exist in OpenProject (via LDAP sync)
    3. Creating a mapping between Jira and OpenProject user IDs for later use
    """

    def __init__(self, dry_run: bool = False):
        """
        Initialize the user migration tools.

        Args:
            dry_run: If True, no changes will be made to OpenProject
        """
        self.jira_client = JiraClient()
        self.op_client = OpenProjectClient()
        self.jira_users = []
        self.op_users = []
        self.user_mapping = {}
        self.dry_run = dry_run

        # Use the centralized config for var directories
        self.data_dir = config.get_path("data")

    def extract_jira_users(self) -> List[Dict[str, Any]]:
        """
        Extract users from Jira.

        Returns:
            List of Jira users
        """
        logger.info("Extracting users from Jira...", extra={"markup": True})

        self.jira_users = self.jira_client.get_users()

        if not self.jira_users:
            logger.error("Failed to extract users from Jira", extra={"markup": True})
            return []

        # Log the number of users found
        logger.info(f"Extracted {len(self.jira_users)} users from Jira", extra={"markup": True})

        # Save users to file for later reference
        self._save_to_json(self.jira_users, "jira_users.json")

        return self.jira_users

    def extract_openproject_users(self) -> List[Dict[str, Any]]:
        """
        Extract users from OpenProject.

        Returns:
            List of OpenProject users
        """
        logger.info("Extracting users from OpenProject...", extra={"markup": True})

        # Get all users from OpenProject
        self.op_users = self.op_client.get_users()

        # Log the number of users found
        logger.info(f"Extracted {len(self.op_users)} users from OpenProject", extra={"markup": True})

        # Save users to file for later reference
        self._save_to_json(self.op_users, "openproject_users.json")

        return self.op_users

    def create_user_mapping(self) -> Dict[str, Any]:
        """
        Create a mapping between Jira and OpenProject users.

        This method creates a mapping based on usernames and email addresses.

        Returns:
            Dictionary mapping Jira user keys to OpenProject user IDs
        """
        logger.info("Creating user mapping...", extra={"markup": True})

        # Make sure we have users from both systems
        if not self.jira_users:
            self.extract_jira_users()

        if not self.op_users:
            self.extract_openproject_users()

        # Create lookup dictionaries for OpenProject users
        op_users_by_username = {
            user.get("login", "").lower(): user for user in self.op_users
        }
        op_users_by_email = {
            user.get("email", "").lower(): user for user in self.op_users if user.get("email")
        }

        mapping = {}

        with ProgressTracker("Mapping users", len(self.jira_users), "Recent User Mappings") as tracker:
            # Map Jira users to OpenProject users
            for jira_user in self.jira_users:
                jira_key = jira_user.get("key")
                jira_name = jira_user.get("name", "").lower()
                jira_email = jira_user.get("emailAddress", "").lower()
                jira_display_name = jira_user.get("displayName", "")

                tracker.update_description(f"Mapping user: {jira_display_name}")

                # Try to find the corresponding OpenProject user
                # First by username/login
                if jira_name in op_users_by_username:
                    op_user = op_users_by_username[jira_name]
                    mapping[jira_key] = {
                        "jira_key": jira_key,
                        "jira_name": jira_name,
                        "jira_email": jira_email,
                        "jira_display_name": jira_display_name,
                        "openproject_id": op_user.get("id"),
                        "openproject_login": op_user.get("login"),
                        "openproject_email": op_user.get("email"),
                        "matched_by": "username",
                    }
                    tracker.add_log_item(f"Matched by username: {jira_display_name} → {op_user.get('login')}")
                    tracker.increment()
                    continue

                # Then by email
                if jira_email and jira_email in op_users_by_email:
                    op_user = op_users_by_email[jira_email]
                    mapping[jira_key] = {
                        "jira_key": jira_key,
                        "jira_name": jira_name,
                        "jira_email": jira_email,
                        "jira_display_name": jira_display_name,
                        "openproject_id": op_user.get("id"),
                        "openproject_login": op_user.get("login"),
                        "openproject_email": op_user.get("email"),
                        "matched_by": "email",
                    }
                    tracker.add_log_item(f"Matched by email: {jira_display_name} → {op_user.get('login')}")
                    tracker.increment()
                    continue

                # If no match found, add to mapping with empty OpenProject data
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": jira_name,
                    "jira_email": jira_email,
                    "jira_display_name": jira_display_name,
                    "openproject_id": None,
                    "openproject_login": None,
                    "openproject_email": None,
                    "matched_by": "none",
                }
                tracker.add_log_item(f"No match found: {jira_display_name}")
                tracker.increment()

        # Save mapping to file
        self.user_mapping = mapping
        self._save_to_json(mapping, "user_mapping.json")

        # Log statistics
        total_users = len(mapping)
        matched_users = sum(
            1 for user in mapping.values() if user["matched_by"] != "none"
        )
        match_percentage = (
            (matched_users / total_users) * 100 if total_users > 0 else 0
        )

        logger.info(f"User mapping created for {total_users} users", extra={"markup": True})
        logger.info(
            f"Successfully matched {matched_users} users ({match_percentage:.1f}%)",
            extra={"markup": True}
        )

        return mapping

    def analyze_user_mapping(self) -> Dict[str, Any]:
        """
        Analyze the user mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.user_mapping:
            if os.path.exists(os.path.join(self.data_dir, "user_mapping.json")):
                with open(os.path.join(self.data_dir, "user_mapping.json"), "r") as f:
                    self.user_mapping = json.load(f)
            else:
                self.create_user_mapping()

        # Analyze the mapping
        analysis = {
            "total_users": len(self.user_mapping),
            "matched_users": sum(
                1 for user in self.user_mapping.values() if user["matched_by"] != "none"
            ),
            "unmatched_users": sum(
                1 for user in self.user_mapping.values() if user["matched_by"] == "none"
            ),
            "matched_by_username": sum(
                1
                for user in self.user_mapping.values()
                if user["matched_by"] == "username"
            ),
            "matched_by_email": sum(
                1
                for user in self.user_mapping.values()
                if user["matched_by"] == "email"
            ),
            "unmatched_details": [
                {
                    "jira_key": user["jira_key"],
                    "jira_name": user["jira_name"],
                    "jira_email": user["jira_email"],
                    "jira_display_name": user["jira_display_name"],
                }
                for user in self.user_mapping.values()
                if user["matched_by"] == "none"
            ],
        }

        # Add match percentage
        analysis["match_percentage"] = (
            (analysis["matched_users"] / analysis["total_users"]) * 100
            if analysis["total_users"] > 0
            else 0
        )

        # Save analysis to file
        self._save_to_json(analysis, "user_mapping_analysis.json")

        # Log analysis summary
        logger.info(f"User mapping analysis complete", extra={"markup": True})
        logger.info(f"Total users: {analysis['total_users']}", extra={"markup": True})
        logger.info(
            f"Matched users: {analysis['matched_users']} ({analysis['match_percentage']:.1f}%)",
            extra={"markup": True}
        )
        logger.info(f"- Matched by username: {analysis['matched_by_username']}", extra={"markup": True})
        logger.info(f"- Matched by email: {analysis['matched_by_email']}", extra={"markup": True})
        logger.info(f"Unmatched users: {analysis['unmatched_users']}", extra={"markup": True})

        return analysis

    def _save_to_json(self, data: Any, filename: str):
        """
        Save data to a JSON file.

        Args:
            data: Data to save
            filename: Name of the file to save to
        """
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved data to {filepath}", extra={"markup": True})


def run_user_migration(dry_run: bool = False):
    """
    Run the user migration as a standalone script.

    Args:
        dry_run: If True, no changes will be made to OpenProject
    """
    logger.info("Starting user migration", extra={"markup": True})

    # The dry_run parameter is included for consistency with other migration scripts,
    # but it's not used in this module as it doesn't make changes to OpenProject
    if dry_run:
        logger.info("Running in dry-run mode (though no changes are made in this module anyway)", extra={"markup": True})

    migration = UserMigration(dry_run=dry_run)

    # Extract users from both systems
    migration.extract_jira_users()
    migration.extract_openproject_users()

    # Create and analyze the user mapping
    migration.create_user_mapping()
    migration.analyze_user_mapping()

    logger.info("User migration complete", extra={"markup": True})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate users from Jira to OpenProject"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no changes to OpenProject)",
    )
    args = parser.parse_args()

    run_user_migration(dry_run=args.dry_run)
