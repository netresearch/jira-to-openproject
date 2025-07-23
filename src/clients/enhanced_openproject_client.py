#!/usr/bin/env python3
"""Enhanced OpenProject Client with Performance Optimizations.

This enhanced client provides:
1. Batch API operations for bulk work package creation
2. Response caching with TTL
3. Connection pooling and session reuse
4. Parallel processing for independent requests
5. Adaptive rate limiting
6. Bulk Rails operations optimization
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union
from urllib.parse import urljoin

from src.clients.openproject_client import OpenProjectClient
from src.utils.performance_optimizer import (
    PerformanceOptimizer,
    StreamingPaginator,
    cached,
    rate_limited,
    batched
)
from src.utils.config_validation import SecurityValidator, ConfigurationValidationError


logger = logging.getLogger(__name__)


class EnhancedOpenProjectClient(OpenProjectClient):
    """Enhanced OpenProject Client with performance optimizations."""
    
    def __init__(self, server: str, username: str, password: str, **kwargs):
        super().__init__(server, username, password, **kwargs)
        
        # Validate performance configuration parameters using SecurityValidator
        try:
            cache_size = SecurityValidator.validate_numeric_parameter('cache_size', kwargs.get('cache_size', 1500))
            cache_ttl = SecurityValidator.validate_numeric_parameter('cache_ttl', kwargs.get('cache_ttl', 2400))
            batch_size = SecurityValidator.validate_numeric_parameter('batch_size', kwargs.get('batch_size', 50))
            max_workers = SecurityValidator.validate_numeric_parameter('max_workers', kwargs.get('max_workers', 12))
            rate_limit = SecurityValidator.validate_numeric_parameter('rate_limit_per_sec', kwargs.get('rate_limit', 12.0))
            
            # Validate resource allocation to prevent system overload
            SecurityValidator.validate_resource_allocation(batch_size, max_workers, 2048)  # 2GB memory limit for enhanced client
            
        except ConfigurationValidationError as e:
            logger.error(f"EnhancedOpenProjectClient configuration validation failed: {e}")
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

    # ===== BATCH WORK PACKAGE OPERATIONS =====
    
    def batch_create_work_packages(self, work_packages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create multiple work packages using optimized batch processing.
        
        Args:
            work_packages: List of work package data dictionaries
            
        Returns:
            Dictionary with creation results and statistics
        """
        if not work_packages:
            return {"created": [], "errors": [], "stats": {"total": 0, "created": 0, "failed": 0}}
        
        logger.info(f"Batch creating {len(work_packages)} work packages using optimized Rails script")
        
        # Use optimized Ruby script with better error handling and performance
        temp_file = self._create_temp_work_packages_file(work_packages)
        
        try:
            result = self._execute_optimized_batch_creation(temp_file)
            
            # Cleanup temp file
            if temp_file.exists():
                temp_file.unlink()
            
            return result
            
        except Exception as e:
            logger.error(f"Batch work package creation failed: {e}")
            if temp_file.exists():
                temp_file.unlink()
            raise

    def _create_temp_work_packages_file(self, work_packages: List[Dict[str, Any]]) -> Path:
        """Create temporary JSON file for batch processing."""
        temp_file = Path(f"/tmp/batch_work_packages_{int(time.time())}.json")
        
        # Optimize data structure for Rails processing
        optimized_data = []
        for wp in work_packages:
            # Clean up data for faster Rails processing
            clean_wp = {k: v for k, v in wp.items() if v is not None}
            optimized_data.append(clean_wp)
        
        with temp_file.open("w") as f:
            json.dump(optimized_data, f, separators=(',', ':'))  # Compact JSON
        
        return temp_file

    @rate_limited()
    def _execute_optimized_batch_creation(self, temp_file: Path) -> Dict[str, Any]:
        """Execute optimized Rails script for batch work package creation."""
        
        # Optimized Ruby script with better performance
        optimized_script = f"""
        require 'json'
        require 'benchmark'
        
        # Performance monitoring
        start_time = Time.now
        
        # Load data efficiently
        wp_data = JSON.parse(File.read('{temp_file}'))
        puts "Loaded #{wp_data.size} work packages in #{Time.now - start_time}s"
        
        # Prepare results
        created_packages = []
        errors = []
        
        # Cache frequently accessed objects
        default_priority = IssuePriority.default
        default_status = Status.default
        admin_user = User.where(admin: true).first
        
        # Custom field cache for better performance
        custom_field_cache = {{}}
        updated_custom_fields = Set.new
        
        # Optimized custom field update function
        def update_custom_field_values(field_name, new_value, cache, updated_set)
          return false if new_value.blank? || updated_set.include?("#{field_name}:#{new_value}")
          
          cache[field_name] ||= CustomField.find_by(name: field_name)
          custom_field = cache[field_name]
          return false unless custom_field&.field_format == "list"
          
          current_values = custom_field.possible_values || []
          return true if current_values.include?(new_value)
          
          custom_field.possible_values = current_values + [new_value]
          if custom_field.save
            updated_set.add("#{field_name}:#{new_value}")
            puts "Updated custom field '#{field_name}' with value: '#{new_value}'"
            true
          else
            false
          end
        end
        
        # Process work packages with optimizations
        ActiveRecord::Base.transaction do
          wp_data.each_with_index do |wp_attrs, index|
            begin
              # Progress logging
              puts "Processing work package #{index + 1}/#{wp_data.size}" if (index + 1) % 50 == 0
              
              # Store Jira data for mapping
              jira_id = wp_attrs.delete('jira_id')
              jira_key = wp_attrs.delete('jira_key')
              
              # Validate and clean watcher IDs
              if wp_attrs['watcher_ids'].is_a?(Array)
                wp_attrs['watcher_ids'] = wp_attrs['watcher_ids'].compact.select do |watcher_id|
                  User.exists?(id: watcher_id)
                end
              end
              
              # Create work package with cached defaults
              wp = WorkPackage.new(wp_attrs)
              wp.priority = default_priority unless wp.priority_id
              wp.author = admin_user unless wp.author_id
              wp.status = default_status unless wp.status_id
              
              # Handle custom field validation errors with optimized updates
              retry_count = 0
              max_retries = 2
              
              while retry_count <= max_retries
                if wp.save
                  created_packages << {{
                    'jira_id' => jira_id,
                    'jira_key' => jira_key,
                    'openproject_id' => wp.id,
                    'subject' => wp.subject
                  }}
                  break
                else
                  retry_count += 1
                  
                  if retry_count <= max_retries
                    # Handle custom field errors
                    cf_errors = wp.errors.full_messages.select {{ |msg| msg.include?('not set to one of the allowed values') }}
                    
                    cf_errors.each do |error|
                      if match = error.match(/^(.*?) is not set to one of the allowed values/)
                        field_name = match[1]
                        if cf = CustomField.find_by(name: field_name)
                          value = wp.custom_value_for(cf)&.value
                          update_custom_field_values(field_name, value, custom_field_cache, updated_custom_fields)
                        end
                      end
                    end
                    
                    # Recreate work package for retry
                    wp = WorkPackage.new(wp_attrs)
                    wp.priority = default_priority unless wp.priority_id
                    wp.author = admin_user unless wp.author_id
                    wp.status = default_status unless wp.status_id
                  else
                    # Max retries reached
                    errors << {{
                      'jira_id' => jira_id,
                      'jira_key' => jira_key,
                      'subject' => wp_attrs['subject'],
                      'errors' => wp.errors.full_messages,
                      'error_type' => 'validation_error'
                    }}
                    break
                  end
                end
              end
              
            rescue => e
              errors << {{
                'jira_id' => wp_attrs['jira_id'],
                'jira_key' => wp_attrs['jira_key'],
                'subject' => wp_attrs['subject'],
                'errors' => [e.message],
                'error_type' => 'exception'
              }}
            end
          end
        end
        
        # Performance summary
        total_time = Time.now - start_time
        puts "Batch processing completed in #{total_time}s"
        puts "Created: #{created_packages.size}, Errors: #{errors.size}"
        
        # Return comprehensive result
        {{
          'status' => 'success',
          'created' => created_packages,
          'errors' => errors,
          'stats' => {{
            'total' => wp_data.size,
            'created' => created_packages.size,
            'failed' => errors.size,
            'processing_time' => total_time,
            'updated_custom_fields' => updated_custom_fields.size
          }}
        }}
        """
        
        # Execute with timeout
        result = self.rails_client.execute_command(optimized_script, timeout=600)
        
        if result.get("status") == "success":
            return result.get("output", {})
        else:
            raise Exception(f"Rails execution failed: {result.get('error', 'Unknown error')}")

    # ===== BATCH TIME ENTRY OPERATIONS =====
    
    def batch_create_time_entries(self, time_entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create multiple time entries using batch processing.
        
        Args:
            time_entries: List of time entry data dictionaries
            
        Returns:
            Dictionary with creation results
        """
        if not time_entries:
            return {"created": [], "errors": [], "stats": {"total": 0, "created": 0, "failed": 0}}
        
        logger.info(f"Batch creating {len(time_entries)} time entries")
        
        results = {"created": [], "errors": [], "stats": {"total": len(time_entries), "created": 0, "failed": 0}}
        
        # Process in parallel batches
        with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
            # Split into batches
            batches = [
                time_entries[i:i + self.batch_size]
                for i in range(0, len(time_entries), self.batch_size)
            ]
            
            # Submit all batches
            future_to_batch = {
                executor.submit(self._create_time_entries_batch, batch): batch
                for batch in batches
            }
            
            # Collect results
            for future in as_completed(future_to_batch):
                batch = future_to_batch[future]
                try:
                    batch_result = future.result()
                    results["created"].extend(batch_result.get("created", []))
                    results["errors"].extend(batch_result.get("errors", []))
                except Exception as e:
                    logger.error(f"Time entry batch failed: {e}")
                    # Mark entire batch as failed
                    for entry in batch:
                        results["errors"].append({
                            "entry": entry,
                            "error": str(e),
                            "error_type": "batch_failure"
                        })
        
        results["stats"]["created"] = len(results["created"])
        results["stats"]["failed"] = len(results["errors"])
        
        return results

    @rate_limited()
    def _create_time_entries_batch(self, entries_batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create a batch of time entries."""
        created = []
        errors = []
        
        for entry in entries_batch:
            try:
                created_entry = self.create_time_entry(entry)
                if created_entry and created_entry.get("id"):
                    created.append(created_entry)
                else:
                    errors.append({
                        "entry": entry,
                        "error": "No ID returned",
                        "error_type": "creation_failure"
                    })
            except Exception as e:
                errors.append({
                    "entry": entry,
                    "error": str(e),
                    "error_type": "exception"
                })
        
        return {"created": created, "errors": errors}

    # ===== CACHED OPERATIONS =====
    
    @cached(ttl=3600)  # Cache for 1 hour
    def get_projects_cached(self) -> List[Dict[str, Any]]:
        """Get all projects with caching."""
        try:
            response = self.session.get(
                urljoin(self.server, "/api/v3/projects"),
                params={"pageSize": 1000}
            )
            response.raise_for_status()
            
            data = response.json()
            return data.get("_embedded", {}).get("elements", [])
        except Exception as e:
            logger.error(f"Failed to get projects: {e}")
            return []

    @cached(ttl=1800)  # Cache for 30 minutes
    def get_work_package_types_cached(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get work package types with caching."""
        try:
            if project_id:
                url = f"/api/v3/projects/{project_id}/types"
            else:
                url = "/api/v3/types"
            
            response = self.session.get(urljoin(self.server, url))
            response.raise_for_status()
            
            data = response.json()
            return data.get("_embedded", {}).get("elements", [])
        except Exception as e:
            logger.error(f"Failed to get work package types for project {project_id}: {e}")
            return []

    @cached(ttl=1800)  # Cache for 30 minutes
    def get_users_cached(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get users with caching."""
        try:
            if project_id:
                # Get users with memberships in the project
                url = f"/api/v3/projects/{project_id}/memberships"
                response = self.session.get(
                    urljoin(self.server, url),
                    params={"pageSize": 1000}
                )
                response.raise_for_status()
                
                data = response.json()
                users = []
                for membership in data.get("_embedded", {}).get("elements", []):
                    user_link = membership.get("_links", {}).get("user", {}).get("href")
                    if user_link:
                        user_id = user_link.split("/")[-1]
                        user_response = self.session.get(urljoin(self.server, f"/api/v3/users/{user_id}"))
                        if user_response.status_code == 200:
                            users.append(user_response.json())
                
                return users
            else:
                response = self.session.get(
                    urljoin(self.server, "/api/v3/users"),
                    params={"pageSize": 1000}
                )
                response.raise_for_status()
                
                data = response.json()
                return data.get("_embedded", {}).get("elements", [])
        except Exception as e:
            logger.error(f"Failed to get users for project {project_id}: {e}")
            return []

    @cached(ttl=3600)  # Cache for 1 hour
    def get_statuses_cached(self) -> List[Dict[str, Any]]:
        """Get work package statuses with caching."""
        try:
            response = self.session.get(urljoin(self.server, "/api/v3/statuses"))
            response.raise_for_status()
            
            data = response.json()
            return data.get("_embedded", {}).get("elements", [])
        except Exception as e:
            logger.error(f"Failed to get statuses: {e}")
            return []

    @cached(ttl=3600)  # Cache for 1 hour  
    def get_priorities_cached(self) -> List[Dict[str, Any]]:
        """Get work package priorities with caching."""
        try:
            response = self.session.get(urljoin(self.server, "/api/v3/priorities"))
            response.raise_for_status()
            
            data = response.json()
            return data.get("_embedded", {}).get("elements", [])
        except Exception as e:
            logger.error(f"Failed to get priorities: {e}")
            return []

    # ===== PARALLEL BULK OPERATIONS =====
    
    def bulk_get_work_packages(self, work_package_ids: List[int]) -> Dict[int, Optional[Dict[str, Any]]]:
        """Get multiple work packages in parallel.
        
        Args:
            work_package_ids: List of work package IDs
            
        Returns:
            Dictionary mapping IDs to work package data (None if not found)
        """
        if not work_package_ids:
            return {}
        
        logger.info(f"Bulk fetching {len(work_package_ids)} work packages")
        
        results = {}
        
        with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
            # Submit all requests
            future_to_id = {
                executor.submit(self._get_work_package_safe, wp_id): wp_id
                for wp_id in work_package_ids
            }
            
            # Collect results
            for future in as_completed(future_to_id):
                wp_id = future_to_id[future]
                try:
                    work_package = future.result()
                    results[wp_id] = work_package
                except Exception as e:
                    logger.error(f"Failed to get work package {wp_id}: {e}")
                    results[wp_id] = None
        
        return results

    @rate_limited()
    def _get_work_package_safe(self, work_package_id: int) -> Optional[Dict[str, Any]]:
        """Get a single work package with error handling."""
        try:
            response = self.session.get(
                urljoin(self.server, f"/api/v3/work_packages/{work_package_id}")
            )
            if response.status_code == 200:
                return response.json()
            else:
                return None
        except Exception as e:
            logger.error(f"Failed to get work package {work_package_id}: {e}")
            return None

    # ===== PERFORMANCE MONITORING =====
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics for this client."""
        stats = self.performance_optimizer.get_comprehensive_stats()
        stats["client_type"] = "enhanced_openproject_client"
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
    
    def get_projects(self) -> List[Dict[str, Any]]:
        """Override with caching for backwards compatibility."""
        return self.get_projects_cached()

    def get_users(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Override with caching for backwards compatibility."""
        return self.get_users_cached(project_id) 