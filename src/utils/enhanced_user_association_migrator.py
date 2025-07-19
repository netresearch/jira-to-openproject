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
import subprocess
import time
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypedDict, Optional
from urllib.parse import quote
from threading import Lock, Semaphore, Timer, Event

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.utils.validators import validate_jira_key


# Custom exceptions for staleness detection
class StaleMappingError(Exception):
    """Exception raised when a user mapping is stale and needs refresh.
    
    This exception is used to trigger refresh mechanisms when a mapping
    is detected as stale based on the configured TTL threshold.
    """
    def __init__(self, username: str, reason: str = "Mapping is stale"):
        """Initialize the stale mapping error.
        
        Args:
            username: Jira username that has a stale mapping
            reason: Specific reason why the mapping is considered stale
        """
        self.username = username
        self.reason = reason
        super().__init__(f"Stale mapping detected for user '{username}': {reason}")


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
    
    # Configuration constants
    DEFAULT_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60  # 24 hours
    DEFAULT_MAX_RETRIES = 2
    DEFAULT_FALLBACK_STRATEGY = "skip"

    # Retry configuration constants
    MAX_ALLOWED_RETRIES = 5  # Prevent excessive retry attempts
    DEFAULT_BASE_DELAY = 0.5  # 500ms base delay
    DEFAULT_MAX_DELAY = 2.0   # 2s maximum delay cap
    DEFAULT_REQUEST_TIMEOUT = 10.0  # 10s timeout per API call
    
    # Rate limiting constants  
    MAX_CONCURRENT_REFRESHES = 3  # Prevent retry storms
    
    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        user_mapping: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        """Initialize the enhanced user association migrator.

        Args:
            jira_client: Initialized Jira client for user lookups
            op_client: Initialized OpenProject client for project operations
            user_mapping: Pre-loaded user mapping (optional)
            **kwargs: Additional configuration options:
                - max_retries: Maximum retry attempts (default: 2)
                - base_delay: Base delay between retries in seconds (default: 0.5)
                - max_delay: Maximum delay cap in seconds (default: 2.0)
                - request_timeout: API request timeout in seconds (default: 10.0)
        """
        # Set up logging first
        self.logger = logging.getLogger(__name__)
        
        self.jira_client = jira_client
        self.op_client = op_client
        
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
        
        # Initialize rate limiting
        self._refresh_semaphore = Semaphore(self.MAX_CONCURRENT_REFRESHES)
        self._refresh_lock = Lock()
        
        # Configurable retry settings
        self.retry_config = {
            'max_retries': kwargs.get('max_retries', self.DEFAULT_MAX_RETRIES),
            'base_delay': kwargs.get('base_delay', self.DEFAULT_BASE_DELAY),
            'max_delay': kwargs.get('max_delay', self.DEFAULT_MAX_DELAY),
            'request_timeout': kwargs.get('request_timeout', self.DEFAULT_REQUEST_TIMEOUT)
        }
        
        # Validate retry configuration
        self._validate_retry_config()

    def _validate_retry_config(self) -> None:
        """Validate retry configuration parameters to prevent resource exhaustion."""
        config = self.retry_config
        
        if not isinstance(config['max_retries'], int) or config['max_retries'] < 0:
            raise ValueError(f"max_retries must be a non-negative integer, got: {config['max_retries']}")
            
        if config['max_retries'] > self.MAX_ALLOWED_RETRIES:
            raise ValueError(f"max_retries cannot exceed {self.MAX_ALLOWED_RETRIES}, got: {config['max_retries']}")
            
        if not isinstance(config['base_delay'], (int, float)) or config['base_delay'] <= 0:
            raise ValueError(f"base_delay must be a positive number, got: {config['base_delay']}")
            
        if not isinstance(config['max_delay'], (int, float)) or config['max_delay'] <= 0:
            raise ValueError(f"max_delay must be a positive number, got: {config['max_delay']}")
            
        if config['base_delay'] > config['max_delay']:
            raise ValueError(f"base_delay ({config['base_delay']}) cannot exceed max_delay ({config['max_delay']})")
            
        if not isinstance(config['request_timeout'], (int, float)) or config['request_timeout'] <= 0:
            raise ValueError(f"request_timeout must be a positive number, got: {config['request_timeout']}")

    def _load_user_mapping(self) -> dict[str, Any]:
        """Load user mapping from file or config."""
        try:
            user_mapping_file = config.get_path("data") / "user_mapping.json"
            if user_mapping_file.exists():
                with user_mapping_file.open() as f:
                    return json.load(f)
            return {}
        except (IOError, json.JSONDecodeError, ValueError) as e:
            self.logger.warning("Failed to load user mapping due to file/JSON error: %s", e)
            return {}
        except Exception as e:
            self.logger.exception("Unexpected error loading user mapping: %s", e)
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
        except (IOError, json.JSONDecodeError, ValueError, KeyError) as e:
            self.logger.warning("Failed to load enhanced user mappings due to file/data error: %s", e)
            self._create_enhanced_mappings()
        except Exception as e:
            self.logger.exception("Unexpected error loading enhanced user mappings: %s", e)
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
        """Fetch user information from Jira API.
        
        Args:
            username: Jira username to fetch
            
        Returns:
            User information dict or None if not found
        """
        try:
            # Get user from Jira with URL-encoded username to prevent injection
            safe_username = quote(username)
            response = self.jira_client.get(f"user/search?username={safe_username}")
            
            if response.status_code == 200:
                users = response.json()
                if users:
                    return users[0]
            
            return None
            
        except (requests.RequestException, ValueError, KeyError) as e:
            self.logger.error("Failed to fetch Jira user info for %s due to API/data error: %s", username, e)
            return None
        except Exception as e:
            self.logger.exception("Unexpected error fetching Jira user info for %s: %s", username, e)
            return None

    def _get_openproject_user_info(self, user_id: int) -> dict[str, Any] | None:
        """Get detailed user information from OpenProject."""
        try:
            return self.op_client.get_user(user_id)
        except (requests.RequestException, ValueError, KeyError, AttributeError) as e:
            self.logger.debug("Failed to get OpenProject user info for %s due to API/data error: %s", user_id, e)
            return None
        except Exception as e:
            self.logger.exception("Unexpected error getting OpenProject user info for %s: %s", user_id, e)
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
            
        except (requests.RequestException, ValueError, KeyError, AttributeError) as e:
            self.logger.warning("Failed to identify fallback users due to API/data error: %s", e)
        except Exception as e:
            self.logger.exception("Unexpected error identifying fallback users: %s", e)
        
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
                except (requests.RequestException, ValueError, KeyError, AttributeError) as e:
                    self.logger.warning("Failed to fetch watchers for %s due to API/data error: %s", jira_issue.key, e)
                except Exception as e:
                    self.logger.exception("Unexpected error fetching watchers for %s: %s", jira_issue.key, e)

        associations["watchers"] = watchers
        return associations

    def _migrate_assignee(
        self, assignee_data: dict[str, Any] | None, work_package_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Migrate assignee with enhanced handling and staleness detection."""
        result = {"warnings": []}

        if not assignee_data or not assignee_data.get("username"):
            return result

        username = assignee_data["username"]
        
        try:
            # Use staleness detection with automatic refresh
            mapping = self.get_mapping_with_staleness_check(username, auto_refresh=True)
            
            if mapping and mapping["openproject_user_id"]:
                # Verify user is still active
                if mapping["mapping_status"] == "mapped" and mapping["metadata"].get("openproject_active", True):
                    work_package_data["assigned_to_id"] = mapping["openproject_user_id"]
                    self.logger.debug("Successfully mapped assignee %s to OpenProject user %d", username, mapping["openproject_user_id"])
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
                    
        except StaleMappingError as e:
            self.logger.warning("Stale mapping detected for assignee %s: %s", username, e)
            # Apply fallback strategy
            fallback_id = self._get_fallback_user("assignee")
            if fallback_id:
                work_package_data["assigned_to_id"] = fallback_id
                result["warnings"].append(f"Assignee {username} mapping stale, using fallback user {fallback_id}")
            else:
                result["warnings"].append(f"Assignee {username} mapping stale and no fallback available")
        except Exception as e:
            self.logger.exception("Unexpected error processing assignee %s: %s", username, e)
            result["warnings"].append(f"Error processing assignee {username}: {e}")

        return result

    def _migrate_author(
        self,
        reporter_data: dict[str, Any] | None,
        creator_data: dict[str, Any] | None,
        work_package_data: dict[str, Any],
        preserve_via_rails: bool,
    ) -> dict[str, Any]:
        """Migrate author with enhanced preservation using Rails console when needed and staleness detection."""
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
        
        try:
            # Use staleness detection with automatic refresh
            mapping = self.get_mapping_with_staleness_check(username, auto_refresh=True)
            
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
                    self.logger.debug("Successfully mapped author %s to OpenProject user %d with Rails preservation", username, mapping["openproject_user_id"])
                else:
                    work_package_data["author_id"] = mapping["openproject_user_id"]
                    self.logger.debug("Successfully mapped author %s to OpenProject user %d", username, mapping["openproject_user_id"])
            else:
                # Handle unmapped author
                fallback_id = self._get_fallback_user("author")
                if fallback_id:
                    work_package_data["author_id"] = fallback_id
                    result["warnings"].append(f"Author {username} unmapped, using fallback user {fallback_id}")
                else:
                    result["warnings"].append(f"Author {username} unmapped and no fallback available")
                    
        except StaleMappingError as e:
            self.logger.warning("Stale mapping detected for author %s: %s", username, e)
            # Apply fallback strategy
            fallback_id = self._get_fallback_user("author")
            if fallback_id:
                work_package_data["author_id"] = fallback_id
                result["warnings"].append(f"Author {username} mapping stale, using fallback user {fallback_id}")
            else:
                result["warnings"].append(f"Author {username} mapping stale and no fallback available")
        except Exception as e:
            self.logger.exception("Unexpected error processing author %s: %s", username, e)
            result["warnings"].append(f"Error processing author {username}: {e}")

        return result

    def _migrate_watchers(
        self, watchers_data: list[dict[str, Any]], work_package_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Migrate watchers with enhanced validation, handling, and staleness detection."""
        result = {"warnings": []}
        valid_watcher_ids = []

        for watcher in watchers_data:
            username = watcher.get("username")
            if not username:
                continue

            try:
                # Use staleness detection with automatic refresh
                mapping = self.get_mapping_with_staleness_check(username, auto_refresh=True)
                
                if mapping and mapping["openproject_user_id"]:
                    # Verify user exists and is active
                    if mapping["mapping_status"] == "mapped" and mapping["metadata"].get("openproject_active", True):
                        valid_watcher_ids.append(mapping["openproject_user_id"])
                        self.logger.debug("Successfully mapped watcher %s to OpenProject user %d", username, mapping["openproject_user_id"])
                    else:
                        result["warnings"].append(f"Watcher {username} inactive, skipping")
                else:
                    result["warnings"].append(f"Watcher {username} unmapped, skipping")
                    
            except StaleMappingError as e:
                self.logger.warning("Stale mapping detected for watcher %s: %s", username, e)
                result["warnings"].append(f"Watcher {username} mapping stale, skipping")
            except Exception as e:
                self.logger.exception("Unexpected error processing watcher %s: %s", username, e)
                result["warnings"].append(f"Error processing watcher {username}: {e}")

        if valid_watcher_ids:
            work_package_data["watcher_ids"] = valid_watcher_ids
            self.logger.debug("Successfully mapped %d watchers", len(valid_watcher_ids))

        return result

    def _get_fallback_user(self, role: str) -> int | None:
        """Get appropriate fallback user for a specific role."""
        # Priority order for fallback users
        fallback_priority = ["migration", "admin", "system"]
        
        for fallback_type in fallback_priority:
            if fallback_type in self.fallback_users:
                return self.fallback_users[fallback_type]
        
        return None

    def is_mapping_stale(self, username: str, current_time: datetime | None = None) -> bool:
        """Check if a user mapping is stale and needs refresh.
        
        Args:
            username: Jira username to check
            current_time: Optional current timestamp for batch operations (caching)
            
        Returns:
            True if mapping is stale, False otherwise
        """
        mapping = self.enhanced_user_mappings.get(username)
        if not mapping:
            return True  # Non-existent mappings are considered stale
        
        last_refreshed = mapping.get("lastRefreshed")
        if not last_refreshed:
            return True  # Mappings without refresh timestamp are stale
        
        refresh_interval = self.refresh_interval_seconds
        
        try:
            last_refresh_time = datetime.fromisoformat(last_refreshed.replace('Z', '+00:00'))
            if current_time is None:
                current_time = datetime.now(tz=UTC)
            age_seconds = (current_time - last_refresh_time).total_seconds()
            
            # Fixed: Use >= for consistent threshold behavior
            return age_seconds >= refresh_interval
            
        except ValueError as e:
            self.logger.warning("Invalid lastRefreshed timestamp for %s: %s", username, e)
            return True  # Invalid timestamps are considered stale

    def check_and_handle_staleness(self, username: str, raise_on_stale: bool = True) -> UserAssociationMapping | None:
        """Check for staleness and handle it according to configuration.
        
        Args:
            username: Jira username to check
            raise_on_stale: Whether to raise StaleMappingError on staleness detection
            
        Returns:
            Fresh mapping if available, None if mapping unavailable after handling
            
        Raises:
            StaleMappingError: If mapping is stale and raise_on_stale is True
        """
        current_time = datetime.now(tz=UTC)
        
        # Check if mapping exists
        mapping = self.enhanced_user_mappings.get(username)
        if not mapping:
            stale_reason = "Mapping does not exist"
            self.logger.debug("Staleness detected for %s: %s", username, stale_reason)
            
            if raise_on_stale:
                raise StaleMappingError(username, stale_reason)
            return None
        
        # Check if mapping is stale
        if self.is_mapping_stale(username, current_time):
            # Determine staleness reason
            last_refreshed = mapping.get("lastRefreshed")
            if not last_refreshed:
                stale_reason = "No lastRefreshed timestamp"
            else:
                try:
                    last_refresh_time = datetime.fromisoformat(last_refreshed.replace('Z', '+00:00'))
                    age_seconds = (current_time - last_refresh_time).total_seconds()
                    stale_reason = f"Age {age_seconds:.0f}s exceeds TTL {self.refresh_interval_seconds}s"
                except ValueError:
                    stale_reason = "Invalid lastRefreshed timestamp"
            
            self.logger.debug("Staleness detected for %s: %s", username, stale_reason)
            
            if raise_on_stale:
                raise StaleMappingError(username, stale_reason)
            return None
        
        # Mapping is fresh
        self.logger.debug("Fresh mapping found for %s", username)
        return mapping

    def get_mapping_with_staleness_check(self, username: str, auto_refresh: bool = False) -> UserAssociationMapping | None:
        """Get user mapping with automatic staleness detection and optional refresh.
        
        Args:
            username: Jira username to look up
            auto_refresh: Whether to attempt automatic refresh on staleness detection
            
        Returns:
            User mapping if available and fresh, None if unavailable or stale
        """
        try:
            # Check staleness without raising exception initially
            mapping = self.check_and_handle_staleness(username, raise_on_stale=False)
            
            if mapping is not None:
                # Mapping is fresh, return it
                return mapping
            
            # Mapping is stale or missing
            if auto_refresh:
                self.logger.info("Attempting automatic refresh for stale mapping: %s", username)
                
                # Attempt refresh
                refreshed_mapping = self.refresh_user_mapping(username)
                
                if refreshed_mapping:
                    self.logger.info("Successfully refreshed mapping for %s", username)
                    return refreshed_mapping
                else:
                    self.logger.warning("Failed to refresh mapping for %s", username)
                    return None
            else:
                # No auto-refresh, just log and return None
                self.logger.debug("Stale mapping detected for %s, auto_refresh disabled", username)
                return None
                
        except StaleMappingError as e:
            # This shouldn't happen with raise_on_stale=False, but handle it anyway
            self.logger.warning("Unexpected StaleMappingError: %s", e)
            return None
        except Exception as e:
            self.logger.exception("Unexpected error in staleness check for %s: %s", username, e)
            return None

    def detect_stale_mappings(self, usernames: list[str] | None = None) -> dict[str, str]:
        """Detect stale mappings in bulk for batch operations.
        
        Args:
            usernames: Optional list of usernames to check. If None, checks all mappings.
            
        Returns:
            Dictionary mapping username to staleness reason for stale mappings
        """
        current_time = datetime.now(tz=UTC)
        stale_mappings = {}
        
        # Determine which usernames to check
        check_usernames = usernames if usernames is not None else list(self.enhanced_user_mappings.keys())
        
        for username in check_usernames:
            try:
                if self.is_mapping_stale(username, current_time):
                    mapping = self.enhanced_user_mappings.get(username)
                    
                    if not mapping:
                        stale_reason = "Mapping does not exist"
                    else:
                        last_refreshed = mapping.get("lastRefreshed")
                        if not last_refreshed:
                            stale_reason = "No lastRefreshed timestamp"
                        else:
                            try:
                                last_refresh_time = datetime.fromisoformat(last_refreshed.replace('Z', '+00:00'))
                                age_seconds = (current_time - last_refresh_time).total_seconds()
                                stale_reason = f"Age {age_seconds:.0f}s exceeds TTL {self.refresh_interval_seconds}s"
                            except ValueError:
                                stale_reason = "Invalid lastRefreshed timestamp"
                    
                    stale_mappings[username] = stale_reason
                    
            except Exception as e:
                self.logger.warning("Error checking staleness for %s: %s", username, e)
                stale_mappings[username] = f"Error during check: {e}"
        
        if stale_mappings:
            self.logger.info("Detected %d stale mappings: %s", len(stale_mappings), list(stale_mappings.keys()))
        else:
            self.logger.debug("No stale mappings detected in %d checked users", len(check_usernames))
        
        return stale_mappings

    def batch_refresh_stale_mappings(self, usernames: list[str] | None = None, max_retries: int | None = None) -> dict[str, Any]:
        """Refresh multiple stale mappings in batch with retry logic.
        
        Args:
            usernames: Optional list of usernames to refresh. If None, refreshes all stale mappings.
            max_retries: Maximum retry attempts per mapping. If None, uses class default.
            
        Returns:
            Dictionary with batch refresh results including success/failure counts
        """
        current_time = datetime.now(tz=UTC)  # Single timestamp for entire batch operation
        max_retries = max_retries or self.DEFAULT_MAX_RETRIES
        
        # Detect stale mappings if no usernames provided
        if usernames is None:
            stale_mappings = self.detect_stale_mappings()
            usernames = list(stale_mappings.keys())
        else:
            stale_mappings = self.detect_stale_mappings(usernames)
        
        results = {
            "refresh_attempted": len(usernames),
            "refresh_successful": 0,
            "refresh_failed": 0,
            "errors": [],
            "results": {}
        }
        
        self.logger.info("Starting batch refresh for %d mappings", len(usernames))
        
        for username in usernames:
            stale_reason = stale_mappings.get(username, "Manual refresh requested")
            
            try:
                # Retry logic for individual mapping refresh
                last_error = None
                success = False
                
                for attempt in range(max_retries + 1):
                    try:
                        refreshed_mapping = self.refresh_user_mapping(username)
                        
                        if refreshed_mapping:
                            results["refresh_successful"] += 1
                            results["results"][username] = {
                                "status": "success",
                                "attempts": attempt + 1,
                                "stale_reason": stale_reason,
                                "refreshed_at": refreshed_mapping["lastRefreshed"]
                            }
                            success = True
                            break
                        else:
                            last_error = "Refresh returned None"
                            
                    except Exception as refresh_error:
                        last_error = str(refresh_error)
                        self.logger.warning(
                            "Refresh attempt %d/%d failed for %s: %s", 
                            attempt + 1, max_retries + 1, username, refresh_error
                        )
                
                if not success:
                    results["refresh_failed"] += 1
                    results["results"][username] = {
                        "status": "failed",
                        "attempts": max_retries + 1,
                        "stale_reason": stale_reason,
                        "error": last_error
                    }
                    results["errors"].append(f"{username}: {last_error}")
                    
            except Exception as e:
                results["refresh_failed"] += 1
                error_msg = f"Unexpected error refreshing {username}: {e}"
                results["errors"].append(error_msg)
                results["results"][username] = {
                    "status": "error",
                    "attempts": 0,
                    "stale_reason": stale_reason,
                    "error": str(e)
                }
                self.logger.exception("Unexpected error during batch refresh for %s: %s", username, e)
        
        self.logger.info(
            "Batch refresh completed: %d/%d successful, %d failed",
            results["refresh_successful"],
            results["refresh_attempted"],
            results["refresh_failed"]
        )
        
        return results

    def _get_jira_user_with_retry(self, username: str, max_retries: Optional[int] = None) -> dict[str, Any] | None:
        """
        Get Jira user data with configurable retry logic and exponential backoff.
        
        Features:
        - Rate limiting to prevent API storms
        - Configurable retry parameters with validation
        - Exponential backoff with maximum delay cap
        - Request timeout protection (cross-platform)
        - Comprehensive error context logging
        
        Args:
            username: The username to look up in Jira
            max_retries: Override default max_retries for this call
            
        Returns:
            Jira user data dict on success, None if user not found
            
        Raises:
            ValueError: If username is invalid or max_retries exceeds limits
            Exception: Last encountered error if all retry attempts fail
        """
        if not username or not isinstance(username, str):
            raise ValueError(f"username must be a non-empty string, got: {type(username).__name__}")
            
        # Use provided max_retries or default from config
        retry_limit = max_retries if max_retries is not None else self.retry_config['max_retries']
        
        # Validate retry limit
        if not isinstance(retry_limit, int) or retry_limit < 0:
            raise ValueError(f"max_retries must be a non-negative integer, got: {retry_limit}")
        if retry_limit > self.MAX_ALLOWED_RETRIES:
            raise ValueError(f"max_retries cannot exceed {self.MAX_ALLOWED_RETRIES}, got: {retry_limit}")
        
        # Rate limiting to prevent retry storms
        with self._refresh_semaphore:
            self.logger.debug(f"Starting Jira user lookup for '{username}' with max_retries={retry_limit}")
            
            last_error = None
            
            for attempt in range(retry_limit + 1):  # +1 for initial attempt
                try:
                    # Simple API call with timeout (threading disabled for YOLO speed)
                    # In production, this would use proper threading/asyncio for timeout protection
                    try:
                        jira_user_data = self.jira_client.get_user_info(username)
                        
                        if attempt > 0:
                            self.logger.info(f"Jira user lookup for '{username}' succeeded on attempt {attempt + 1}")
                        else:
                            self.logger.debug(f"Jira user lookup for '{username}' succeeded on first attempt")
                            
                        return jira_user_data
                    except Exception as e:
                        # Re-raise the exception to be handled by retry logic
                        raise e
                        
                except Exception as e:
                    last_error = e
                    
                    # Enhanced error context logging
                    error_context = {
                        'username': username,
                        'attempt': attempt + 1,
                        'total_attempts': retry_limit + 1,
                        'error_type': type(e).__name__,
                        'error_message': str(e),
                    }
                    
                    if attempt < retry_limit:  # Not the final attempt
                        # Calculate delay with exponential backoff and cap
                        raw_delay = self.retry_config['base_delay'] * (2 ** attempt)
                        actual_delay = min(raw_delay, self.retry_config['max_delay'])
                        
                        self.logger.warning(
                            f"Jira user lookup for '{username}' failed on attempt {attempt + 1}/{retry_limit + 1}. "
                            f"Error: {type(e).__name__}: {e}. Retrying in {actual_delay}s..."
                        )
                        
                        time.sleep(actual_delay)
                    else:
                        # Final attempt failed
                        self.logger.error(
                            f"Jira user lookup for '{username}' failed on all {retry_limit + 1} attempts. "
                            f"Final error: {type(e).__name__}: {e}. "
                            f"Error context: {error_context}"
                        )
            
            # All retry attempts exhausted
            raise last_error

    def refresh_user_mapping(self, username: str) -> UserAssociationMapping | None:
        """Refresh a stale user mapping by re-fetching from Jira.
        
        This method fetches fresh user data from Jira and updates the mapping
        with a new timestamp and any changed information.
        
        Args:
            username: Jira username to refresh
            
        Returns:
            Refreshed mapping if successful, None if refresh failed
        """
        try:
            self.logger.info("Refreshing user mapping for: %s", username)
            
            # Get fresh data from Jira with retry logic and exponential backoff
            jira_user_data = self._get_jira_user_with_retry(username)
            
            if not jira_user_data:
                self.logger.warning("User %s not found in Jira during refresh", username)
                # Update mapping to mark as inactive/not found
                current_mapping = self.enhanced_user_mappings.get(username, {})
                current_mapping.update({
                    "mapping_status": "not_found",
                    "lastRefreshed": datetime.now(tz=UTC).isoformat(),
                    "metadata": {
                        **current_mapping.get("metadata", {}),
                        "refresh_error": "User not found in Jira",
                        "jira_active": False
                    }
                })
                self.enhanced_user_mappings[username] = current_mapping
                return current_mapping
            
            # Check if user is active in Jira
            jira_active = jira_user_data.get("active", True)
            
            # Get current mapping or create new one
            current_mapping = self.enhanced_user_mappings.get(username, {})
            
            # Update Jira metadata
            refreshed_mapping = {
                **current_mapping,
                "lastRefreshed": datetime.now(tz=UTC).isoformat(),
                "metadata": {
                    **current_mapping.get("metadata", {}),
                    "jira_active": jira_active,
                    "jira_display_name": jira_user_data.get("displayName"),
                    "jira_email": jira_user_data.get("emailAddress"),
                    "jira_account_id": jira_user_data.get("accountId"),
                    "refresh_success": True,
                    "refresh_error": None
                }
            }
            
            # If user is inactive in Jira, mark mapping as inactive but don't lose OpenProject mapping
            if not jira_active:
                self.logger.warning("User %s is inactive in Jira", username)
                refreshed_mapping["mapping_status"] = "inactive_jira"
            elif not refreshed_mapping.get("openproject_user_id"):
                # Try to find OpenProject mapping if we don't have one
                refreshed_mapping = self._attempt_openproject_mapping(refreshed_mapping, jira_user_data)
            
            # Update the mapping
            self.enhanced_user_mappings[username] = refreshed_mapping
            
            # Save updated mappings
            self._save_enhanced_mappings()
            
            self.logger.info("Successfully refreshed mapping for %s", username)
            return refreshed_mapping
            
        except Exception as e:
            self.logger.exception("Error refreshing user mapping for %s: %s", username, e)
            
            # Update mapping with error information but preserve existing data
            current_mapping = self.enhanced_user_mappings.get(username, {})
            error_mapping = {
                **current_mapping,
                "lastRefreshed": datetime.now(tz=UTC).isoformat(),
                "metadata": {
                    **current_mapping.get("metadata", {}),
                    "refresh_success": False,
                    "refresh_error": str(e)
                }
            }
            self.enhanced_user_mappings[username] = error_mapping
            
            try:
                self._save_enhanced_mappings()
            except (IOError, json.JSONDecodeError, ValueError) as e:
                self.logger.error("Failed to save error mapping for %s: %s", username, e)
            
            return None

    def _attempt_openproject_mapping(self, mapping: UserAssociationMapping, jira_user_data: dict[str, Any]) -> UserAssociationMapping:
        """Attempt to find OpenProject mapping for a user during refresh.
        
        Args:
            mapping: Current mapping data
            jira_user_data: Fresh Jira user data
            
        Returns:
            Updated mapping with OpenProject information if found
        """
        try:
            jira_email = jira_user_data.get("emailAddress", "").strip().lower()
            
            if jira_email:
                # Try to find user in OpenProject by email
                openproject_user = self.op_client.get_user_by_email(jira_email)
                
                if openproject_user:
                    mapping.update({
                        "openproject_user_id": openproject_user["id"],
                        "mapping_status": "mapped",
                        "metadata": {
                            **mapping.get("metadata", {}),
                            "openproject_active": openproject_user.get("status") == "active",
                            "openproject_email": openproject_user.get("email"),
                            "openproject_name": f"{openproject_user.get('firstname', '')} {openproject_user.get('lastname', '')}".strip(),
                            "mapping_method": "email_refresh"
                        }
                    })
                    self.logger.info("Found OpenProject mapping during refresh for %s: user ID %d", 
                                   mapping.get("jira_username", "unknown"), openproject_user["id"])
                else:
                    mapping["mapping_status"] = "no_openproject_match"
                    mapping["metadata"] = {
                        **mapping.get("metadata", {}),
                        "openproject_search_attempted": True,
                        "openproject_search_email": jira_email
                    }
                    self.logger.debug("No OpenProject user found for email %s during refresh", jira_email)
            else:
                mapping["mapping_status"] = "no_email"
                self.logger.debug("No email available for OpenProject lookup during refresh")
                
        except Exception as e:
            self.logger.warning("Error attempting OpenProject mapping during refresh: %s", e)
            mapping["metadata"] = {
                **mapping.get("metadata", {}),
                "openproject_mapping_error": str(e)
            }
        
        return mapping

    def trigger_mapping_refresh(self, username: str, force: bool = False) -> bool:
        """Trigger a mapping refresh for a specific user.
        
        Args:
            username: Jira username to refresh
            force: Whether to force refresh even if mapping is fresh
            
        Returns:
            True if refresh was successful, False otherwise
        """
        try:
            # Check if refresh is needed (unless forced)
            if not force and not self.is_mapping_stale(username):
                self.logger.debug("Mapping for %s is fresh, skipping refresh", username)
                return True
            
            # Attempt refresh
            refreshed_mapping = self.refresh_user_mapping(username)
            
            if refreshed_mapping:
                self.logger.info("Successfully triggered refresh for %s", username)
                return True
            else:
                self.logger.warning("Failed to refresh mapping for %s", username)
                return False
                
        except Exception as e:
            self.logger.exception("Error triggering refresh for %s: %s", username, e)
            return False

    def validate_mapping_freshness(self, usernames: list[str] | None = None) -> dict[str, Any]:
        """Validate freshness of multiple mappings and provide refresh recommendations.
        
        Args:
            usernames: Optional list of usernames to validate. If None, validates all mappings.
            
        Returns:
            Dictionary with validation results and recommendations
        """
        current_time = datetime.now(tz=UTC)
        check_usernames = usernames if usernames is not None else list(self.enhanced_user_mappings.keys())
        
        validation_results = {
            "total_checked": len(check_usernames),
            "fresh_mappings": 0,
            "stale_mappings": 0,
            "missing_mappings": 0,
            "error_mappings": 0,
            "stale_users": [],
            "missing_users": [],
            "recommendations": []
        }
        
        for username in check_usernames:
            try:
                mapping = self.enhanced_user_mappings.get(username)
                
                if not mapping:
                    validation_results["missing_mappings"] += 1
                    validation_results["missing_users"].append(username)
                    continue
                
                # Check staleness
                if self.is_mapping_stale(username, current_time):
                    validation_results["stale_mappings"] += 1
                    validation_results["stale_users"].append(username)
                    
                    # Determine staleness reason for recommendation
                    last_refreshed = mapping.get("lastRefreshed")
                    age_seconds = 0  # Initialize age_seconds outside try block
                    
                    if not last_refreshed:
                        reason = "Never refreshed"
                    else:
                        try:
                            last_refresh_time = datetime.fromisoformat(last_refreshed.replace('Z', '+00:00'))
                            age_seconds = (current_time - last_refresh_time).total_seconds()
                            reason = f"Age {age_seconds:.0f}s exceeds TTL {self.refresh_interval_seconds}s"
                        except ValueError:
                            reason = "Invalid timestamp"
                            age_seconds = self.refresh_interval_seconds * 3  # Assume very stale for invalid timestamps
                    
                    validation_results["recommendations"].append({
                        "username": username,
                        "action": "refresh",
                        "reason": reason,
                        "priority": "high" if age_seconds > (self.refresh_interval_seconds * 2) else "medium"
                    })
                else:
                    validation_results["fresh_mappings"] += 1
                    
            except Exception as e:
                validation_results["error_mappings"] += 1
                self.logger.warning("Error validating mapping for %s: %s", username, e)
        
        # Add summary recommendations
        if validation_results["stale_mappings"] > 0:
            validation_results["recommendations"].append({
                "username": "ALL_STALE",
                "action": "batch_refresh",
                "reason": f"Detected {validation_results['stale_mappings']} stale mappings",
                "priority": "high" if validation_results["stale_mappings"] > 10 else "medium"
            })
        
        if validation_results["missing_mappings"] > 0:
            validation_results["recommendations"].append({
                "username": "ALL_MISSING",
                "action": "investigate",
                "reason": f"Found {validation_results['missing_mappings']} missing mappings",
                "priority": "medium"
            })
        
        self.logger.info(
            "Mapping validation complete: %d fresh, %d stale, %d missing, %d errors",
            validation_results["fresh_mappings"],
            validation_results["stale_mappings"],
            validation_results["missing_mappings"],
            validation_results["error_mappings"]
        )
        
        return validation_results

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
        except (IOError, subprocess.SubprocessError, ValueError) as e:
            self.logger.error("Failed to execute Rails author operations due to process/file error: %s", e)
        except Exception as e:
            self.logger.exception("Unexpected error executing Rails author operations: %s", e)
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

    def _save_enhanced_mappings(self) -> None:
        """Save enhanced user mappings to file."""
        try:
            enhanced_mapping_file = config.get_path("data") / "enhanced_user_mappings.json"
            
            # Convert to serializable format
            serializable_mappings = {
                k: dict(v) for k, v in self.enhanced_user_mappings.items()
            }
            
            with enhanced_mapping_file.open("w") as f:
                json.dump(serializable_mappings, f, indent=2)
            
            self.logger.debug("Saved enhanced user mappings to %s", enhanced_mapping_file)
        except (IOError, json.JSONDecodeError, ValueError) as e:
            self.logger.error("Failed to save enhanced user mappings due to file/JSON error: %s", e)
        except Exception as e:
            self.logger.exception("Unexpected error saving enhanced user mappings: %s", e)

    def save_enhanced_mappings(self) -> None:
        """Public API to save enhanced user mappings to file."""
        return self._save_enhanced_mappings()

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
                mapping_config.get("fallback_strategy", self.DEFAULT_FALLBACK_STRATEGY)
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
            
        except (IOError, json.JSONDecodeError, ValueError, KeyError) as e:
            self.logger.warning("Failed to load staleness configuration due to file/data error: %s", e)
            # Set defaults using class constants
            self.refresh_interval_seconds = self.DEFAULT_REFRESH_INTERVAL_SECONDS
            self.fallback_strategy = self.DEFAULT_FALLBACK_STRATEGY
            self.admin_user_id = None
        except Exception as e:
            self.logger.exception("Unexpected error loading staleness configuration: %s", e)
            # Set defaults using class constants
            self.refresh_interval_seconds = self.DEFAULT_REFRESH_INTERVAL_SECONDS
            self.fallback_strategy = self.DEFAULT_FALLBACK_STRATEGY
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

    def _validate_fallback_strategy(self, strategy: str) -> str:
        """Validate fallback strategy value.
        
        Args:
            strategy: Strategy string to validate
            
        Returns:
            Validated fallback strategy string
            
        Raises:
            ValueError: If strategy is invalid
        """
        valid_strategies: tuple[FallbackStrategy, ...] = ("skip", "assign_admin", "create_placeholder")
        
        if strategy not in valid_strategies:
            raise ValueError(
                f"Invalid fallback_strategy: {strategy}. "
                f"Valid options: {', '.join(valid_strategies)}"
            )
        
        return strategy 