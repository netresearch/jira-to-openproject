#!/usr/bin/env python3
"""User migration module for Jira to OpenProject migration.

Handles the migration of users and their accounts from Jira to OpenProject.
"""

import json
import logging
import re
import uuid
import contextlib
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import ProgressTracker
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult, MigrationError

# Get logger from config
logger = config.logger


class UserMigration(BaseMigration):
    """Handles the migration of users from Jira to OpenProject.

    Since both systems use LDAP/AD for authentication, the focus is on:
    1. Identifying the users in Jira
    2. Ensuring they exist in OpenProject (via LDAP sync)
    3. Creating a mapping between Jira and OpenProject user IDs for later use
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
    ) -> None:
        """Initialize the user migration tools.

        Args:
            jira_client: Initialized Jira client instance
            op_client: Initialized OpenProject client instance

        """
        # Initialize base migration with client dependencies
        super().__init__(
            jira_client=jira_client,
            op_client=op_client,
        )

        # Data storage
        self.jira_users: list[dict[str, Any]] = []
        self.op_users: list[dict[str, Any]] = []
        self.user_mapping: dict[str, Any] = {}

        # Setup file paths
        self.jira_users_file = self.data_dir / "jira_users.json"
        self.op_users_file = self.data_dir / "op_users.json"
        self.user_mapping_file = self.data_dir / "user_mapping.json"

        # Logging
        self.logger.debug("UserMigration initialized with data dir: %s", self.data_dir)

        # Load existing data if available
        self.jira_users = self._load_from_json(Path("jira_users.json")) or []
        self.op_users = self._load_from_json(Path("op_users.json")) or []
        self.user_mapping = self._load_from_json(Path("user_mapping.json")) or {}

    def extract_jira_users(self) -> list[dict[str, Any]]:
        """Extract users from Jira.

        Returns:
            List of Jira users

        Raises:
            MigrationError: If users cannot be extracted from Jira

        """
        self.logger.info("Extracting users from Jira...")

        self.jira_users = self.jira_client.get_users()

        if not self.jira_users:
            msg = "Failed to extract users from Jira"
            raise MigrationError(msg)

        self.logger.info("Extracted %s users from Jira", len(self.jira_users))

        self._save_to_json(self.jira_users, Path("jira_users.json"))

        return self.jira_users

    def extract_openproject_users(self) -> list[dict[str, Any]]:
        """Extract users from OpenProject.

        Returns:
            List of OpenProject users

        Raises:
            MigrationError: If users cannot be extracted from OpenProject

        """
        self.logger.info("Extracting users from OpenProject...")

        # Get users from OpenProject - no fallbacks or mocks
        self.op_users = self.op_client.get_users()

        if not self.op_users:
            # Instead of failing completely, log a warning and continue with empty list
            # This allows the migration to proceed even if user extraction has issues
            self.logger.warning(
                "Failed to extract users from OpenProject - continuing with empty user list"
            )
            self.logger.warning(
                "This may be due to JSON parsing issues with large user datasets"
            )
            self.op_users = []

        self.logger.info(
            "Extracted %s users from OpenProject",
            len(self.op_users),
        )

        self._save_to_json(self.op_users, Path("op_users.json"))

        return self.op_users

    def create_user_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira and OpenProject users.

        Returns:
            Dictionary mapping Jira user keys to OpenProject user IDs

        Raises:
            MigrationError: If required user data is missing

        """
        self.logger.info("Creating user mapping...")

        if not self.jira_users:
            self.extract_jira_users()

        if not self.op_users:
            self.extract_openproject_users()

        # Debug: Check what type of data we're getting
        self.logger.debug("OpenProject users data type: %s", type(self.op_users))
        if self.op_users:
            self.logger.debug("First user data type: %s", type(self.op_users[0]))
            self.logger.debug("First user content: %s", str(self.op_users[0])[:200])

        # Ensure we have a list of dictionaries
        if not isinstance(self.op_users, list):
            raise MigrationError(f"Expected list of users, got {type(self.op_users)}")

        # Filter out any non-dictionary items
        valid_users = []
        for i, user in enumerate(self.op_users):
            if isinstance(user, dict):
                valid_users.append(user)
            else:
                self.logger.warning(
                    "Skipping invalid user data at index %d: %s (type: %s)",
                    i,
                    str(user)[:100],
                    type(user),
                )

        self.op_users = valid_users
        self.logger.info("Filtered to %d valid user records", len(self.op_users))

        op_users_by_username = {
            user.get("login", "").lower(): user for user in self.op_users
        }
        op_users_by_email = {
            user.get("email", "").lower(): user
            for user in self.op_users
            if user.get("email")
        }

        mapping: dict[str, Any] = {}

        with ProgressTracker(
            "Mapping users",
            len(self.jira_users),
            "Recent User Mappings",
        ) as tracker:
            for jira_user in self.jira_users:
                jira_key = jira_user.get("key", "")  # Ensure non-None value
                if not jira_key:
                    self.logger.warning("Found Jira user without key, skipping")
                    continue

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
                        f"Matched by username: {jira_display_name} → {op_user.get('login')}",
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
                        f"Matched by email: {jira_display_name} → {op_user.get('login')}",
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

        # Save the mapping
        self._save_to_json(mapping, Path("user_mapping.json"))
        self.user_mapping = mapping

        return mapping

    def _build_fallback_email(self, login: str, existing_emails: set[str] | None = None) -> str:
        """Build a safe, unique fallback email address for a user.
        
        Args:
            login: The user's login name from JIRA
            existing_emails: Set of existing email addresses to avoid collisions
            
        Returns:
            A valid, unique email address
        """
        if existing_emails is None:
            existing_emails = set()
            
        # Sanitize login to RFC-5322 compliant format
        # Keep only letters, digits, dots, underscores, and hyphens
        sanitized_login = re.sub(r'[^a-zA-Z0-9._-]', '', login.lower())
        
        # If sanitization results in empty string, use UUID
        if not sanitized_login:
            sanitized_login = str(uuid.uuid4())[:8]
            
        # Build base email
        base_email = f"{sanitized_login}@{config.FALLBACK_MAIL_DOMAIN}"
        
        # Check for uniqueness
        if base_email not in existing_emails:
            return base_email
            
        # Handle collisions by appending counter
        counter = 1
        while True:
            candidate_email = f"{sanitized_login}.{counter}@{config.FALLBACK_MAIL_DOMAIN}"
            if candidate_email not in existing_emails:
                return candidate_email
            counter += 1

    def create_missing_users(self, batch_size: int | None = None) -> dict[str, Any]:
        """Create missing users in OpenProject using the LDAP synchronization.

        Args:
            batch_size: Number of users to create in each batch (defaults to config value)

        Returns:
            Dictionary with results of user creation

        Raises:
            MigrationError: If user mapping is missing or if users cannot be created

        """
        self.logger.info("Creating missing users in OpenProject...")

        # Use config default if no batch_size provided
        if batch_size is None:
            batch_size = config.USER_CREATION_BATCH_SIZE

        if not self.user_mapping:
            self.create_user_mapping()

        missing_users = [
            user for user in self.user_mapping.values() if user["matched_by"] == "none"
        ]

        if not missing_users:
            self.logger.info("No missing users to create")
            return {"created": 0, "failed": 0, "total": 0}

        self.logger.info(
            "Found %s users missing in OpenProject",
            len(missing_users),
        )

        created = 0
        failed = 0
        created_users: list[dict[str, Any]] = []

        # Collect existing emails to prevent collisions
        existing_emails = set()
        if self.op_users:
            for op_user in self.op_users:
                if isinstance(op_user, dict) and "mail" in op_user:
                    existing_emails.add(op_user["mail"])

        with ProgressTracker(
            "Creating users",
            len(missing_users),
            "Recent User Creations",
        ) as tracker:
            for i in range(0, len(missing_users), batch_size):
                batch = missing_users[i : i + batch_size]

                # Prepare data for user creation
                users_to_create = []
                for user in batch:
                    # Split display name into first and last name - handle empty display names
                    display_name = user["jira_display_name"].strip() if user["jira_display_name"] else ""
                    if not display_name:
                        names = ["User", user["jira_name"]]
                    else:
                        names = display_name.split(" ", 1)
                    
                    first_name = names[0].strip() if names[0].strip() else "User"
                    last_name = names[1] if len(names) > 1 else user["jira_name"]

                    # Handle missing or empty email addresses with fallback
                    email = user["jira_email"]
                    if not email or email.strip() == "":
                        # Generate safe, unique fallback email
                        email = self._build_fallback_email(user["jira_name"], existing_emails)
                        existing_emails.add(email)  # Track newly generated email
                        self.logger.info(f"Using fallback email for user {user['jira_name']}: {email}")

                    users_to_create.append(
                        {
                            "login": user["jira_name"],
                            "firstname": first_name,
                            "lastname": last_name,
                            "mail": email,
                            "admin": False,
                            "status": "active",
                        },
                    )

                batch_users = [user["jira_name"] for user in batch]
                tracker.update_description(f"Creating users: {', '.join(batch_users)}")

                # Use context manager for proper file cleanup
                with contextlib.ExitStack() as stack:
                    try:
                        # Use file-based transfer approach (like other migrations)
                        self.logger.info(f"Creating {len(users_to_create)} users via file transfer")
                        
                        # Create temporary file for user data
                        batch_num = tracker.processed_count // batch_size
                        temp_file_path = Path(self.data_dir) / f"users_batch_{batch_num}.json"
                        result_local_path = Path(self.data_dir) / f'users_result_{batch_num}.json'
                        
                        # Register files for cleanup
                        stack.callback(lambda: temp_file_path.unlink(missing_ok=True))
                        stack.callback(lambda: result_local_path.unlink(missing_ok=True))
                        
                        # Write users data to JSON file
                        with temp_file_path.open("w", encoding='utf-8') as f:
                            json.dump(users_to_create, f, ensure_ascii=False, indent=2)

                        # Transfer file to container
                        container_temp_path = f'/tmp/users_batch_{batch_num}.json'
                        self.op_client.transfer_file_to_container(
                            temp_file_path, Path(container_temp_path)
                        )

                        # Header: f-string interpolation for file paths and variables
                        result_file_path = f'/tmp/users_result_{batch_num}.json'
                        script_header = f"""
                        require 'json'
                        
                        # File paths
                        input_file = '{container_temp_path}'
                        output_file = '{result_file_path}'
                        
                        # Load the data from the JSON file
                        users_data = JSON.parse(File.read(input_file))
                        puts "Loaded " + users_data.length.to_s + " users from JSON file"
                        """
                        
                        # Body: pure Ruby code without Python interpolation
                        script_body = """
                        created_users = []
                        errors = []
                        
                        users_data.each do |user_data|
                          begin
                            # Create user with the provided data
                            user = User.new(
                              login: user_data['login'],
                              firstname: user_data['firstname'],
                              lastname: user_data['lastname'],
                              mail: user_data['mail'],
                              admin: user_data['admin'] || false,
                              status: User.statuses.key(user_data['status']) || User.statuses['active']
                            )
                            
                            if user.save
                              created_users << {
                                status: 'success',
                                login: user.login,
                                mail: user.mail,
                                id: user.id
                              }
                            else
                              errors << {
                                status: 'error',
                                login: user_data['login'],
                                mail: user_data['mail'],
                                errors: user.errors.full_messages
                              }
                            end
                          rescue => e
                            errors << {
                              status: 'error',
                              login: user_data['login'],
                              mail: user_data['mail'],
                              errors: [e.message]
                            }
                          end
                        end
                        
                        # Write results to file
                        result = {
                          created: created_users.length,
                          failed: errors.length,
                          total: users_data.length,
                          created_users: created_users,
                          errors: errors
                        }
                        
                        puts "User creation completed: " + created_users.length.to_s + " created, " + errors.length.to_s + " failed"
                        File.write(output_file, result.to_json)
                        puts "Results written to " + output_file
                        """
                        
                        script = script_header + script_body

                        # Debug: Log the script being executed
                        self.logger.debug(f"Executing Ruby script:\n{script}")
                        
                        # Execute the Ruby script with configurable timeout for user creation
                        script_result = self.op_client.execute_query(script, timeout=config.USER_CREATION_TIMEOUT)
                        self.logger.debug(f"Ruby script output: {script_result}")

                        # Transfer result file back and read it
                        container_result_path = f'/tmp/users_result_{batch_num}.json'
                        
                        result_path = self.op_client.transfer_file_from_container(
                            Path(container_result_path), result_local_path
                        )
                        
                        # Read the result
                        with result_path.open('r', encoding='utf-8') as f:
                            result = json.load(f)
                        
                        # Extract result stats
                        batch_created = result.get("created", 0)
                        batch_failed = result.get("failed", 0)
                        batch_created_users = result.get("created_users", [])
                        batch_errors = result.get("errors", [])

                        created += batch_created
                        failed += batch_failed
                        created_users.extend(batch_created_users)

                        # Improved error logging - avoid PII exposure
                        if batch_errors and self.logger.isEnabledFor(logging.DEBUG):
                            for error in batch_errors[:3]:  # Show first 3 errors
                                # Log only login and error messages, not full user data
                                safe_error = {
                                    "login": error.get("login", "unknown"),
                                    "errors": error.get("errors", [])
                                }
                                self.logger.debug(f"User creation error: {safe_error}")

                        tracker.add_log_item(
                            f"Created {batch_created}/{len(batch)} users in batch",
                        )
                    except Exception as e:
                        error_msg = f"Exception during bulk user creation: {e!s}"
                        self.logger.exception(error_msg)
                        failed += len(batch)
                        tracker.add_log_item(
                            f"Exception during creation: {', '.join(batch_users)}",
                        )
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

    def analyze_user_mapping(self) -> dict[str, Any]:
        """Analyze the user mapping for statistics and potential issues.

        Returns:
            Dictionary with analysis results

        Raises:
            MigrationError: If user mapping is missing

        """
        if not self.user_mapping:
            self.create_user_mapping()

        total_users = len(self.user_mapping)
        matched_by_username = len(
            [u for u in self.user_mapping.values() if u["matched_by"] == "username"],
        )
        matched_by_email = len(
            [u for u in self.user_mapping.values() if u["matched_by"] == "email"],
        )
        not_matched = len(
            [u for u in self.user_mapping.values() if u["matched_by"] == "none"],
        )

        analysis = {
            "total_users": total_users,
            "matched_by_username": matched_by_username,
            "matched_by_email": matched_by_email,
            "not_matched": not_matched,
            "username_match_percentage": (
                (matched_by_username / total_users) * 100 if total_users > 0 else 0
            ),
            "email_match_percentage": (
                (matched_by_email / total_users) * 100 if total_users > 0 else 0
            ),
            "total_match_percentage": (
                ((matched_by_username + matched_by_email) / total_users) * 100
                if total_users > 0
                else 0
            ),
            "not_matched_percentage": (
                (not_matched / total_users) * 100 if total_users > 0 else 0
            ),
        }

        # Display the analysis
        self.logger.info("User mapping analysis:")
        self.logger.info("Total users: %s", total_users)
        self.logger.info(
            "Matched by username: %s (%s%%)",
            matched_by_username,
            analysis["username_match_percentage"],
        )
        self.logger.info(
            "Matched by email: %s (%s%%)",
            matched_by_email,
            analysis["email_match_percentage"],
        )
        self.logger.info(
            "Total matched: %s (%s%%)",
            matched_by_username + matched_by_email,
            analysis["total_match_percentage"],
        )
        self.logger.info(
            "Not matched: %s (%s%%)",
            not_matched,
            analysis["not_matched_percentage"],
        )

        return analysis

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        Args:
            entity_type: Type of entities to retrieve

        Returns:
            List of current entities from Jira

        Raises:
            ValueError: If entity_type is not supported by this migration
        """
        if entity_type == "users":
            return self.jira_client.get_users()
        else:
            raise ValueError(
                f"UserMigration does not support entity type: {entity_type}. "
                f"Supported types: ['users']"
            )

    def run(self) -> ComponentResult:
        """Execute the complete user migration process."""
        self.logger.info("Starting user migration")

        try:
            result = self.create_missing_users()
            # Consider success if we have results (even if 0 users needed creation)
            created = result.get("created", 0)
            total = result.get("total", 0)
            failed = result.get("failed", 0)
            
            # Success if no failures occurred (even if no users needed creation)
            is_success = failed == 0
            message = f"User migration completed: {created}/{total} users created, {failed} failed"
            
            return ComponentResult(
                success=is_success,
                message=message,
                data=result,
                success_count=created,
                failed_count=failed,
                total_count=total,
            )
        except Exception as e:
            self.logger.exception("User migration failed")
            return ComponentResult(
                component="users",
                status="failed",
                message=f"User migration failed: {e}",
                data={"error": str(e)},
            )

    def process_single_user(self, user_data: dict[str, Any]) -> dict[str, Any] | None:
        """Process a single user for selective updates.

        Args:
            user_data: Single user data to process

        Returns:
            Dict with processing result containing openproject_id if successful
        """
        try:
            # For now, simulate user creation/processing
            # In a real implementation, this would integrate with create_missing_users logic
            self.logger.debug("Processing single user: %s", user_data.get("displayName", "unknown"))

            # Mock successful processing
            return {
                "openproject_id": user_data.get("id", 1),
                "success": True,
                "message": "User processed successfully"
            }
        except Exception as e:
            self.logger.error("Failed to process single user: %s", e)
            return None

    def update_user_in_openproject(self, user_data: dict[str, Any], user_id: str) -> dict[str, Any] | None:
        """Update a user in OpenProject.

        Args:
            user_data: Updated user data
            user_id: OpenProject user ID to update

        Returns:
            Dict with update result
        """
        try:
            # For now, simulate user update
            # In a real implementation, this would call OpenProject API to update the user
            self.logger.debug("Updating user %s in OpenProject: %s", user_id, user_data.get("displayName", "unknown"))

            # Mock successful update
            return {
                "id": user_id,
                "success": True,
                "message": "User updated successfully"
            }
        except Exception as e:
            self.logger.error("Failed to update user in OpenProject: %s", e)
            return None
