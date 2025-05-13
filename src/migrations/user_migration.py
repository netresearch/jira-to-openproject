#!/usr/bin/env python3
"""
User migration module for Jira to OpenProject migration.
Handles the migration of users and their accounts from Jira to OpenProject.
"""

import os
from pathlib import Path

from src import config
from src.display import ProgressTracker
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult, MigrationError

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

    def extract_jira_users(self) -> list:
        """
        Extract users from Jira.

        Returns:
            List of Jira users

        Raises:
            MigrationError: If users cannot be extracted from Jira
        """
        self.logger.info("Extracting users from Jira...", extra={"markup": True})

        self.jira_users = self.jira_client.get_users()

        if not self.jira_users:
            raise MigrationError("Failed to extract users from Jira")

        self.logger.info(f"Extracted {len(self.jira_users)} users from Jira", extra={"markup": True})

        self._save_to_json(self.jira_users, "jira_users.json")

        return self.jira_users

    def extract_openproject_users(self) -> list:
        """
        Extract users from OpenProject.

        Returns:
            List of OpenProject users

        Raises:
            MigrationError: If users cannot be extracted from OpenProject
        """
        self.logger.info("Extracting users from OpenProject...", extra={"markup": True})

        # Get users from OpenProject - no fallbacks or mocks
        self.op_users = self.op_client.get_users()

        if not self.op_users:
            raise MigrationError("Failed to extract users from OpenProject")

        self.logger.info(
            f"Extracted {len(self.op_users)} users from OpenProject",
            extra={"markup": True},
        )

        self._save_to_json(self.op_users, "op_users.json")

        return self.op_users

    def create_user_mapping(self) -> dict:
        """
        Create a mapping between Jira and OpenProject users.

        Returns:
            Dictionary mapping Jira user keys to OpenProject user IDs

        Raises:
            MigrationError: If required user data is missing
        """
        self.logger.info("Creating user mapping...", extra={"markup": True})

        if not self.jira_users:
            self.extract_jira_users()

        if not self.op_users:
            self.extract_openproject_users()

        op_users_by_username = {user.get("login", "").lower(): user for user in self.op_users}
        op_users_by_email = {user.get("email", "").lower(): user for user in self.op_users if user.get("email")}

        mapping = {}

        with ProgressTracker("Mapping users", len(self.jira_users), "Recent User Mappings") as tracker:
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
                    tracker.add_log_item(f"Matched by username: {jira_display_name} → {op_user.get('login')}")
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
                    tracker.add_log_item(f"Matched by email: {jira_display_name} → {op_user.get('login')}")
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

        # Save the mapping
        self._save_to_json(mapping, "user_mapping.json")
        self.user_mapping = mapping

        return mapping

    def create_missing_users(self, batch_size: int = 10) -> dict:
        """
        Create missing users in OpenProject using the LDAP synchronization.

        Args:
            batch_size: Number of users to create in each batch

        Returns:
            Dictionary with results of user creation

        Raises:
            MigrationError: If user mapping is missing or if users cannot be created
        """
        self.logger.info("Creating missing users in OpenProject...", extra={"markup": True})

        if not self.user_mapping:
            self.create_user_mapping()

        missing_users = [user for user in self.user_mapping.values() if user["matched_by"] == "none"]

        if not missing_users:
            self.logger.info("No missing users to create", extra={"markup": True})
            return {"created": 0, "failed": 0, "total": 0}

        self.logger.info(
            f"Found {len(missing_users)} users missing in OpenProject",
            extra={"markup": True},
        )

        created = 0
        failed = 0
        created_users = []

        with ProgressTracker("Creating users", len(missing_users), "Recent User Creations") as tracker:
            for i in range(0, len(missing_users), batch_size):
                batch = missing_users[i : i + batch_size]

                # Prepare data for user creation
                users_to_create = []
                for user in batch:
                    # Split display name into first and last name
                    names = user["jira_display_name"].split(" ", 1)
                    first_name = names[0] if len(names) > 0 else "User"
                    last_name = names[1] if len(names) > 1 else user["jira_name"]

                    users_to_create.append(
                        {
                            "login": user["jira_name"],
                            "firstname": first_name,
                            "lastname": last_name,
                            "mail": user["jira_email"],
                            "admin": False,
                            "status": "active",
                        }
                    )

                batch_users = [user["jira_name"] for user in batch]
                tracker.update_description(f"Creating users: {', '.join(batch_users)}")

                try:
                    # Create users in bulk - response is now a string instead of a dict
                    result_str = self.op_client.create_users_in_bulk(users_to_create)

                    # Parse the JSON string to extract the needed information
                    # If string parsing fails, use a reasonable default
                    try:
                        import json
                        import re

                        # First try standard JSON parsing
                        try:
                            result = json.loads(result_str)
                        except json.JSONDecodeError:
                            # Fall back to regex extraction if there are Ruby hash markers
                            # Extract data between curly braces
                            match = re.search(r"\{.*\}", result_str, re.DOTALL)
                            if match:
                                # Convert Ruby hash string to JSON format
                                json_str = match.group(0)
                                json_str = re.sub(r":(\w+)\s*=>", r'"\1":', json_str)
                                json_str = json_str.replace("=>", ":")
                                try:
                                    result = json.loads(json_str)
                                except:
                                    # If everything fails, rely on string counts for basic success metrics
                                    self.logger.warning("Could not parse JSON, using basic string parsing")
                                    batch_created = result_str.count('"status": "success"') or result_str.count(
                                        '"status" => "success"'
                                    )
                                    batch_failed = len(batch) - batch_created
                                    result = {"created_count": batch_created, "created_users": [], "failed_users": []}
                            else:
                                # No JSON-like structure found
                                batch_created = 0
                                batch_failed = len(batch)
                                result = {"created_count": 0, "created_users": [], "failed_users": []}
                    except ImportError:
                        # If somehow json module is not available
                        self.logger.warning("JSON module not available, using basic string parsing")
                        batch_created = result_str.count('"status": "success"') or result_str.count(
                            '"status" => "success"'
                        )
                        batch_failed = len(batch) - batch_created
                        result = {"created_count": batch_created, "created_users": [], "failed_users": []}

                    # Extract result stats
                    batch_created = result.get("created_count", 0)
                    if isinstance(batch_created, str) and batch_created.isdigit():
                        batch_created = int(batch_created)
                    batch_failed = len(batch) - batch_created
                    batch_created_users = result.get("created_users", [])

                    created += batch_created
                    failed += batch_failed
                    created_users.extend(batch_created_users)

                    tracker.add_log_item(f"Created {batch_created}/{len(batch)} users in batch")
                except Exception as e:
                    error_msg = f"Exception during bulk user creation: {str(e)}"
                    self.logger.error(error_msg, extra={"markup": True})
                    failed += len(batch)
                    tracker.add_log_item(f"Exception during creation: {', '.join(batch_users)}")
                    raise MigrationError(error_msg) from e

                tracker.increment(len(batch))

        # Update user mapping after creating new users
        self.extract_openproject_users()
        self.create_user_mapping()

        return {
            "created": created,
            "failed": failed,
            "total": len(missing_users),
            "created_count": created,  # Add for test compatibility
            "created_users": created_users,
        }

    def analyze_user_mapping(self) -> dict:
        """
        Analyze the user mapping for statistics and potential issues.

        Returns:
            Dictionary with analysis results

        Raises:
            MigrationError: If user mapping is missing
        """
        if not self.user_mapping:
            self.create_user_mapping()

        total_users = len(self.user_mapping)
        matched_by_username = len([u for u in self.user_mapping.values() if u["matched_by"] == "username"])
        matched_by_email = len([u for u in self.user_mapping.values() if u["matched_by"] == "email"])
        not_matched = len([u for u in self.user_mapping.values() if u["matched_by"] == "none"])

        analysis = {
            "total_users": total_users,
            "matched_by_username": matched_by_username,
            "matched_by_email": matched_by_email,
            "not_matched": not_matched,
            "username_match_percentage": ((matched_by_username / total_users) * 100 if total_users > 0 else 0),
            "email_match_percentage": ((matched_by_email / total_users) * 100 if total_users > 0 else 0),
            "total_match_percentage": (
                ((matched_by_username + matched_by_email) / total_users) * 100 if total_users > 0 else 0
            ),
            "not_matched_percentage": ((not_matched / total_users) * 100 if total_users > 0 else 0),
        }

        # Display the analysis
        self.logger.info("User mapping analysis:", extra={"markup": True})
        self.logger.info(f"Total users: {total_users}", extra={"markup": True})
        self.logger.info(
            f"Matched by username: {matched_by_username} ({analysis['username_match_percentage']:.2f}%)",
            extra={"markup": True},
        )
        self.logger.info(
            f"Matched by email: {matched_by_email} ({analysis['email_match_percentage']:.2f}%)",
            extra={"markup": True},
        )
        self.logger.info(
            f"Total matched: {matched_by_username + matched_by_email} ({analysis['total_match_percentage']:.2f}%)",
            extra={"markup": True},
        )
        self.logger.info(
            f"Not matched: {not_matched} ({analysis['not_matched_percentage']:.2f}%)",
            extra={"markup": True},
        )

        return analysis

    def run(self) -> ComponentResult:
        """
        Run the user migration.

        Returns:
            ComponentResult with migration results
        """
        self.logger.info("Starting user migration...", extra={"markup": True})

        try:
            # Extract users from both systems
            self.extract_jira_users()
            self.extract_openproject_users()

            # Create mapping
            self.create_user_mapping()

            # Analyze the mapping
            analysis = self.analyze_user_mapping()

            # Create missing users if configured
            create_missing = config.get_value("migration", "create_missing_users", False)
            creation_results = {}
            if create_missing:
                creation_results = self.create_missing_users()
                self.logger.info(
                    f"Created {creation_results['created']} users, {creation_results['failed']} failed",
                    extra={"markup": True},
                )
            else:
                self.logger.info(
                    "Skipping creation of missing users (disabled in config)",
                    extra={"markup": True},
                )

            # Update mappings with new data
            config.mappings.set_mapping("users", self.user_mapping)

            return ComponentResult(
                success=True,
                data={
                    "jira_users": len(self.jira_users),
                    "op_users": len(self.op_users),
                    "mapped_users": len(self.user_mapping),
                    "analysis": analysis,
                    "creation_results": creation_results,
                },
                success_count=analysis["matched_by_username"] + analysis["matched_by_email"],
                failed_count=analysis["not_matched"],
                total_count=analysis["total_users"],
            )
        except Exception as e:
            self.logger.error(f"Error in user migration: {str(e)}", extra={"markup": True})
            return ComponentResult(
                success=False,
                errors=[f"Error in user migration: {str(e)}"],
                success_count=0,
                failed_count=0,
                total_count=0,
            )
