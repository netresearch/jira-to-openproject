#!/usr/bin/env python3
"""
User migration module for Jira to OpenProject migration.
Handles the migration of users and their accounts from Jira to OpenProject.
"""

import json
import os
from pathlib import Path
from typing import Any

from src import config
from src.display import ProgressTracker
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult
# Get logger from config
logger = config.logger


class UserMigration(BaseMigration):
    """
    Handles the migration of users from Jira to OpenProject.

    Since both systems use LDAP/AD for authentication, the focus is on:
    1. Identifying the users in Jira
    2. Ensuring they exist in OpenProject (via LDAP sync)
    3. Creating a mapping between Jira and OpenProject user IDs for later use
    """

    def __init__(
        self,
        jira_client=None,
        op_client=None,
        data_dir=None,
    ) -> None:
        """
        Initialize the user migration tools.

        Args:
            jira_client: Initialized Jira client instance
            op_client: Initialized OpenProject client instance
            data_dir: Path to data directory for storing mappings
        """
        # Initialize base migration with client dependencies
        super().__init__(
            jira_client=jira_client,
            op_client=op_client,
        )

        # Configure paths
        self.data_dir = Path(data_dir or config.get_path("data"))
        os.makedirs(self.data_dir, exist_ok=True)

        # Data storage
        self.jira_users = []
        self.op_users = []
        self.user_mapping = {}

        # Setup file paths
        self.jira_users_file = self.data_dir / "jira_users.json"
        self.op_users_file = self.data_dir / "op_users.json"
        self.user_mapping_file = self.data_dir / "user_mapping.json"

        # Logging
        self.logger.debug(f"UserMigration initialized with data dir: {self.data_dir}")

        # Load existing data if available
        self.jira_users = self._load_from_json("jira_users.json") or []
        self.op_users = self._load_from_json("op_users.json") or []
        self.user_mapping = self._load_from_json("user_mapping.json") or {}

    def extract_jira_users(self) -> list[dict[str, Any]]:
        """
        Extract users from Jira.

        Returns:
            List of Jira users
        """
        self.logger.info("Extracting users from Jira...", extra={"markup": True})

        self.jira_users = self.jira_client.get_users()

        if not self.jira_users:
            self.logger.error("Failed to extract users from Jira", extra={"markup": True})
            return []

        self.logger.info(
            f"Extracted {len(self.jira_users)} users from Jira", extra={"markup": True}
        )

        self._save_to_json(self.jira_users, "jira_users.json")

        return self.jira_users

    def extract_openproject_users(self) -> list[dict[str, Any]]:
        """
        Extract users from OpenProject.

        Returns:
            List of OpenProject users
        """
        self.logger.info("Extracting users from OpenProject...", extra={"markup": True})

        # Use the standard get_users method which now has the robust implementation
        self.op_users = self.op_client.get_users()

        self.logger.info(
            f"Extracted {len(self.op_users)} users from OpenProject",
            extra={"markup": True},
        )

        self._save_to_json(self.op_users, "op_users.json")

        return self.op_users

    def create_user_mapping(self) -> dict[str, Any]:
        """
        Create a mapping between Jira and OpenProject users.

        Returns:
            Dictionary mapping Jira user keys to OpenProject user IDs
        """
        self.logger.info("Creating user mapping...", extra={"markup": True})

        if not self.jira_users:
            self.extract_jira_users()

        if not self.op_users:
            self.extract_openproject_users()

        op_users_by_username = {
            user.get("login", "").lower(): user for user in self.op_users
        }
        op_users_by_email = {
            user.get("email", "").lower(): user
            for user in self.op_users
            if user.get("email")
        }

        mapping = {}

        with ProgressTracker(
            "Mapping users", len(self.jira_users), "Recent User Mappings"
        ) as tracker:
            for jira_user in self.jira_users:
                jira_key = jira_user.get("key")
                jira_name = jira_user.get("name", "").lower()
                jira_email = jira_user.get("emailAddress", "").lower()
                jira_display_name = jira_user.get("displayName", "")

                tracker.update_description(f"Mapping user: {jira_display_name}")

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
                    tracker.add_log_item(
                        f"Matched by username: {jira_display_name} → {op_user.get('login')}"
                    )
                    tracker.increment()
                    continue

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
                    tracker.add_log_item(
                        f"Matched by email: {jira_display_name} → {op_user.get('login')}"
                    )
                    tracker.increment()
                    continue

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

        self.user_mapping = mapping
        self._save_to_json(mapping, "user_mapping.json")

        total_users = len(mapping)
        matched_users = sum(
            1 for user in mapping.values() if user["matched_by"] != "none"
        )
        match_percentage = (matched_users / total_users) * 100 if total_users > 0 else 0

        self.logger.info(
            f"User mapping created for {total_users} users", extra={"markup": True}
        )
        self.logger.info(
            f"Successfully matched {matched_users} users ({match_percentage:.1f}%)",
            extra={"markup": True},
        )

        return mapping

    def create_missing_users(self, batch_size: int = 10) -> dict[str, Any]:
        """
        Create users in OpenProject that exist in Jira but not in OpenProject.

        This method:
        1. Gets all users from both systems
        2. Finds users that exist in Jira but not in OpenProject
        3. Creates those users in OpenProject

        Args:
            batch_size: Number of users to create in a single batch operation

        Returns:
            Dictionary with stats about created users
        """
        # Get Jira users using Jira client API
        jira_users = self.jira_client.get_users()
        jira_users_dict = {}
        for jira_user in jira_users:
            key = jira_user.get("key")
            if key:
                jira_users_dict[key] = jira_user

        if not jira_users_dict:
            logger.warning("No Jira users found to create in OpenProject")
            return {
                "created_count": 0,
                "total_users": 0
            }

        # Get mapping and existing users
        mapping_dict = self.user_mapping  # Access directly as a dictionary
        existing_emails = set()

        # Get existing OpenProject users to avoid conflicts
        op_users = self.op_client.get_users()
        for user in op_users:
            if user.get("email"):
                existing_emails.add(user.get("email").lower())

        # Prepare users to create in bulk
        users_to_create = []
        for user_key, jira_user in jira_users_dict.items():
            # Skip if user is already mapped
            if user_key in mapping_dict and mapping_dict[user_key] is not None:
                continue

            # Get user details
            email = jira_user.get("emailAddress")
            if not email:
                logger.warning(f"Missing email for Jira user {user_key}, cannot create OpenProject user")
                continue

            # Skip if user already exists in OpenProject
            if email.lower() in existing_emails:
                logger.debug(f"User with email {email} already exists in OpenProject, skipping")
                continue

            # Parse name
            display_name = jira_user.get("displayName", "")
            name_parts = display_name.split(" ", 1)
            firstname = name_parts[0] if len(name_parts) > 0 else "Unknown"
            lastname = name_parts[1] if len(name_parts) > 1 else "User"

            # Create user attributes
            user_attrs = {
                "login": email.split("@")[0],
                "firstname": firstname,
                "lastname": lastname,
                "email": email,
                "admin": False,
                "status": "active"
            }

            # Add to bulk creation list
            users_to_create.append(user_attrs)

        # No users to create
        if not users_to_create:
            logger.info("No new users to create in OpenProject")
            return {
                "created_count": 0,
                "total_users": len(jira_users_dict)
            }

        # Create users in bulk
        logger.info(f"Creating {len(users_to_create)} users in OpenProject in bulk")
        result = self.op_client.create_users_in_bulk(users_to_create)

        created_count = result.get("created_count", 0)
        logger.success(f"Successfully created {created_count} users in OpenProject")

        # Update mapping with newly created users
        created_users = result.get("created_users", [])
        for user in created_users:
            email = user.get("email")
            if not email:
                continue

            # Find matching Jira user
            for user_key, jira_user in jira_users_dict.items():
                if jira_user.get("emailAddress") == email:
                    op_id = user.get("id")
                    if op_id:
                        self.user_mapping[user_key] = str(op_id)
                        break

        # Save the updated mapping
        self._save_to_json(self.user_mapping, "user_mapping.json")

        # Return simple dictionary with created_count to avoid issues with user_mapping format
        return {
            "created_count": created_count,
            "total_users": len(self.user_mapping),
        }

    def analyze_user_mapping(self) -> dict[str, Any]:
        """
        Analyze the user mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.user_mapping:
            mapping_path = os.path.join(self.data_dir, "user_mapping.json")
            if os.path.exists(mapping_path):
                with open(mapping_path) as f:
                    self.user_mapping = json.load(f)
            else:
                self.logger.error(
                    "No user mapping found. Run create_user_mapping() first."
                )
                return {}

        analysis = {
            "total_users": len(self.user_mapping),
            "matched_users": sum(
                1 for user in self.user_mapping.values() if user["matched_by"] != "none"
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
            "matched_by_existing_email": sum(
                1
                for user in self.user_mapping.values()
                if user["matched_by"] == "email_existing"
            ),
            "matched_by_creation": sum(
                1
                for user in self.user_mapping.values()
                if user["matched_by"] == "created"
            ),
            "unmatched_users": sum(
                1 for user in self.user_mapping.values() if user["matched_by"] == "none"
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

        total = analysis["total_users"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_users"] / total) * 100
        else:
            analysis["match_percentage"] = 0

        self._save_to_json(analysis, "user_mapping_analysis.json")

        self.logger.info("User mapping analysis complete")
        self.logger.info(f"Total users: {analysis['total_users']}")
        self.logger.info(
            f"Matched users: {analysis['matched_users']} ({analysis['match_percentage']:.1f}%)"
        )
        self.logger.info(f"- Matched by username: {analysis['matched_by_username']}")
        self.logger.info(f"- Matched by email: {analysis['matched_by_email']}")
        self.logger.info(
            f"- Matched by email (existing): {analysis['matched_by_existing_email']}"
        )
        self.logger.info(f"- Created in OpenProject: {analysis['matched_by_creation']}")
        self.logger.info(f"Unmatched users: {analysis['unmatched_users']}")

        return analysis

    def run(self) -> ComponentResult:
        """
        Run the user migration process.

        Returns:
            Dictionary with migration results
        """
        self.logger.info("Starting user migration", extra={"markup": True})

        try:
            # Extract data
            jira_users = self.extract_jira_users()
            op_users = self.extract_openproject_users()

            # Create mapping
            self.create_user_mapping()

            # Create missing users if not in dry run mode
            self.create_missing_users()

            # Analyze results
            analysis = self.analyze_user_mapping()

            return ComponentResult(
                success=True,
                success_count=analysis["matched_users"],
                failed_count=analysis["unmatched_users"],
                total_count=analysis["total_users"],
                jira_users_count=len(jira_users),
                op_users_count=len(op_users),
                analysis=analysis,
            )
        except Exception as e:
            self.logger.error(
                f"Error during user migration: {str(e)}",
                extra={"markup": True, "traceback": True},
            )
            self.logger.exception(e)
            return ComponentResult(
                success=False,
                error=str(e),
                success_count=0,
                failed_count=len(self.jira_users) if self.jira_users else 0,
                total_count=len(self.jira_users) if self.jira_users else 0,
            )

    def get_jira_users(self) -> dict[str, Any]:
        """
        Get Jira users from the migration data.

        Returns:
            Dictionary of Jira users indexed by key
        """
        users = {}
        for jira_user in self.jira_users:
            key = jira_user.get("key")
            if key:
                users[key] = jira_user
        return users
