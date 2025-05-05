"""
User migration module for Jira to OpenProject migration.
Handles the migration of users and their accounts from Jira to OpenProject.
"""

import json
import os
import re
from pathlib import Path
from typing import Any

import requests
from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
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
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        data_dir: str | None = None,
    ) -> None:
        """
        Initialize the user migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.
            data_dir: Path to data directory for storing mappings.
        """
        super().__init__(jira_client, op_client, None)

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

    def create_missing_users(self) -> dict[str, Any]:
        """
        Create users in OpenProject that don't have a match.

        Returns:
            Updated user mapping with newly created users
        """
        if not self.user_mapping:
            self.create_user_mapping()

        unmatched_users = [
            user for user in self.user_mapping.values() if user["matched_by"] == "none"
        ]

        if not unmatched_users:
            self.logger.info(
                "All users have a match in OpenProject, no need to create new users",
                extra={"markup": True},
            )
            return self.user_mapping

        self.logger.info(
            f"Creating {len(unmatched_users)} missing users in OpenProject...",
            extra={"markup": True},
        )

        created_count = 0
        skipped_count = 0
        failed_count = 0
        found_existing_count = 0

        with ProgressTracker(
            "Creating missing users", len(unmatched_users), "Recently Created Users"
        ) as tracker:
            for user_data in unmatched_users:
                jira_key = user_data["jira_key"]
                jira_name = user_data["jira_name"]
                jira_email = user_data["jira_email"]
                jira_display_name = user_data["jira_display_name"]

                tracker.update_description(f"Creating user: {jira_display_name}")

                if not jira_name or not jira_email:
                    self.logger.warning(
                        f"Skipping user {jira_display_name} - missing username or email",
                        extra={"markup": True},
                    )
                    tracker.add_log_item(f"Skipped (missing data): {jira_display_name}")
                    skipped_count += 1
                    tracker.increment()
                    continue

                cleaned_display_name = jira_display_name
                has_special_handling = False

                # Handle special cases
                if "(" in cleaned_display_name:
                    # Try to clean the display name if it contains parentheses
                    try:
                        cleaned_display_name = re.sub(r'\s*\([^)]*\)', '', cleaned_display_name).strip()
                        has_special_handling = True
                    except Exception as e:
                        self.logger.warning(f"Failed to clean display name {cleaned_display_name}: {str(e)}")

                # First, check if a user with this email already exists
                existing_user = None
                try:
                    existing_user = self.op_client.get_user_by_email(jira_email)
                    if existing_user:
                        self.logger.info(
                            f"Found existing user with email {jira_email}: {existing_user.get('login')}",
                            extra={"markup": True},
                        )
                        # Update mapping
                        self.user_mapping[jira_key] = {
                            "jira_key": jira_key,
                            "jira_name": jira_name,
                            "jira_email": jira_email,
                            "jira_display_name": jira_display_name,
                            "openproject_id": existing_user.get("id"),
                            "openproject_login": existing_user.get("login"),
                            "openproject_email": existing_user.get("email"),
                            "matched_by": "email_existing",
                        }
                        tracker.add_log_item(
                            f"Found existing user: {jira_display_name} → {existing_user.get('login')}"
                        )
                        found_existing_count += 1
                        tracker.increment()
                        continue
                except Exception as e:
                    self.logger.warning(
                        f"Error checking for existing user with email {jira_email}: {str(e)}",
                        extra={"markup": True},
                    )

                # If in dry run mode, skip the actual creation
                if config.migration_config.get("dry_run"):
                    self.logger.info(
                        f"DRY RUN: Would create user {jira_name} ({jira_email})",
                        extra={"markup": True},
                    )
                    tracker.add_log_item(f"Would create: {jira_display_name}")
                    skipped_count += 1
                    tracker.increment()
                    continue

                # Format first/last name from display name
                first_name = ""
                last_name = ""

                # Use special handling to split the name based on spaces
                if has_special_handling and " " in cleaned_display_name:
                    name_parts = cleaned_display_name.split()
                    if len(name_parts) >= 2:
                        first_name = name_parts[0]
                        last_name = " ".join(name_parts[1:])
                    else:
                        first_name = cleaned_display_name
                        last_name = "."
                else:
                    # Otherwise use the regular display name
                    if " " in jira_display_name:
                        name_parts = jira_display_name.split()
                        if len(name_parts) >= 2:
                            first_name = name_parts[0]
                            last_name = " ".join(name_parts[1:])
                        else:
                            first_name = jira_display_name
                            last_name = "."
                    else:
                        first_name = jira_display_name
                        last_name = "."

                # Create the user in OpenProject
                try:
                    # Prepare user data
                    user_data = {
                        "login": jira_name,
                        "email": jira_email,
                        "firstName": first_name,
                        "lastName": last_name,
                        "admin": False,
                        "status": "active",
                        "language": "en",
                    }

                    # Create user with a fake password (since LDAP will be used for authentication)
                    user_data["password"] = "ChangeMe123!"

                    # Use the _request method directly to ensure compatibility with test mocks
                    created_user = self.op_client._request("POST", "/users", data=user_data)

                    if created_user:
                        # Update the mapping with the newly created user
                        self.user_mapping[jira_key] = {
                            "jira_key": jira_key,
                            "jira_name": jira_name,
                            "jira_email": jira_email,
                            "jira_display_name": jira_display_name,
                            "openproject_id": created_user.get("id"),
                            "openproject_login": created_user.get("login"),
                            "openproject_email": created_user.get("email"),
                            "matched_by": "created",
                        }
                        tracker.add_log_item(
                            f"Created: {jira_display_name} → {created_user.get('login')}"
                        )
                        created_count += 1
                    else:
                        tracker.add_log_item(f"Failed to create: {jira_display_name}")
                        failed_count += 1
                except requests.exceptions.HTTPError as e:
                    error_msg = str(e)
                    if "422" in error_msg:  # Validation error
                        # Try to extract error details
                        error_details = "Unknown validation error"
                        try:
                            if hasattr(e, "response") and e.response is not None:
                                error_json = e.response.json()
                                if "_embedded" in error_json and "errors" in error_json["_embedded"]:
                                    errors = error_json["_embedded"]["errors"]
                                    error_details = "; ".join([
                                        error.get("message", "") for error in errors
                                    ])
                        except Exception:
                            pass

                        self.logger.warning(
                            f"Validation error creating user {jira_display_name}: {error_details}",
                            extra={"markup": True},
                        )
                        tracker.add_log_item(f"Validation error: {jira_display_name} - {error_details}")
                    else:
                        self.logger.warning(
                            f"HTTP error creating user {jira_display_name}: {str(e)}",
                            extra={"markup": True},
                        )
                        tracker.add_log_item(f"HTTP error: {jira_display_name} - {str(e)}")
                    failed_count += 1
                except Exception as e:
                    self.logger.warning(
                        f"Error creating user {jira_display_name}: {str(e)}",
                        extra={"markup": True},
                    )
                    tracker.add_log_item(f"Error: {jira_display_name} - {str(e)}")
                    failed_count += 1

                tracker.increment()

        # Save the updated mapping
        self._save_to_json(self.user_mapping, "user_mapping.json")

        self.logger.info(
            f"Created {created_count} users, found {found_existing_count} existing users, "
            f"skipped {skipped_count}, failed {failed_count}",
            extra={"markup": True},
        )

        return self.user_mapping

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
