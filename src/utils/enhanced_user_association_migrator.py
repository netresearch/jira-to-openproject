#!/usr/bin/env python3
"""Enhanced User Association Migration for comprehensive metadata preservation.

This module provides advanced user association migration capabilities that:
1. Maps all Jira user references to OpenProject users
2. Preserves creator/author information using Rails console when API limitations exist
3. Transfers assignee relationships with proper mapping
4. Migrates watchers and subscribers with all metadata
5. Handles edge cases like deleted users or missing references
"""

import json
import re
import requests
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.parse import quote

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.utils.validators import validate_jira_key


# Enum types for validation
FallbackStrategy = Literal["skip", "assign_admin", "create_placeholder"]


class UserAssociationMapping(TypedDict):
    """Represents a mapping between Jira and OpenProject user associations with staleness tracking."""
    
    jira_username: str
    jira_user_id: str | None
    jira_display_name: str | None
    jira_email: str | None
    openproject_user_id: int | None
    openproject_username: str | None
    openproject_email: str | None
    mapping_status: str  # 'mapped', 'deleted', 'unmapped', 'fallback'
    fallback_user_id: int | None
    metadata: dict[str, Any]
    lastRefreshed: str | None  # ISO datetime string for staleness detection


class AssociationResult(TypedDict):
    """Result of user association migration."""
    
    original_association: dict[str, Any]
    mapped_association: dict[str, Any] | None
    status: str  # 'success', 'fallback_used', 'failed'
    warnings: list[str]
    metadata: dict[str, Any]


