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

    def create_missing_users(self) -> Dict[str, Any]:
        """
        Create users in OpenProject that don't have a match.

        This method will create users in OpenProject for those Jira users
        that couldn't be matched to an existing OpenProject user.

        Returns:
            Updated user mapping with newly created users
        """
        if not self.user_mapping:
            self.create_user_mapping()

        # Count how many users need to be created
        unmatched_users = [
            user for user in self.user_mapping.values()
            if user["matched_by"] == "none"
        ]

        if not unmatched_users:
            logger.info("All users have a match in OpenProject, no need to create new users", extra={"markup": True})
            return self.user_mapping

        logger.info(f"Creating {len(unmatched_users)} missing users in OpenProject...", extra={"markup": True})

        created_count = 0
        skipped_count = 0
        failed_count = 0
        found_existing_count = 0

        with ProgressTracker("Creating missing users", len(unmatched_users), "Recently Created Users") as tracker:
            for user_data in unmatched_users:
                jira_key = user_data["jira_key"]
                jira_name = user_data["jira_name"]
                jira_email = user_data["jira_email"]
                jira_display_name = user_data["jira_display_name"]

                tracker.update_description(f"Creating user: {jira_display_name}")

                # Skip if username or email is missing
                if not jira_name or not jira_email:
                    logger.warning(f"Skipping user {jira_display_name} - missing username or email", extra={"markup": True})
                    tracker.add_log_item(f"Skipped (missing data): {jira_display_name}")
                    skipped_count += 1
                    tracker.increment()
                    continue

                # Extract first and last name if possible
                name_parts = jira_display_name.split()

                # Handle names with bracketed suffixes like "John Smith [Company]"
                # by replacing the bracketed part with parentheses
                cleaned_display_name = jira_display_name
                original_display_name = cleaned_display_name  # Store original for comparison
                has_special_handling = False

                # Replace square brackets with parentheses instead of removing them
                if '[' in cleaned_display_name and ']' in cleaned_display_name:
                    cleaned_display_name = cleaned_display_name.replace('[', '(').replace(']', ')')
                    has_special_handling = True

                # Handle names with colons
                if ':' in cleaned_display_name:
                    cleaned_display_name = cleaned_display_name.replace(':', ' -')
                    has_special_handling = True

                # Handle other special characters in names
                # Only replace characters that are likely to cause problems, preserve apostrophes
                old_display_name = cleaned_display_name
                # Keep alphanumeric, spaces, apostrophes, hyphens, and parentheses
                cleaned_display_name = re.sub(r'[^\w\s\'\-\(\)]', ' ', cleaned_display_name)
                if old_display_name != cleaned_display_name:
                    has_special_handling = True

                # Now split into name parts
                cleaned_name_parts = cleaned_display_name.split()
                first_name = cleaned_name_parts[0] if cleaned_name_parts else "(none)"

                # If there's only one name part, use "(none)" for last name
                last_name = " ".join(cleaned_name_parts[1:]) if len(cleaned_name_parts) > 1 else "(none)"

                # Log if special handling was applied
                if has_special_handling or first_name == "(none)" or last_name == "(none)":
                    logger.info(f"Special name handling for {jira_display_name}: "
                               f"original='{original_display_name}', "
                               f"cleaned='{cleaned_display_name}', "
                               f"first_name='{first_name}', last_name='{last_name}'")
                else:
                    logger.debug(f"Name parsing for {jira_display_name}: first_name='{first_name}', last_name='{last_name}'")

                # Create user directly without checking for duplicates (already handled in mapping)
                try:
                    # Try to create the user using the API
                    data = {
                        "login": jira_name,
                        "email": jira_email,
                        "status": "active"
                    }

                    if first_name:
                        data["firstName"] = first_name

                    if last_name:
                        data["lastName"] = last_name

                    # Generate a random password
                    import random
                    import string
                    password = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(12))
                    data["password"] = password

                    # Create the user
                    try:
                        created_user = self.op_client._request("POST", "/users", data=data)
                    except requests.exceptions.HTTPError as e:
                        # Initialize created_user as None when handling an error
                        created_user = None

                        # Log details of the request that failed
                        logger.debug(f"Failed user creation request data for {jira_display_name}: {json.dumps(data)}")

                        # Extract detailed error message from OpenProject
                        if e.response.status_code == 422:
                            try:
                                error_details = e.response.json()
                                error_message = error_details.get("message", str(e))

                                # Log specific validation errors
                                has_username_taken = False
                                has_email_taken = False

                                # Log more detailed information for debugging
                                if "_embedded" in error_details and "errors" in error_details["_embedded"]:
                                    for error in error_details["_embedded"]["errors"]:
                                        error_msg = error.get("message", "Unknown error")
                                        logger.warning(f"Validation error for {jira_display_name}: {error_msg}", extra={"markup": True})

                                        # Log more detailed information for debugging
                                        if "embedded" in error_details:
                                            logger.debug(f"Full error details for {jira_display_name}: {json.dumps(error_details)}")

                                        if "username" in error_msg.lower() and "already been taken" in error_msg.lower():
                                            has_username_taken = True
                                        if "email" in error_msg.lower() and "already been taken" in error_msg.lower():
                                            has_email_taken = True
                                else:
                                    # If there's no embedded errors structure, log the whole error message
                                    logger.warning(f"Error creating user {jira_display_name}: {error_message}", extra={"markup": True})

                                # If admin is required, retry with admin=true
                                if "admin" in error_message.lower() and "can't be blank" in error_message.lower():
                                    logger.info(f"Retrying user creation with admin=true for {jira_display_name}", extra={"markup": True})
                                    data["admin"] = True
                                    created_user = self.op_client._request("POST", "/users", data=data)
                                # If the username or email is already taken, try to find the existing user
                                elif has_username_taken or has_email_taken:
                                    logger.info(f"User appears to already exist in OpenProject, searching for {jira_display_name}", extra={"markup": True})

                                    # Force refresh user list to make sure we have all users
                                    # Use this method directly to ensure we get all users
                                    all_users = self.op_client.get_users(force_refresh=True)

                                    logger.info(f"Retrieved {len(all_users)} users from OpenProject for matching", extra={"markup": True})

                                    # Look for user by login or email (case-insensitive)
                                    existing_user = None
                                    matched_by = None
                                    jira_name_lower = jira_name.lower()
                                    jira_email_lower = jira_email.lower() if jira_email else None

                                    for user in all_users:
                                        # Try matching by username first
                                        op_login = user.get("login", "").lower()
                                        if op_login == jira_name_lower:
                                            logger.info(f"Found existing user with login: {jira_name}", extra={"markup": True})
                                            existing_user = user
                                            matched_by = "username"
                                            break

                                        # Then try matching by email
                                        if jira_email_lower:
                                            op_email = user.get("email", "").lower()
                                            if op_email == jira_email_lower:
                                                logger.info(f"Found existing user with email: {jira_email}", extra={"markup": True})
                                                existing_user = user
                                                matched_by = "email"
                                                break

                                    if existing_user:
                                        # Update the mapping with the found user
                                        self.user_mapping[jira_key].update({
                                            "openproject_id": existing_user.get("id"),
                                            "openproject_login": existing_user.get("login"),
                                            "openproject_email": existing_user.get("email"),
                                            "matched_by": "found_after_error",
                                        })
                                        found_existing_count += 1
                                        tracker.add_log_item(f"Found existing: {jira_display_name} → {existing_user.get('login')} (by {matched_by})")
                                        # Skip to next user
                                        tracker.increment()
                                        continue
                                    else:
                                        # If we still couldn't find the user, mark it as a special case
                                        logger.warning(f"User reported as duplicate but couldn't be found: {jira_display_name}", extra={"markup": True})
                                        # Skip rather than fail - mark as a special type of skipped
                                        self.user_mapping[jira_key].update({
                                            "openproject_id": None,
                                            "openproject_login": None,
                                            "openproject_email": None,
                                            "matched_by": "duplicate_but_not_found",
                                        })
                                        skipped_count += 1
                                        tracker.add_log_item(f"Skipped (duplicate but not found): {jira_display_name}")
                                        # Skip to next user
                                        tracker.increment()
                                        continue
                            except json.JSONDecodeError:
                                # If we can't parse the response as JSON, just raise the original exception
                                raise e
                        else:
                            # For other errors, just re-raise
                            raise e

                    # If API fails, try using Rails (if available)
                    if not created_user and self.op_client.rails_client:
                        logger.info(f"API user creation failed for {jira_name}, trying Rails", extra={"markup": True})

                        # Prepare attributes for Rails
                        attributes = {
                            'login': jira_name,
                            'mail': jira_email,
                            'password': password
                        }

                        if first_name:
                            attributes['firstname'] = first_name

                        if last_name:
                            attributes['lastname'] = last_name

                        # Create the user via Rails
                        success, record_data, error = self.op_client.rails_client.create_record('User', attributes)
                        created_user = record_data if success and record_data else None

                        if error:
                            logger.warning(f"Rails user creation error: {error}", extra={"markup": True})

                    if created_user:
                        # Update the user mapping with the new OpenProject user
                        self.user_mapping[jira_key].update({
                            "openproject_id": created_user.get("id"),
                            "openproject_login": created_user.get("login"),
                            "openproject_email": created_user.get("email"),
                            "matched_by": "created",
                        })
                        created_count += 1
                        tracker.add_log_item(f"Created: {jira_display_name} → {created_user.get('login')}")
                    else:
                        failed_count += 1
                        tracker.add_log_item(f"Failed to create: {jira_display_name}")
                except Exception as e:
                    logger.error(f"Error creating user {jira_display_name}: {str(e)}", extra={"markup": True})
                    failed_count += 1
                    tracker.add_log_item(f"Failed to create: {jira_display_name} (error: {str(e)})")

                tracker.increment()

        # Refresh the OpenProject user cache once after all creations
        if created_count > 0:
            self.op_client._users_cache = []

        # Save the updated mapping
        self._save_to_json(self.user_mapping, "user_mapping.json")

        # Log results
        logger.info(f"User creation complete", extra={"markup": True})
        logger.info(f"Created: {created_count} users", extra={"markup": True})
        logger.info(f"Found existing: {found_existing_count} users", extra={"markup": True})
        logger.info(f"Skipped: {skipped_count} users", extra={"markup": True})
        logger.info(f"Failed: {failed_count} users", extra={"markup": True})

        # Log updated statistics
        total_users = len(self.user_mapping)
        matched_users = sum(
            1 for user in self.user_mapping.values() if user["matched_by"] != "none"
        )
        match_percentage = (
            (matched_users / total_users) * 100 if total_users > 0 else 0
        )

        logger.info(f"Updated user mapping: {matched_users}/{total_users} users matched ({match_percentage:.1f}%)", extra={"markup": True})

        return self.user_mapping

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
            "created_users": sum(
                1
                for user in self.user_mapping.values()
                if user["matched_by"] == "created"
            ),
            "found_after_error": sum(
                1
                for user in self.user_mapping.values()
                if user["matched_by"] == "found_after_error"
            ),
            "duplicate_but_not_found": sum(
                1
                for user in self.user_mapping.values()
                if user["matched_by"] == "duplicate_but_not_found"
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
        logger.info(f"- Created in OpenProject: {analysis['created_users']}", extra={"markup": True})
        logger.info(f"- Found after error: {analysis['found_after_error']}", extra={"markup": True})
        logger.info(f"- Duplicate but not found: {analysis['duplicate_but_not_found']}", extra={"markup": True})
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
        logger.info("Running in dry-run mode - will not create users in OpenProject", extra={"markup": True})

    migration = UserMigration(dry_run=dry_run)

    # Extract users from both systems
    migration.extract_jira_users()
    migration.extract_openproject_users()

    # Create and analyze the user mapping
    migration.create_user_mapping()

    # Create missing users unless in dry-run mode
    if not dry_run:
        migration.create_missing_users()

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
