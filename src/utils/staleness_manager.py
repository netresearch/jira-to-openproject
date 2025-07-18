#!/usr/bin/env python3
"""Staleness Manager for TTL-based user mapping cache with automatic refresh.

This module provides intelligent caching with staleness detection for user mappings
to prevent silent failures from deactivated or changed users. Features include:
- TTL-based staleness detection
- Automatic refresh via JiraClient API calls
- Configurable fallback strategies for failed refreshes
- Comprehensive monitoring and metrics collection
- Exponential backoff retry logic for API failures
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from enum import Enum
from typing import Any, Optional, Dict, List
from pathlib import Path

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient


class FallbackStrategy(Enum):
    """Fallback strategies for handling stale or failed user mappings."""
    SKIP = "skip"
    ASSIGN_ADMIN = "assign_admin"
    CREATE_PLACEHOLDER = "create_placeholder"


@dataclass
class CacheEntry:
    """Represents a cached user mapping with metadata."""
    mapped_user: Optional[Dict[str, Any]]
    last_refreshed: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def age(self) -> timedelta:
        """Calculate the age of this cache entry."""
        return datetime.now(tz=UTC) - self.last_refreshed
    
    def is_stale(self, refresh_interval: timedelta) -> bool:
        """Check if this cache entry is stale based on the refresh interval."""
        return self.age() > refresh_interval or self.mapped_user is None


@dataclass
class RefreshResult:
    """Result of a cache refresh operation."""
    success: bool
    user_data: Optional[Dict[str, Any]]
    error_reason: Optional[str]
    attempts: int
    fallback_applied: bool = False
    fallback_strategy: Optional[FallbackStrategy] = None


class StalenessManager:
    """Manages TTL-based user mapping cache with automatic refresh and fallback strategies.
    
    This component provides:
    - Staleness detection based on configurable TTL
    - Automatic refresh via JiraClient API with retry logic
    - Configurable fallback strategies for validation failures
    - Comprehensive monitoring and metrics collection
    """
    
    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        refresh_interval: str = "24h",
        fallback_strategy: str = "skip",
        fallback_admin_user_id: Optional[str] = None
    ) -> None:
        """Initialize the staleness manager.
        
        Args:
            jira_client: Initialized Jira client for API calls
            op_client: Initialized OpenProject client
            refresh_interval: TTL string (e.g., "24h", "2d", "30m")
            fallback_strategy: Strategy for handling failed refreshes
            fallback_admin_user_id: Admin user ID for assign_admin strategy
        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.logger = config.logger
        
        # Parse configuration
        self.refresh_interval = self._parse_duration(refresh_interval)
        self.fallback_strategy = FallbackStrategy(fallback_strategy)
        self.fallback_admin_user_id = fallback_admin_user_id
        
        # Cache storage
        self.cache: Dict[str, CacheEntry] = {}
        
        # Metrics tracking
        self.metrics = {
            "cache_hits": 0,
            "cache_misses": 0,
            "staleness_detected": 0,
            "refreshes_attempted": 0,
            "refreshes_successful": 0,
            "fallbacks_applied": 0
        }
        
        self.logger.info(
            "StalenessManager initialized: refresh_interval=%s, fallback_strategy=%s",
            self.refresh_interval, self.fallback_strategy.value
        )
    
    def _parse_duration(self, duration_str: str) -> timedelta:
        """Parse duration string into timedelta object.
        
        Args:
            duration_str: Duration string like "24h", "2d", "30m"
            
        Returns:
            timedelta object
            
        Raises:
            ValueError: If duration format is invalid
        """
        duration_str = duration_str.strip().lower()
        
        try:
            if duration_str.endswith('h'):
                hours = int(duration_str[:-1])
                return timedelta(hours=hours)
            elif duration_str.endswith('d'):
                days = int(duration_str[:-1])
                return timedelta(days=days)
            elif duration_str.endswith('m'):
                minutes = int(duration_str[:-1])
                return timedelta(minutes=minutes)
            elif duration_str.endswith('s'):
                seconds = int(duration_str[:-1])
                return timedelta(seconds=seconds)
            else:
                raise ValueError(f"Invalid duration format: {duration_str}. Use format like '24h', '2d', '30m'")
        except (ValueError, TypeError) as e:
            if "Invalid duration format" in str(e):
                raise  # Re-raise our custom message
            else:
                raise ValueError(f"Invalid duration format: {duration_str}. Use format like '24h', '2d', '30m'") from e
    
    def get_cached_mapping(self, user_key: str) -> Optional[Dict[str, Any]]:
        """Get user mapping with automatic staleness detection and refresh.
        
        Args:
            user_key: Jira user key to look up
            
        Returns:
            User mapping data or None if not available/fallback applied
        """
        # Check if entry exists in cache
        if user_key not in self.cache:
            self.logger.debug("Cache miss for user %s", user_key)
            self.metrics["cache_misses"] += 1
            return None
        
        entry = self.cache[user_key]
        
        # Check if entry is stale
        if entry.is_stale(self.refresh_interval):
            self.logger.info("Staleness detected: %s (age: %s)", user_key, entry.age())
            self.metrics["staleness_detected"] += 1
            
            # Attempt refresh
            refresh_result = self._refresh_user_mapping(user_key)
            if refresh_result.success:
                return refresh_result.user_data
            elif refresh_result.fallback_applied:
                return refresh_result.user_data  # May be None for skip strategy
            else:
                return None
        
        # Cache hit with fresh data
        self.logger.debug("Cache hit for user %s", user_key)
        self.metrics["cache_hits"] += 1
        return entry.mapped_user
    
    def add_entry(self, user_key: str, mapped_user: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> None:
        """Add or update a cache entry.
        
        Args:
            user_key: Jira user key
            mapped_user: User mapping data
            metadata: Additional metadata for the entry
        """
        entry = CacheEntry(
            mapped_user=mapped_user,
            last_refreshed=datetime.now(tz=UTC),
            metadata=metadata or {}
        )
        self.cache[user_key] = entry
        
        self.logger.debug("Added cache entry for user %s", user_key)
    
    def _refresh_user_mapping(self, user_key: str) -> RefreshResult:
        """Refresh user mapping with retry logic and fallback handling.
        
        Args:
            user_key: Jira user key to refresh
            
        Returns:
            RefreshResult with success status and data
        """
        self.metrics["refreshes_attempted"] += 1
        
        # Retry logic with exponential backoff
        max_attempts = 3
        base_delay = 0.5  # 500ms base delay
        
        for attempt in range(max_attempts):
            try:
                self.logger.debug("Refreshing user mapping: %s (attempt %d/%d)", user_key, attempt + 1, max_attempts)
                
                # Call JiraClient to get fresh user data
                user_data = self._get_jira_user_data(user_key)
                
                if user_data:
                    # Validate the refreshed user data
                    if self._validate_user_data(user_data):
                        # Success - update cache
                        self.add_entry(user_key, user_data)
                        self.metrics["refreshes_successful"] += 1
                        
                        self.logger.info("User mapping refreshed successfully: %s", user_key)
                        return RefreshResult(
                            success=True,
                            user_data=user_data,
                            error_reason=None,
                            attempts=attempt + 1
                        )
                    else:
                        # Validation failed - apply fallback
                        self.logger.warning("User validation failed for %s: user inactive or data mismatch", user_key)
                        return self._apply_fallback_strategy(user_key, "User validation failed", attempt + 1)
                else:
                    # No user data returned
                    if attempt < max_attempts - 1:
                        delay = base_delay * (2 ** attempt)
                        self.logger.debug("No user data returned, retrying in %s seconds", delay)
                        time.sleep(delay)
                        continue
                    else:
                        # Final attempt failed
                        return self._apply_fallback_strategy(user_key, "User not found after retries", attempt + 1)
                        
            except Exception as e:
                self.logger.warning("Failed to refresh user %s (attempt %d): %s", user_key, attempt + 1, e)
                
                if attempt < max_attempts - 1:
                    delay = base_delay * (2 ** attempt)
                    self.logger.debug("Retrying in %s seconds", delay)
                    time.sleep(delay)
                else:
                    # All attempts exhausted - apply fallback
                    return self._apply_fallback_strategy(user_key, f"API error: {e}", attempt + 1)
        
        # Should not reach here, but safety fallback
        return self._apply_fallback_strategy(user_key, "Unexpected error in refresh", max_attempts)
    
    def _get_jira_user_data(self, user_key: str) -> Optional[Dict[str, Any]]:
        """Get user data from Jira API.
        
        Note: This will need to be implemented in JiraClient as get_user_info method.
        For now, using the existing pattern from EnhancedUserAssociationMigrator.
        
        Args:
            user_key: Jira user key
            
        Returns:
            User data dictionary or None if not found
        """
        try:
            # This calls the existing method that needs to be implemented in JiraClient
            return self.jira_client.get_user_info(user_key)
        except Exception as e:
            self.logger.debug("Failed to get user data from Jira for %s: %s", user_key, e)
            return None
    
    def _validate_user_data(self, user_data: Dict[str, Any]) -> bool:
        """Validate refreshed user data.
        
        Args:
            user_data: User data from Jira API
            
        Returns:
            True if user data is valid and active
        """
        # Check if user is active
        if not user_data.get("active", True):
            return False
        
        # Check if required fields are present
        required_fields = ["accountId", "displayName"]
        for field in required_fields:
            if field not in user_data or not user_data[field]:
                return False
        
        return True
    
    def _apply_fallback_strategy(self, user_key: str, error_reason: str, attempts: int) -> RefreshResult:
        """Apply the configured fallback strategy.
        
        Args:
            user_key: Jira user key
            error_reason: Reason for applying fallback
            attempts: Number of refresh attempts made
            
        Returns:
            RefreshResult with fallback data
        """
        self.metrics["fallbacks_applied"] += 1
        
        self.logger.warning(
            "Applying fallback strategy '%s' for user %s: %s",
            self.fallback_strategy.value, user_key, error_reason
        )
        
        if self.fallback_strategy == FallbackStrategy.SKIP:
            return self._skip_strategy(user_key, error_reason, attempts)
        elif self.fallback_strategy == FallbackStrategy.ASSIGN_ADMIN:
            return self._assign_admin_strategy(user_key, error_reason, attempts)
        elif self.fallback_strategy == FallbackStrategy.CREATE_PLACEHOLDER:
            return self._create_placeholder_strategy(user_key, error_reason, attempts)
        else:
            # Unknown strategy - default to skip
            self.logger.error("Unknown fallback strategy: %s", self.fallback_strategy)
            return self._skip_strategy(user_key, error_reason, attempts)
    
    def _skip_strategy(self, user_key: str, error_reason: str, attempts: int) -> RefreshResult:
        """Skip strategy: omit the association and log warning."""
        self.logger.warning("Skipping user mapping: %s (reason: %s)", user_key, error_reason)
        
        # Remove from cache to prevent future lookups
        if user_key in self.cache:
            del self.cache[user_key]
        
        return RefreshResult(
            success=False,
            user_data=None,
            error_reason=error_reason,
            attempts=attempts,
            fallback_applied=True,
            fallback_strategy=FallbackStrategy.SKIP
        )
    
    def _assign_admin_strategy(self, user_key: str, error_reason: str, attempts: int) -> RefreshResult:
        """Assign admin strategy: map to configurable admin user."""
        if not self.fallback_admin_user_id:
            self.logger.error("Admin user ID not configured for assign_admin fallback")
            return self._skip_strategy(user_key, "No admin user configured", attempts)
        
        admin_user_data = {
            "accountId": self.fallback_admin_user_id,
            "displayName": "Admin User (Fallback)",
            "emailAddress": self.fallback_admin_user_id,
            "active": True,
            "_fallback": True,
            "_original_user": user_key,
            "_fallback_reason": error_reason
        }
        
        # Update cache with admin mapping
        self.add_entry(user_key, admin_user_data, {"fallback": True, "original_error": error_reason})
        
        self.logger.info("Reassigned user %s to admin %s (reason: %s)", 
                        user_key, self.fallback_admin_user_id, error_reason)
        
        return RefreshResult(
            success=False,
            user_data=admin_user_data,
            error_reason=error_reason,
            attempts=attempts,
            fallback_applied=True,
            fallback_strategy=FallbackStrategy.ASSIGN_ADMIN
        )
    
    def _create_placeholder_strategy(self, user_key: str, error_reason: str, attempts: int) -> RefreshResult:
        """Create placeholder strategy: insert placeholder record and flag for manual review."""
        placeholder_data = {
            "accountId": f"PLACEHOLDER_{user_key}",
            "displayName": f"PLACEHOLDER: {user_key}",
            "emailAddress": f"placeholder_{user_key}@pending.review",
            "active": False,
            "_placeholder": True,
            "_original_user": user_key,
            "_manual_review_required": True,
            "_error_reason": error_reason,
            "_created_at": datetime.now(tz=UTC).isoformat()
        }
        
        # Update cache with placeholder
        self.add_entry(user_key, placeholder_data, {
            "placeholder": True, 
            "manual_review": True,
            "original_error": error_reason
        })
        
        self.logger.warning("Created placeholder for %s: %s", user_key, error_reason)
        
        return RefreshResult(
            success=False,
            user_data=placeholder_data,
            error_reason=error_reason,
            attempts=attempts,
            fallback_applied=True,
            fallback_strategy=FallbackStrategy.CREATE_PLACEHOLDER
        )
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics and metrics.
        
        Returns:
            Dictionary with cache statistics
        """
        return {
            "cache_size": len(self.cache),
            "metrics": self.metrics.copy(),
            "config": {
                "refresh_interval": str(self.refresh_interval),
                "fallback_strategy": self.fallback_strategy.value,
                "fallback_admin_user_id": self.fallback_admin_user_id
            }
        }
    
    def clear_cache(self) -> None:
        """Clear all cache entries and reset metrics."""
        self.cache.clear()
        self.metrics = {key: 0 for key in self.metrics}
        self.logger.info("Cache cleared and metrics reset") 