class EnhancedUserAssociationMigrator:
    """Enhanced user association migrator with comprehensive edge case handling.
    
    This class provides advanced capabilities for migrating user associations
    from Jira to OpenProject with robust handling of:
    - Deleted or inactive users
    - Missing user mappings
    - Creator information preservation via Rails console
    - Watcher relationships with metadata
    - Assignee relationships with proper fallbacks
    - Staleness detection and automatic refresh
    """

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        user_mapping: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the enhanced user association migrator.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client 
            user_mapping: Pre-loaded user mapping (optional)
        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.logger = config.logger
        
        # Load user mapping
        self.user_mapping = user_mapping or self._load_user_mapping()
        
        # Enhanced user association mappings with metadata
        self.enhanced_user_mappings: dict[str, UserAssociationMapping] = {}
        self._load_enhanced_mappings()
        
        # Fallback users for different scenarios
        self.fallback_users = self._identify_fallback_users()
        
        # Cache for Rails console operations
        self._rails_operations_cache: list[dict[str, Any]] = []
        
        # Load and validate staleness configuration
        self._load_staleness_config()

    def _load_user_mapping(self) -> dict[str, Any]:
        """Load user mapping from file or config."""
        try:
            user_mapping_file = config.get_path("data") / "user_mapping.json"
            if user_mapping_file.exists():
                with user_mapping_file.open() as f:
                    return json.load(f)
            return {}
        except Exception as e:
            self.logger.warning("Failed to load user mapping: %s", e)
            return {}

    def _validate_jira_key(self, jira_key: str) -> None:
        """Validate JIRA key format using the centralized validator.
        
        Args:
            jira_key: The JIRA key to validate
            
        Raises:
            ValueError: If jira_key format is invalid or contains potentially dangerous characters
        """
        validate_jira_key(jira_key)

    def _load_enhanced_mappings(self) -> None:
        """Load enhanced user association mappings with metadata."""
        try:
            enhanced_mapping_file = config.get_path("data") / "enhanced_user_mappings.json"
            if enhanced_mapping_file.exists():
                with enhanced_mapping_file.open() as f:
                    data = json.load(f)
                    self.enhanced_user_mappings = {}
                    current_time = self._get_current_timestamp()
                    
                    for k, v in data.items():
                        # Handle backwards compatibility for existing cache files
                        if "lastRefreshed" not in v:
                            v["lastRefreshed"] = current_time
                            self.logger.debug(
                                "Added lastRefreshed timestamp to existing cache entry for %s", k
                            )
                        
                        self.enhanced_user_mappings[k] = UserAssociationMapping(**v)
                        
                    self.logger.info(
                        "Loaded %d enhanced user mappings with staleness tracking",
                        len(self.enhanced_user_mappings)
                    )
            else:
                # Create enhanced mappings from basic user mapping
                self._create_enhanced_mappings()
        except Exception as e:
            self.logger.warning("Failed to load enhanced user mappings: %s", e)
            self._create_enhanced_mappings()

    def _create_enhanced_mappings(self) -> None:
        """Create enhanced user mappings from basic user mapping."""
        self.logger.info("Creating enhanced user mappings from basic mapping")
        
        for jira_username, op_user_id in self.user_mapping.items():
            # Get additional user info from Jira if possible
            jira_user_info = self._get_jira_user_info(jira_username)
            op_user_info = self._get_openproject_user_info(op_user_id) if op_user_id else None
            
            mapping = UserAssociationMapping(
                jira_username=jira_username,
                jira_user_id=jira_user_info.get("accountId") if jira_user_info else None,
                jira_display_name=jira_user_info.get("displayName") if jira_user_info else None,
                jira_email=jira_user_info.get("emailAddress") if jira_user_info else None,
                openproject_user_id=op_user_id,
                openproject_username=op_user_info.get("login") if op_user_info else None,
                openproject_email=op_user_info.get("mail") if op_user_info else None,
                mapping_status="mapped" if op_user_id else "unmapped",
                fallback_user_id=None,
                metadata={
                    "created_at": self._get_current_timestamp(),
                    "jira_active": jira_user_info.get("active", True) if jira_user_info else None,
                    "openproject_active": op_user_info.get("status") == 1 if op_user_info else None,
                },
                lastRefreshed=self._get_current_timestamp()
            )
            
            self.enhanced_user_mappings[jira_username] = mapping

    def _get_current_timestamp(self) -> str:
        """Get current timestamp in ISO format.
        
        Returns:
            Current timestamp as ISO string
        """
        return datetime.now(tz=UTC).isoformat()

    def _get_jira_user_info(self, username: str) -> dict[str, Any] | None:
        """Fetch user information from Jira API with staleness check.
        
        Args:
            username: Jira username to fetch
            
        Returns:
            User information dict or None if not found
        """
        # Check staleness once and cache result to avoid double checking
        is_stale = self.is_mapping_stale(username)
        if is_stale:
            self.logger.debug("User mapping for %s is stale, will refresh if found", username)
        
        # Existing implementation continues here...
        try:
            # Get user from Jira with URL-encoded username to prevent injection
            safe_username = quote(username)
            response = self.jira_client.get(f"user/search?username={safe_username}")
            
            if response.status_code == 200:
                users = response.json()
                if users:
                    user_info = users[0]
                    
                    # Auto-refresh stale mapping with fresh data
                    if is_stale:
                        self.logger.info("Auto-refreshing stale mapping for %s", username)
                        self.refresh_user_mapping(username)
                    
                    return user_info
            
            return None
            
        except Exception as e:
            self.logger.error("Failed to fetch Jira user info for %s: %s", username, e)
            return None

    def _get_openproject_user_info(self, user_id: int) -> dict[str, Any] | None:
        """Get detailed user information from OpenProject."""
        try:
            return self.op_client.get_user(user_id)
        except Exception as e:
            self.logger.debug("Failed to get OpenProject user info for %s: %s", user_id, e)
            return None

    def _identify_fallback_users(self) -> dict[str, int]:
        """Identify fallback users for different scenarios."""
        fallback_users = {}
        
        try:
            # Get admin user as default fallback
            admin_users = self.op_client.get_users(filters={"admin": True})
            if admin_users:
                fallback_users["admin"] = admin_users[0]["id"]
            
            # Get system user if available
            system_users = self.op_client.get_users(filters={"login": "system"})
            if system_users:
                fallback_users["system"] = system_users[0]["id"]
            
            # Get migration user if created
            migration_users = self.op_client.get_users(filters={"login": "migration_user"})
            if migration_users:
                fallback_users["migration"] = migration_users[0]["id"]
            
        except Exception as e:
            self.logger.warning("Failed to identify fallback users: %s", e)
        
        return fallback_users

    def migrate_user_associations(
        self,
        jira_issue: dict[str, Any],
        work_package_data: dict[str, Any],
        preserve_creator_via_rails: bool = True,
    ) -> AssociationResult:
        """Migrate all user associations for a work package with enhanced handling.

        Args:
            jira_issue: Jira issue data with user associations
            work_package_data: Work package data to enhance with user associations
            preserve_creator_via_rails: Whether to use Rails console for creator preservation

        Returns:
            AssociationResult with migration results and metadata
        """
        result = AssociationResult(
            original_association={},
            mapped_association={},
            status="success",
            warnings=[],
            metadata={
                "migration_timestamp": self._get_current_timestamp(),
                "preserve_creator_via_rails": preserve_creator_via_rails,
            }
        )

        # Extract user associations from Jira issue
        associations = self._extract_user_associations(jira_issue)
        result["original_association"] = associations

        # Migrate assignee
        assignee_result = self._migrate_assignee(associations.get("assignee"), work_package_data)
        if assignee_result["warnings"]:
            result["warnings"].extend(assignee_result["warnings"])

        # Migrate author/reporter with enhanced preservation
        author_result = self._migrate_author(
            associations.get("reporter"),
            associations.get("creator"),
            work_package_data,
            preserve_creator_via_rails
        )
        if author_result["warnings"]:
            result["warnings"].extend(author_result["warnings"])

        # Migrate watchers with enhanced validation
        watcher_result = self._migrate_watchers(associations.get("watchers", []), work_package_data)
        if watcher_result["warnings"]:
            result["warnings"].extend(watcher_result["warnings"])

        # Store mapped associations
        result["mapped_association"] = {
            "assignee": work_package_data.get("assigned_to_id"),
            "author": work_package_data.get("author_id"),
            "watchers": work_package_data.get("watcher_ids", []),
        }

        # Update status based on warnings
        if result["warnings"]:
            result["status"] = "fallback_used" if any("fallback" in w for w in result["warnings"]) else "success"

        return result

    def _extract_user_associations(self, jira_issue: dict[str, Any]) -> dict[str, Any]:
        """Extract all user associations from Jira issue."""
        associations = {}

        # Extract assignee
        if hasattr(jira_issue.fields, "assignee") and jira_issue.fields.assignee:
            associations["assignee"] = {
                "username": getattr(jira_issue.fields.assignee, "name", None),
                "account_id": getattr(jira_issue.fields.assignee, "accountId", None),
                "display_name": getattr(jira_issue.fields.assignee, "displayName", None),
                "email": getattr(jira_issue.fields.assignee, "emailAddress", None),
                "active": getattr(jira_issue.fields.assignee, "active", True),
            }

        # Extract reporter
        if hasattr(jira_issue.fields, "reporter") and jira_issue.fields.reporter:
            associations["reporter"] = {
                "username": getattr(jira_issue.fields.reporter, "name", None),
                "account_id": getattr(jira_issue.fields.reporter, "accountId", None),
                "display_name": getattr(jira_issue.fields.reporter, "displayName", None),
                "email": getattr(jira_issue.fields.reporter, "emailAddress", None),
                "active": getattr(jira_issue.fields.reporter, "active", True),
            }

        # Extract creator
        if hasattr(jira_issue.fields, "creator") and jira_issue.fields.creator:
            associations["creator"] = {
                "username": getattr(jira_issue.fields.creator, "name", None),
                "account_id": getattr(jira_issue.fields.creator, "accountId", None),
                "display_name": getattr(jira_issue.fields.creator, "displayName", None),
                "email": getattr(jira_issue.fields.creator, "emailAddress", None),
                "active": getattr(jira_issue.fields.creator, "active", True),
            }

        # Extract watchers
        watchers = []
        if hasattr(jira_issue.fields, "watches") and jira_issue.fields.watches:
            watcher_count = getattr(jira_issue.fields.watches, "watchCount", 0)
            if watcher_count > 0:
                try:
                    watchers_data = self.jira_client.get_issue_watchers(jira_issue.key)
                    if watchers_data:
                        for watcher in watchers_data:
                            watchers.append({
                                "username": watcher.get("name"),
                                "account_id": watcher.get("accountId"),
                                "display_name": watcher.get("displayName"),
                                "email": watcher.get("emailAddress"),
                                "active": watcher.get("active", True),
                            })
                except Exception as e:
                    self.logger.exception("Failed to fetch watchers for %s: %s", jira_issue.key, e)

        associations["watchers"] = watchers
        return associations

    def _migrate_assignee(
        self, assignee_data: dict[str, Any] | None, work_package_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Migrate assignee with enhanced handling."""
        result = {"warnings": []}

        if not assignee_data or not assignee_data.get("username"):
            return result

        username = assignee_data["username"]
        mapping = self.enhanced_user_mappings.get(username)

        if mapping and mapping["openproject_user_id"]:
            # Verify user is still active
            if mapping["mapping_status"] == "mapped" and mapping["metadata"].get("openproject_active", True):
                work_package_data["assigned_to_id"] = mapping["openproject_user_id"]
            else:
                # Use fallback for inactive user
                fallback_id = self._get_fallback_user("assignee")
                if fallback_id:
                    work_package_data["assigned_to_id"] = fallback_id
                    result["warnings"].append(f"Assignee {username} inactive, using fallback user {fallback_id}")
                else:
                    result["warnings"].append(f"Assignee {username} inactive and no fallback available")
        else:
            # Handle unmapped user
            fallback_id = self._get_fallback_user("assignee")
            if fallback_id:
                work_package_data["assigned_to_id"] = fallback_id
                result["warnings"].append(f"Assignee {username} unmapped, using fallback user {fallback_id}")
            else:
                result["warnings"].append(f"Assignee {username} unmapped and no fallback available")

        return result

    def _migrate_author(
        self,
        reporter_data: dict[str, Any] | None,
        creator_data: dict[str, Any] | None,
        work_package_data: dict[str, Any],
        preserve_via_rails: bool,
    ) -> dict[str, Any]:
        """Migrate author with enhanced preservation using Rails console when needed."""
        result = {"warnings": []}

        # Prefer reporter over creator
        author_data = reporter_data or creator_data
        if not author_data or not author_data.get("username"):
            # Use fallback user
            fallback_id = self._get_fallback_user("author")
            if fallback_id:
                work_package_data["author_id"] = fallback_id
                result["warnings"].append("No author data available, using fallback user")
            return result

        username = author_data["username"]
        mapping = self.enhanced_user_mappings.get(username)

        if mapping and mapping["openproject_user_id"]:
            # Check if we need Rails console for immutable field preservation
            if preserve_via_rails and mapping["mapping_status"] == "mapped":
                # Queue Rails operation for later execution
                self._queue_rails_author_operation(
                    work_package_data.get("jira_key", ""),
                    mapping["openproject_user_id"],
                    author_data
                )
                # Set temporary author for creation
                work_package_data["author_id"] = mapping["openproject_user_id"]
            else:
                work_package_data["author_id"] = mapping["openproject_user_id"]
        else:
            # Handle unmapped author
            fallback_id = self._get_fallback_user("author")
            if fallback_id:
                work_package_data["author_id"] = fallback_id
                result["warnings"].append(f"Author {username} unmapped, using fallback user {fallback_id}")
            else:
                result["warnings"].append(f"Author {username} unmapped and no fallback available")

        return result

    def _migrate_watchers(
        self, watchers_data: list[dict[str, Any]], work_package_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Migrate watchers with enhanced validation and handling."""
        result = {"warnings": []}
        valid_watcher_ids = []

        for watcher in watchers_data:
            username = watcher.get("username")
            if not username:
                continue

            mapping = self.enhanced_user_mappings.get(username)
            if mapping and mapping["openproject_user_id"]:
                # Verify user exists and is active
                if mapping["mapping_status"] == "mapped" and mapping["metadata"].get("openproject_active", True):
                    valid_watcher_ids.append(mapping["openproject_user_id"])
                else:
                    result["warnings"].append(f"Watcher {username} inactive, skipping")
            else:
                result["warnings"].append(f"Watcher {username} unmapped, skipping")

        if valid_watcher_ids:
            work_package_data["watcher_ids"] = valid_watcher_ids

        return result

    def _get_fallback_user(self, role: str) -> int | None:
        """Get appropriate fallback user for a specific role."""
        # Priority order for fallback users
        fallback_priority = ["migration", "admin", "system"]
        
        for fallback_type in fallback_priority:
            if fallback_type in self.fallback_users:
                return self.fallback_users[fallback_type]
        
        return None

    def is_mapping_stale(self, username: str) -> bool:
        """Check if a user mapping is stale and needs refresh.
        
        Args:
            username: Jira username to check
            
        Returns:
            True if mapping is stale, False otherwise
        """
        mapping = self.enhanced_user_mappings.get(username)
        if not mapping:
            return True  # Non-existent mappings are considered stale
        
        last_refreshed = mapping.get("lastRefreshed")
        if not last_refreshed:
            return True  # Mappings without refresh timestamp are stale
        
        refresh_interval = self.staleness_config["refresh_interval"]
        
        try:
            last_refresh_time = datetime.fromisoformat(last_refreshed.replace('Z', '+00:00'))
            current_time = datetime.now(tz=UTC)
            age_seconds = (current_time - last_refresh_time).total_seconds()
            
            # Fixed: Use >= for consistent threshold behavior
            return age_seconds >= refresh_interval
            
        except ValueError as e:
            self.logger.warning("Invalid lastRefreshed timestamp for %s: %s", username, e)
            return True  # Invalid timestamps are considered stale

    def refresh_user_mapping(self, username: str) -> bool:
        """Refresh a stale user mapping by re-fetching from Jira.
        
        Args:
            username: Jira username to refresh
            
        Returns:
            True if refresh succeeded, False otherwise
        """
        try:
            self.logger.debug("Refreshing user mapping for %s", username)
            
            # Get fresh user info from Jira
            jira_user_info = self._get_jira_user_info(username)
            
            if not jira_user_info:
                self.logger.warning("Could not fetch fresh user info for %s", username)
                return False
            
            # Get existing mapping or create new one
            existing_mapping = self.enhanced_user_mappings.get(username)
            current_time = self._get_current_timestamp()
            
            if existing_mapping:
                # Update existing mapping with fresh data
                existing_mapping["jira_user_id"] = jira_user_info.get("accountId")
                existing_mapping["jira_display_name"] = jira_user_info.get("displayName")
                existing_mapping["jira_email"] = jira_user_info.get("emailAddress")
                existing_mapping["lastRefreshed"] = current_time
                existing_mapping["metadata"]["jira_active"] = jira_user_info.get("active", True)
                existing_mapping["metadata"]["refreshed_at"] = current_time
            else:
                # Create new mapping
                op_user_id = self.user_mapping.get(username)
                op_user_info = self._get_openproject_user_info(op_user_id) if op_user_id else None
                
                mapping = UserAssociationMapping(
                    jira_username=username,
                    jira_user_id=jira_user_info.get("accountId"),
                    jira_display_name=jira_user_info.get("displayName"),
                    jira_email=jira_user_info.get("emailAddress"),
                    openproject_user_id=op_user_id,
                    openproject_username=op_user_info.get("login") if op_user_info else None,
                    openproject_email=op_user_info.get("mail") if op_user_info else None,
                    mapping_status="mapped" if op_user_id else "unmapped",
                    fallback_user_id=None,
                    metadata={
                        "created_at": current_time,
                        "jira_active": jira_user_info.get("active", True),
                        "openproject_active": op_user_info.get("status") == 1 if op_user_info else None,
                        "refreshed_at": current_time,
                    },
                    lastRefreshed=current_time
                )
                
                self.enhanced_user_mappings[username] = mapping
            
            self.logger.debug("Successfully refreshed user mapping for %s", username)
            return True
            
        except (requests.RequestException, ValueError, KeyError) as e:
            self.logger.error("Failed to refresh user mapping for %s: %s", username, e)
            return False
        except Exception as e:
            self.logger.error("Unexpected error refreshing user mapping for %s: %s", username, e)
            return False

    def _queue_rails_author_operation(
        self, jira_key: str, author_id: int, author_data: dict[str, Any]
    ) -> None:
        """Queue Rails console operation for author preservation."""
        operation = {
            "type": "set_author",
            "jira_key": jira_key,
            "author_id": author_id,
            "author_metadata": author_data,
            "timestamp": self._get_current_timestamp(),
        }
        self._rails_operations_cache.append(operation)

    def execute_rails_author_operations(self, work_package_mapping: dict[str, Any]) -> dict[str, Any]:
        """Execute queued Rails operations for author preservation."""
        if not self._rails_operations_cache:
            return {"processed": 0, "errors": []}

        # Generate Rails script for author updates
        script = self._generate_author_preservation_script(work_package_mapping)
        
        try:
            # Execute via Rails console
            result = self.op_client.rails_client.execute_script(script)
            
            # Clear cache after successful execution
            processed_count = len(self._rails_operations_cache)
            self._rails_operations_cache.clear()
            
            return {
                "processed": processed_count,
                "errors": [],
                "result": result
            }
        except Exception as e:
            self.logger.error("Failed to execute Rails author operations: %s", e)
            return {
                "processed": 0,
                "errors": [str(e)]
            }

    def _generate_author_preservation_script(self, work_package_mapping: dict[str, Any]) -> str:
        """Generate Rails script for preserving author information.
        
        SECURITY: This method generates Ruby code that will be executed in the Rails
        console. To prevent injection attacks, all user-provided data (especially 
        jira_key values) must be validated and properly escaped before inclusion.
        
        Security Measures Implemented:
        1. Validates all jira_key values via _validate_jira_key() before use
        2. Uses json.dumps() to escape jira_key for safe Ruby hash literals  
        3. Wraps each operation in begin/rescue blocks for error isolation
        4. Uses parameterized database queries (WorkPackage.find(id))
        
        Generated Script Structure:
        - Ruby requires json library for safe data handling
        - Each operation wrapped in begin/rescue for error isolation
        - Operations and errors tracked in arrays for audit trail
        - Human-readable output for monitoring and debugging
        
        Args:
            work_package_mapping: Dict mapping work package IDs to their metadata,
                                 including jira_key for cross-reference
                                 
        Returns:
            str: Safe Ruby script ready for Rails console execution
            
        Raises:
            ValueError: If any jira_key fails security validation
            
        Note:
            This method assumes _rails_operations_cache contains validated operations
            from _queue_rails_author_operation(). All external data should be 
            validated before reaching this point.
        """
        script_lines = [
            "# Enhanced Author Preservation Script",
            "require 'json'",
            "",
            "operations = []",
            "errors = []",
            "",
        ]

        for operation in self._rails_operations_cache:
            jira_key = operation["jira_key"]
            author_id = operation["author_id"]
            
            # SECURITY: Validate jira_key before using in script generation
            # This prevents injection attacks by rejecting malicious input
            self._validate_jira_key(jira_key)
            
            # Find OpenProject work package ID from mapping
            wp_id = None
            for mapping_entry in work_package_mapping.values():
                if mapping_entry.get("jira_key") == jira_key:
                    wp_id = mapping_entry.get("openproject_id")
                    break

            if wp_id:
                # SECURITY: Escape jira_key to prevent injection in Ruby hash literals
                # json.dumps() ensures quotes, newlines, and special chars are properly escaped
                # Example: "TEST'; DROP TABLE users;" becomes "\"TEST'; DROP TABLE users;\""
                escaped_jira_key = json.dumps(jira_key)
                script_lines.extend([
                    f"# Update author for work package {wp_id} (Jira: {jira_key})",
                    f"begin",
                    f"  wp = WorkPackage.find({wp_id})",
                    f"  wp.author_id = {author_id}",
                    f"  wp.save(validate: false)  # Skip validations for metadata updates",
                    f"  operations << {{jira_key: {escaped_jira_key}, wp_id: {wp_id}, status: 'success'}}",
                    f"rescue => e",
                    f"  errors << {{jira_key: {escaped_jira_key}, wp_id: {wp_id}, error: e.message}}",
                    f"end",
                    "",
                ])

        script_lines.extend([
            "puts \"Author preservation completed:\"",
            "puts \"Successful operations: #{operations.length}\"",
            "puts \"Errors: #{errors.length}\"",
            "",
            "if errors.any?",
            "  puts \"Errors encountered:\"",
            "  errors.each { |error| puts \"  #{error[:jira_key]}: #{error[:error]}\" }",
            "end",
            "",
            "# Return results",
            "{operations: operations, errors: errors}",
        ])

        return "\n".join(script_lines)

    def save_enhanced_mappings(self) -> None:
        """Save enhanced user mappings to file."""
        try:
            enhanced_mapping_file = config.get_path("data") / "enhanced_user_mappings.json"
            
            # Convert to serializable format
            serializable_mappings = {
                k: dict(v) for k, v in self.enhanced_user_mappings.items()
            }
            
            with enhanced_mapping_file.open("w") as f:
                json.dump(serializable_mappings, f, indent=2)
            
            self.logger.info("Saved enhanced user mappings to %s", enhanced_mapping_file)
        except Exception as e:
            self.logger.error("Failed to save enhanced user mappings: %s", e)

    def generate_association_report(self) -> dict[str, Any]:
        """Generate comprehensive report on user association migration."""
        total_users = len(self.enhanced_user_mappings)
        mapped_users = sum(1 for m in self.enhanced_user_mappings.values() if m["mapping_status"] == "mapped")
        unmapped_users = sum(1 for m in self.enhanced_user_mappings.values() if m["mapping_status"] == "unmapped")
        deleted_users = sum(1 for m in self.enhanced_user_mappings.values() if m["mapping_status"] == "deleted")

        report = {
            "summary": {
                "total_users": total_users,
                "mapped_users": mapped_users,
                "unmapped_users": unmapped_users,
                "deleted_users": deleted_users,
                "mapping_percentage": (mapped_users / total_users * 100) if total_users > 0 else 0,
            },
            "fallback_users": self.fallback_users,
            "rails_operations_pending": len(self._rails_operations_cache),
            "detailed_mappings": dict(self.enhanced_user_mappings),
            "generated_at": self._get_current_timestamp(),
        }

        return report 

    def _load_staleness_config(self) -> None:
        """Load and validate staleness detection configuration."""
        try:
            mapping_config = config.migration_config.get("mapping", {})
            
            # Validate refresh_interval
            self.refresh_interval_seconds = self._parse_duration(
                mapping_config.get("refresh_interval", "24h")
            )
            
            # Validate fallback_strategy
            self.fallback_strategy = self._validate_fallback_strategy(
                mapping_config.get("fallback_strategy", "skip")
            )
            
            # Get admin user ID for assign_admin strategy
            self.admin_user_id = mapping_config.get("fallback_admin_user_id")
            if self.fallback_strategy == "assign_admin" and not self.admin_user_id:
                self.logger.warning(
                    "fallback_strategy is 'assign_admin' but no fallback_admin_user_id configured"
                )
            
            self.logger.debug(
                "Staleness config loaded: refresh_interval=%ds, fallback_strategy=%s",
                self.refresh_interval_seconds, self.fallback_strategy
            )
            
        except Exception as e:
            self.logger.warning("Failed to load staleness configuration: %s", e)
            # Set defaults
            self.refresh_interval_seconds = 24 * 60 * 60  # 24 hours
            self.fallback_strategy = "skip"
            self.admin_user_id = None

    def _parse_duration(self, duration_str: str) -> int:
        """Parse duration string like '1h', '30m', '2d' into seconds.
        
        Args:
            duration_str: Duration string (e.g., '1h', '30m', '2d')
            
        Returns:
            Duration in seconds
            
        Raises:
            ValueError: If duration format is invalid or zero/negative
        """
        duration_str = duration_str.strip()  # Handle whitespace defensively
        pattern = r'^(\d+)([smhd])$'
        match = re.match(pattern, duration_str.lower())
        
        if not match:
            raise ValueError(f"Invalid duration format: {duration_str}")
        
        value, unit = match.groups()
        value = int(value)
        
        # Validate positive non-zero duration
        if value <= 0:
            raise ValueError(f"Duration must be positive: {duration_str}")
        
        multipliers = {
            's': 1,          # seconds
            'm': 60,         # minutes
            'h': 3600,       # hours
            'd': 86400,      # days
        }
        
        return value * multipliers[unit]

    def _validate_fallback_strategy(self, strategy: str) -> FallbackStrategy:
        """Validate fallback strategy value.
        
        Args:
            strategy: Strategy string to validate
            
        Returns:
            Validated fallback strategy
            
        Raises:
            ValueError: If strategy is invalid
        """
        valid_strategies: tuple[FallbackStrategy, ...] = ("skip", "assign_admin", "create_placeholder")
        
        if strategy not in valid_strategies:
            raise ValueError(
                f"Invalid fallback_strategy: {strategy}. "
                f"Valid options: {', '.join(valid_strategies)}"
            )
        
        return strategy  # type: ignore[return-value] 