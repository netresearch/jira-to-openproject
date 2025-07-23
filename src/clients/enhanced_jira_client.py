#!/usr/bin/env python3
"""Enhanced Jira Client with Performance Optimizations.

This enhanced client provides:
1. Batch API operations for bulk data retrieval
2. Response caching with TTL
3. Connection pooling and session reuse
4. Parallel processing for independent requests
5. Adaptive rate limiting
6. Memory-efficient streaming pagination
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterator, List, Optional, Union
from urllib.parse import urljoin

from jira import JIRA, Issue, JIRAError
from jira.exceptions import JIRAError as JiraApiError

from src.clients.jira_client import JiraClient
from src.utils.performance_optimizer import (
    PerformanceOptimizer,
    StreamingPaginator,
    cached,
    rate_limited,
    batched
)
from src.utils.config_validation import SecurityValidator, ConfigurationValidationError


logger = logging.getLogger(__name__)


class EnhancedJiraClient(JiraClient):
    """Enhanced Jira Client with performance optimizations."""
    
    def __init__(self, server: str, username: str, password: str, **kwargs):
        super().__init__(server, username, password, **kwargs)
        
        # Validate performance configuration parameters using SecurityValidator
        try:
            cache_size = SecurityValidator.validate_numeric_parameter('cache_size', kwargs.get('cache_size', 2000))
            cache_ttl = SecurityValidator.validate_numeric_parameter('cache_ttl', kwargs.get('cache_ttl', 1800))
            batch_size = SecurityValidator.validate_numeric_parameter('batch_size', kwargs.get('batch_size', 100))
            max_workers = SecurityValidator.validate_numeric_parameter('max_workers', kwargs.get('max_workers', 15))
            rate_limit = SecurityValidator.validate_numeric_parameter('rate_limit_per_sec', kwargs.get('rate_limit', 15.0))
            
            # Validate resource allocation to prevent system overload  
            SecurityValidator.validate_resource_allocation(batch_size, max_workers, 2048)  # 2GB memory limit for enhanced client
            
        except ConfigurationValidationError as e:
            logger.error(f"EnhancedJiraClient configuration validation failed: {e}")
            raise
        
        # Initialize performance optimizer with validated parameters
        self.performance_optimizer = PerformanceOptimizer(
            cache_size=cache_size,
            cache_ttl=cache_ttl,
            batch_size=batch_size,
            max_workers=max_workers,
            rate_limit=rate_limit
        )
        
        self.batch_size = batch_size
        self.parallel_workers = max_workers

    # ===== BATCH OPERATIONS =====
    
    def batch_get_issues(self, issue_keys: List[str]) -> Dict[str, Optional[Issue]]:
        """Get multiple issues in batches with parallel processing.
        
        Args:
            issue_keys: List of Jira issue keys
            
        Returns:
            Dictionary mapping issue keys to Issue objects (None if not found)
        """
        if not issue_keys:
            return {}
        
        logger.info(f"Batch fetching {len(issue_keys)} issues using {self.parallel_workers} workers")
        
        results = {}
        
        # Process in parallel batches
        with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
            # Split into batches
            batches = [
                issue_keys[i:i + self.batch_size]
                for i in range(0, len(issue_keys), self.batch_size)
            ]
            
            # Submit all batches
            future_to_batch = {
                executor.submit(self._fetch_issues_batch, batch): batch
                for batch in batches
            }
            
            # Collect results
            for future in as_completed(future_to_batch):
                batch = future_to_batch[future]
                try:
                    batch_results = future.result()
                    results.update(batch_results)
                except Exception as e:
                    logger.error(f"Failed to fetch batch {batch[:3]}...: {e}")
                    # Mark failed issues as None
                    for key in batch:
                        results[key] = None
        
        logger.info(f"Successfully fetched {sum(1 for v in results.values() if v)} of {len(issue_keys)} issues")
        return results

    @rate_limited()
    def _fetch_issues_batch(self, issue_keys: List[str]) -> Dict[str, Optional[Issue]]:
        """Fetch a batch of issues using JQL."""
        if not issue_keys:
            return {}
        
        # Build JQL query for batch
        jql = f"key in ({','.join(issue_keys)})"
        
        try:
            issues = self.jira.search_issues(
                jql,
                maxResults=len(issue_keys),
                expand='changelog'
            )
            
            # Map results back to keys
            result = {key: None for key in issue_keys}  # Default to None
            for issue in issues:
                result[issue.key] = issue
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to fetch batch with JQL '{jql}': {e}")
            return {key: None for key in issue_keys}

    def batch_get_work_logs(self, issue_keys: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Get work logs for multiple issues in parallel batches.
        
        Args:
            issue_keys: List of Jira issue keys
            
        Returns:
            Dictionary mapping issue keys to their work logs
        """
        if not issue_keys:
            return {}
        
        logger.info(f"Batch fetching work logs for {len(issue_keys)} issues")
        
        results = {}
        
        with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
            # Submit individual work log requests (they don't batch well)
            future_to_key = {
                executor.submit(self._get_work_logs_for_issue_safe, key): key
                for key in issue_keys
            }
            
            # Collect results
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    work_logs = future.result()
                    results[key] = work_logs
                except Exception as e:
                    logger.error(f"Failed to fetch work logs for {key}: {e}")
                    results[key] = []
        
        total_logs = sum(len(logs) for logs in results.values())
        logger.info(f"Fetched {total_logs} work logs across {len(issue_keys)} issues")
        return results

    @rate_limited()
    @cached(ttl=900)  # Cache for 15 minutes
    def _get_work_logs_for_issue_safe(self, issue_key: str) -> List[Dict[str, Any]]:
        """Get work logs for a single issue with error handling."""
        try:
            return self.get_work_logs_for_issue(issue_key)
        except Exception as e:
            logger.error(f"Failed to get work logs for {issue_key}: {e}")
            return []

    # ===== STREAMING OPERATIONS =====
    
    def iter_project_issues_optimized(self, project_key: str, 
                                    jql_filter: Optional[str] = None,
                                    fields: Optional[str] = None,
                                    expand: Optional[str] = None) -> Iterator[Issue]:
        """Memory-efficient streaming of project issues with optimizations.
        
        Args:
            project_key: Jira project key
            jql_filter: Additional JQL filter
            fields: Fields to retrieve
            expand: Expand parameter
            
        Yields:
            Individual Issue objects
        """
        # Build JQL
        base_jql = f'project = "{project_key}"'
        if jql_filter:
            base_jql += f' AND ({jql_filter})'
        base_jql += ' ORDER BY created ASC'
        
        # Create streaming paginator
        paginator = StreamingPaginator(
            fetch_func=self._fetch_issues_page_optimized,
            page_size=self.batch_size
        )
        
        # Stream issues
        for issue in paginator.iter_items(
            jql=base_jql,
            fields=fields,
            expand=expand
        ):
            yield issue

    @rate_limited()
    @cached(ttl=300)  # Cache pages for 5 minutes
    def _fetch_issues_page_optimized(self, start_at: int, max_results: int, 
                                   jql: str, fields: Optional[str] = None,
                                   expand: Optional[str] = None) -> List[Issue]:
        """Fetch a page of issues with caching and rate limiting."""
        try:
            return self.jira.search_issues(
                jql,
                startAt=start_at,
                maxResults=max_results,
                fields=fields,
                expand=expand,
                json_result=False
            )
        except Exception as e:
            logger.error(f"Failed to fetch issues page at {start_at}: {e}")
            return []

    # ===== CACHED OPERATIONS =====
    
    @cached(ttl=3600)  # Cache for 1 hour
    def get_projects_cached(self) -> List[Dict[str, Any]]:
        """Get all projects with caching."""
        try:
            projects = self.jira.projects()
            return [
                {
                    "id": proj.id,
                    "key": proj.key,
                    "name": proj.name,
                    "description": getattr(proj, 'description', ''),
                    "lead": getattr(proj.lead, 'name', None) if hasattr(proj, 'lead') else None
                }
                for proj in projects
            ]
        except Exception as e:
            logger.error(f"Failed to get projects: {e}")
            return []

    @cached(ttl=1800)  # Cache for 30 minutes
    def get_issue_types_cached(self, project_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get issue types with caching."""
        try:
            if project_key:
                project = self.jira.project(project_key)
                issue_types = project.issueTypes
            else:
                issue_types = self.jira.issue_types()
            
            return [
                {
                    "id": it.id,
                    "name": it.name,
                    "description": getattr(it, 'description', ''),
                    "subtask": getattr(it, 'subtask', False)
                }
                for it in issue_types
            ]
        except Exception as e:
            logger.error(f"Failed to get issue types for {project_key}: {e}")
            return []

    @cached(ttl=3600)  # Cache for 1 hour  
    def get_users_cached(self, project_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get users with caching."""
        try:
            if project_key:
                # Get users assigned to issues in the project
                jql = f'project = "{project_key}" AND assignee is not EMPTY'
                issues = self.jira.search_issues(jql, fields='assignee', maxResults=1000)
                
                # Extract unique users
                users_dict = {}
                for issue in issues:
                    if hasattr(issue.fields, 'assignee') and issue.fields.assignee:
                        user = issue.fields.assignee
                        users_dict[user.name] = {
                            "accountId": getattr(user, 'accountId', user.name),
                            "name": user.name,
                            "displayName": user.displayName,
                            "emailAddress": getattr(user, 'emailAddress', ''),
                            "active": getattr(user, 'active', True)
                        }
                
                return list(users_dict.values())
            else:
                # This is expensive, so we limit it
                users = self.jira.search_users('', maxResults=500)
                return [
                    {
                        "accountId": getattr(user, 'accountId', user.name),
                        "name": user.name,
                        "displayName": user.displayName,
                        "emailAddress": getattr(user, 'emailAddress', ''),
                        "active": getattr(user, 'active', True)
                    }
                    for user in users
                ]
        except Exception as e:
            logger.error(f"Failed to get users for {project_key}: {e}")
            return []

    # ===== PARALLEL BULK OPERATIONS =====
    
    def bulk_get_issue_metadata(self, issue_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get comprehensive metadata for multiple issues in parallel.
        
        Args:
            issue_keys: List of issue keys
            
        Returns:
            Dictionary mapping issue keys to their metadata
        """
        logger.info(f"Bulk fetching metadata for {len(issue_keys)} issues")
        
        results = {}
        
        with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
            # Submit all metadata requests
            future_to_key = {
                executor.submit(self._get_issue_metadata_safe, key): key
                for key in issue_keys
            }
            
            # Collect results
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    metadata = future.result()
                    results[key] = metadata
                except Exception as e:
                    logger.error(f"Failed to get metadata for {key}: {e}")
                    results[key] = {"error": str(e)}
        
        return results

    @rate_limited()
    def _get_issue_metadata_safe(self, issue_key: str) -> Dict[str, Any]:
        """Get comprehensive metadata for a single issue safely."""
        try:
            issue = self.jira.issue(issue_key, expand='changelog,attachments,worklog')
            
            return {
                "key": issue.key,
                "id": issue.id,
                "summary": issue.fields.summary,
                "description": getattr(issue.fields, 'description', ''),
                "status": issue.fields.status.name,
                "priority": getattr(issue.fields.priority, 'name', None),
                "assignee": getattr(issue.fields.assignee, 'name', None) if issue.fields.assignee else None,
                "creator": getattr(issue.fields.creator, 'name', None) if issue.fields.creator else None,
                "created": str(issue.fields.created),
                "updated": str(issue.fields.updated),
                "attachments_count": len(getattr(issue.fields, 'attachment', [])),
                "worklogs_count": len(getattr(issue.fields, 'worklog', {}).get('worklogs', [])),
                "changelog_count": len(getattr(issue, 'changelog', {}).get('histories', []))
            }
        except Exception as e:
            logger.error(f"Failed to get metadata for {issue_key}: {e}")
            return {"error": str(e)}

    # ===== PERFORMANCE MONITORING =====
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics for this client."""
        stats = self.performance_optimizer.get_comprehensive_stats()
        stats["client_type"] = "enhanced_jira_client"
        return stats

    def reset_performance_stats(self) -> None:
        """Reset performance statistics."""
        self.performance_optimizer.cache.clear()
        self.performance_optimizer._stats = {
            "operations_cached": 0,
            "operations_batched": 0,
            "connections_reused": 0,
            "rate_limited_calls": 0
        }

    def shutdown(self) -> None:
        """Shutdown the enhanced client and cleanup resources."""
        self.performance_optimizer.shutdown()
        if hasattr(super(), 'shutdown'):
            super().shutdown()

    # ===== BACKWARDS COMPATIBILITY =====
    
    def get_work_logs_for_issue(self, issue_key: str) -> List[Dict[str, Any]]:
        """Override with caching for backwards compatibility."""
        return self._get_work_logs_for_issue_safe(issue_key)

    def get_projects(self) -> List[Dict[str, Any]]:
        """Override with caching for backwards compatibility.""" 
        return self.get_projects_cached()

    def get_all_issues_for_project(self, project_key: str) -> List[Issue]:
        """Override with streaming for backwards compatibility."""
        return list(self.iter_project_issues_optimized(project_key)) 