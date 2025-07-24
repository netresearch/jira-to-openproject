#!/usr/bin/env python3
"""Enhanced Audit Trail Migration for comprehensive history and activity stream preservation.

This module provides advanced audit trail migration capabilities that:
1. Extracts complete change history and activity streams from Jira issues
2. Transforms changelog entries to OpenProject audit format
3. Uses Rails console integration for inserting audit events that cannot be created via API
4. Preserves proper user attribution and timestamps
5. Handles orphaned or partial events gracefully
6. Provides comprehensive validation and reporting
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from src.display import configure_logging
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient

# Get logger from config
logger = configure_logging("INFO", None)


class AuditEventData(TypedDict):
    """Type hint for audit event data structure."""
    
    jira_issue_key: str
    jira_changelog_id: str
    openproject_work_package_id: int
    user_id: Optional[int]
    user_name: Optional[str]
    created_at: str
    action: str
    auditable_type: str
    auditable_id: int
    version: int
    comment: Optional[str]
    changes: Dict[str, Any]


class EnhancedAuditTrailMigrator:
    """Enhanced audit trail migrator with comprehensive changelog migration capabilities."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        """Initialize the Enhanced Audit Trail Migrator.
        
        Args:
            jira_client: JiraClient instance for accessing Jira data
            op_client: OpenProjectClient instance for creating audit events
        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.logger = logger
        
        # Data storage
        self.changelog_data: Dict[str, List[Any]] = {}
        self.audit_events: List[AuditEventData] = []
        self.user_mapping: Dict[str, int] = {}
        
        # Migration results
        self.migration_results = {
            "total_changelog_entries": 0,
            "processed_entries": 0,
            "successful_migrations": 0,
            "failed_migrations": 0,
            "skipped_entries": 0,
            "orphaned_events": 0,
            "user_attribution_failures": 0,
            "errors": [],
            "warnings": []
        }
        
        # Rails operations queue
        self.rails_operations: List[Dict[str, Any]] = []
        
        # Load user mapping if available
        self._load_user_mapping()
        
    def _load_user_mapping(self) -> None:
        """Load user mapping from previous migration results."""
        try:
            from src.utils import data_handler
            
            user_mapping_file = Path("data") / "user_mapping.json"
            if user_mapping_file.exists():
                self.user_mapping = data_handler.load(
                    filename="user_mapping.json",
                    directory=Path("data")
                )
                self.logger.info(f"Loaded {len(self.user_mapping)} user mappings")
            else:
                self.logger.warning("User mapping file not found - will use fallback user attribution")
                
        except Exception as e:
            self.logger.warning(f"Failed to load user mapping: {e}")
            
    def extract_changelog_from_issue(self, jira_issue: Any) -> List[Dict[str, Any]]:
        """Extract changelog data from a Jira issue.
        
        Args:
            jira_issue: Jira issue object with changelog expanded
            
        Returns:
            List of changelog entries with normalized structure
        """
        changelog_entries = []
        
        try:
            if not hasattr(jira_issue, 'changelog') or not jira_issue.changelog:
                self.logger.debug(f"No changelog found for issue {jira_issue.key}")
                return changelog_entries
                
            self.logger.debug(f"Processing {len(jira_issue.changelog.histories)} changelog entries for {jira_issue.key}")
            
            for history in jira_issue.changelog.histories:
                # Extract basic history information
                entry = {
                    "id": history.id,
                    "created": history.created,
                    "author": {
                        "name": getattr(history.author, 'name', None) if history.author else None,
                        "displayName": getattr(history.author, 'displayName', None) if history.author else None,
                        "emailAddress": getattr(history.author, 'emailAddress', None) if history.author else None
                    },
                    "items": []
                }
                
                # Process individual changes within this history entry
                for item in history.items:
                    change_item = {
                        "field": item.field,
                        "fieldtype": getattr(item, 'fieldtype', None),
                        "fieldId": getattr(item, 'fieldId', None),
                        "from": getattr(item, 'fromString', None),
                        "fromString": getattr(item, 'fromString', None),
                        "to": getattr(item, 'toString', None),
                        "toString": getattr(item, 'toString', None)
                    }
                    entry["items"].append(change_item)
                
                changelog_entries.append(entry)
                
        except Exception as e:
            self.logger.error(f"Error extracting changelog from issue {jira_issue.key}: {e}")
            self.migration_results["errors"].append(f"Changelog extraction failed for {jira_issue.key}: {e}")
            
        return changelog_entries
        
    def transform_changelog_to_audit_events(
        self, 
        changelog_entries: List[Dict[str, Any]], 
        jira_issue_key: str,
        openproject_work_package_id: int
    ) -> List[AuditEventData]:
        """Transform Jira changelog entries to OpenProject audit events.
        
        Args:
            changelog_entries: List of Jira changelog entries
            jira_issue_key: Jira issue key for reference
            openproject_work_package_id: OpenProject work package ID
            
        Returns:
            List of audit event data structures
        """
        audit_events = []
        
        for entry in changelog_entries:
            try:
                # Map user
                user_id = None
                user_name = None
                if entry.get("author") and entry["author"].get("name"):
                    user_name = entry["author"]["name"]
                    user_id = self.user_mapping.get(user_name)
                    
                if not user_id:
                    self.migration_results["user_attribution_failures"] += 1
                    self.migration_results["warnings"].append(
                        f"No user mapping found for {user_name} in issue {jira_issue_key}"
                    )
                    # Use system user as fallback
                    user_id = 1  # Assuming admin user ID is 1
                    
                # Process each change item in the entry
                for item in entry.get("items", []):
                    # Map field changes to OpenProject audit format
                    changes = self._map_field_changes(item)
                    
                    if changes:
                        audit_event: AuditEventData = {
                            "jira_issue_key": jira_issue_key,
                            "jira_changelog_id": entry["id"],
                            "openproject_work_package_id": openproject_work_package_id,
                            "user_id": user_id,
                            "user_name": user_name,
                            "created_at": entry["created"],
                            "action": "update",
                            "auditable_type": "WorkPackage",
                            "auditable_id": openproject_work_package_id,
                            "version": 1,  # Will be calculated properly during insertion
                            "comment": self._generate_audit_comment(item),
                            "changes": changes
                        }
                        audit_events.append(audit_event)
                        
            except Exception as e:
                self.logger.error(f"Error transforming changelog entry {entry.get('id', 'unknown')} for {jira_issue_key}: {e}")
                self.migration_results["errors"].append(f"Transformation failed for changelog {entry.get('id')} in {jira_issue_key}: {e}")
                
        return audit_events
        
    def _map_field_changes(self, change_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Map Jira field changes to OpenProject audit format.
        
        Args:
            change_item: Individual change item from Jira changelog
            
        Returns:
            Mapped changes in OpenProject format or None if not mappable
        """
        field = change_item.get("field")
        if not field:
            return None
            
        # Field mapping from Jira to OpenProject
        field_mappings = {
            "summary": "subject",
            "description": "description", 
            "status": "status_id",
            "assignee": "assigned_to_id",
            "priority": "priority_id",
            "issuetype": "type_id",
            "resolution": "resolution",
            "labels": "tags",
            "fixVersion": "version",
            "component": "category",
            "reporter": "author_id"
        }
        
        op_field = field_mappings.get(field, field)
        
        # Handle different field types
        from_value = change_item.get("from") or change_item.get("fromString")
        to_value = change_item.get("to") or change_item.get("toString")
        
        # Special handling for certain fields
        if field in ["assignee", "reporter"]:
            # Map user names to IDs
            from_id = self.user_mapping.get(from_value) if from_value else None
            to_id = self.user_mapping.get(to_value) if to_value else None
            return {
                op_field: [from_id, to_id]
            }
        elif field in ["status", "priority", "issuetype"]:
            # These need ID mapping which we'll handle during insertion
            return {
                op_field: [from_value, to_value]
            }
        else:
            # Generic field change
            return {
                op_field: [from_value, to_value]
            }
            
    def _generate_audit_comment(self, change_item: Dict[str, Any]) -> str:
        """Generate a descriptive comment for the audit event.
        
        Args:
            change_item: Individual change item from Jira changelog
            
        Returns:
            Human-readable comment describing the change
        """
        field = change_item.get("field", "unknown field")
        from_value = change_item.get("fromString") or change_item.get("from") or "empty"
        to_value = change_item.get("toString") or change_item.get("to") or "empty"
        
        return f"Changed {field} from '{from_value}' to '{to_value}'"
        
    def migrate_audit_trail_for_issue(
        self, 
        jira_issue: Any, 
        openproject_work_package_id: int
    ) -> Dict[str, Any]:
        """Migrate complete audit trail for a single issue.
        
        Args:
            jira_issue: Jira issue object with changelog expanded
            openproject_work_package_id: OpenProject work package ID
            
        Returns:
            Migration result for this issue
        """
        issue_key = jira_issue.key
        self.logger.debug(f"Migrating audit trail for issue {issue_key}")
        
        result = {
            "jira_issue_key": issue_key,
            "openproject_work_package_id": openproject_work_package_id,
            "changelog_entries_found": 0,
            "audit_events_created": 0,
            "errors": [],
            "warnings": []
        }
        
        try:
            # Extract changelog
            changelog_entries = self.extract_changelog_from_issue(jira_issue)
            result["changelog_entries_found"] = len(changelog_entries)
            self.migration_results["total_changelog_entries"] += len(changelog_entries)
            
            if not changelog_entries:
                result["warnings"].append("No changelog entries found")
                self.migration_results["skipped_entries"] += 1
                return result
                
            # Transform to audit events
            audit_events = self.transform_changelog_to_audit_events(
                changelog_entries, issue_key, openproject_work_package_id
            )
            
            # Queue Rails operations for batch execution
            for event in audit_events:
                self.rails_operations.append({
                    "operation": "create_audit_event",
                    "data": event
                })
                
            result["audit_events_created"] = len(audit_events)
            self.migration_results["processed_entries"] += len(changelog_entries)
            
        except Exception as e:
            error_msg = f"Failed to migrate audit trail for {issue_key}: {e}"
            self.logger.error(error_msg)
            result["errors"].append(error_msg)
            self.migration_results["errors"].append(error_msg)
            self.migration_results["failed_migrations"] += 1
            
        return result
        
    def process_stored_changelog_data(self, work_package_mapping: Dict[str, Any]) -> None:
        """Process all stored changelog data and create audit events.
        
        Args:
            work_package_mapping: Mapping of Jira IDs to OpenProject work packages
        """
        if not self.changelog_data:
            self.logger.info("No stored changelog data to process")
            return
            
        self.logger.info(f"Processing {len(self.changelog_data)} stored changelog entries")
        
        for jira_id, changelog_info in self.changelog_data.items():
            try:
                # Find the corresponding work package ID
                work_package_info = work_package_mapping.get(jira_id)
                if not work_package_info:
                    self.logger.warning(f"No work package mapping found for Jira ID {jira_id}")
                    self.migration_results["orphaned_events"] += 1
                    continue
                    
                openproject_work_package_id = work_package_info.get("openproject_id")
                if not openproject_work_package_id:
                    self.logger.warning(f"No OpenProject ID found for Jira ID {jira_id}")
                    self.migration_results["orphaned_events"] += 1
                    continue
                
                # Transform changelog entries to audit events
                audit_events = self.transform_changelog_to_audit_events(
                    changelog_info["changelog_entries"],
                    changelog_info["jira_issue_key"],
                    openproject_work_package_id
                )
                
                # Queue Rails operations for batch execution
                for event in audit_events:
                    self.rails_operations.append({
                        "operation": "create_audit_event",
                        "data": event
                    })
                    
                self.migration_results["processed_entries"] += len(changelog_info["changelog_entries"])
                
            except Exception as e:
                error_msg = f"Failed to process changelog data for Jira ID {jira_id}: {e}"
                self.logger.error(error_msg)
                self.migration_results["errors"].append(error_msg)
                self.migration_results["failed_migrations"] += 1

    def execute_rails_audit_operations(self, work_package_mapping: Dict[str, Any]) -> Dict[str, Any]:
        """Execute queued Rails operations for audit event creation.
        
        Args:
            work_package_mapping: Mapping of Jira IDs to OpenProject work packages
            
        Returns:
            Execution results
        """
        # First, process any stored changelog data
        self.process_stored_changelog_data(work_package_mapping)
        
        if not self.rails_operations:
            self.logger.info("No audit trail Rails operations to execute")
            return {"processed": 0, "errors": [], "warnings": []}
            
        self.logger.info(f"Executing {len(self.rails_operations)} audit trail Rails operations")
        
        try:
            # Create Rails script for batch audit event creation
            ruby_script = self._generate_audit_creation_script(self.rails_operations)
            
            # Execute via Rails console
            result = self.op_client.execute_query(ruby_script, timeout=300)
            
            if result.get("status") == "success":
                output = result.get("output", {})
                processed = output.get("processed", 0) if isinstance(output, dict) else 0
                errors = output.get("errors", []) if isinstance(output, dict) else []
                
                self.migration_results["successful_migrations"] += processed
                if errors:
                    self.migration_results["errors"].extend(errors)
                    
                self.logger.success(f"Successfully created {processed} audit events")
                return {"processed": processed, "errors": errors, "warnings": []}
            else:
                error_msg = result.get("error", "Unknown Rails execution error")
                self.logger.error(f"Rails audit operations failed: {error_msg}")
                self.migration_results["errors"].append(error_msg)
                return {"processed": 0, "errors": [error_msg], "warnings": []}
                
        except Exception as e:
            error_msg = f"Failed to execute audit Rails operations: {e}"
            self.logger.error(error_msg)
            self.migration_results["errors"].append(error_msg)
            return {"processed": 0, "errors": [error_msg], "warnings": []}
            
    def _generate_audit_creation_script(self, operations: List[Dict[str, Any]]) -> str:
        """Generate Ruby script for creating audit events.
        
        Args:
            operations: List of audit operations to execute
            
        Returns:
            Ruby script as string
        """
        # Create the audit events data as JSON
        audit_events_data = []
        for op in operations:
            if op.get("operation") == "create_audit_event":
                audit_events_data.append(op["data"])
                
        # Ruby script for audit event creation
        script = f"""
        require 'json'
        
        begin
          # Audit events data
          audit_events = {json.dumps(audit_events_data, indent=2)}
          
          created_count = 0
          errors = []
          
          audit_events.each do |event_data|
            begin
              # Find the work package
              wp = WorkPackage.find_by(id: event_data['openproject_work_package_id'])
              unless wp
                errors << "Work package #{{event_data['openproject_work_package_id']}} not found for {{event_data['jira_issue_key']}}"
                next
              end
              
              # Find or create user
              user_id = event_data['user_id'] || 1  # Fallback to admin
              user = User.find_by(id: user_id) || User.where(admin: true).first
              
              # Calculate next version number
              current_version = wp.journals.maximum(:version) || 0
              next_version = current_version + 1
              
              # Parse timestamp
              created_at = Time.parse(event_data['created_at'])
              
              # Create journal entry (OpenProject's audit system)
              journal = Journal.create!(
                journable: wp,
                user: user,
                version: next_version,
                activity_type: 'work_packages',
                created_at: created_at,
                notes: event_data['comment']
              )
              
              # Create journal details for field changes
              event_data['changes'].each do |field, values|
                next unless values.is_a?(Array) && values.length == 2
                
                old_value = values[0]
                new_value = values[1]
                
                JournalDetail.create!(
                  journal: journal,
                  property: 'attr',
                  prop_key: field,
                  old_value: old_value&.to_s,
                  value: new_value&.to_s
                )
              end
              
              created_count += 1
              puts "Created audit event for {{event_data['jira_issue_key']}} (WP #{{wp.id}})"
              
            rescue => e
              error_msg = "Failed to create audit event for {{event_data['jira_issue_key']}}: {{e.message}}"
              errors << error_msg
              puts error_msg
            end
          end
          
          result = {{
            'status' => 'success',
            'processed' => created_count,
            'errors' => errors,
            'total' => audit_events.length
          }}
          
          puts "Audit trail migration completed: {{created_count}}/{{audit_events.length}} events created"
          result
          
        rescue => e
          error_result = {{
            'status' => 'error',
            'message' => e.message,
            'processed' => 0,
            'errors' => [e.message]
          }}
          puts "Audit trail migration failed: {{e.message}}"
          error_result
        end
        """
        
        return script
        
    def generate_audit_trail_report(self) -> Dict[str, Any]:
        """Generate comprehensive audit trail migration report.
        
        Returns:
            Detailed report of audit trail migration results
        """
        total_entries = self.migration_results["total_changelog_entries"]
        successful = self.migration_results["successful_migrations"]
        failed = self.migration_results["failed_migrations"]
        
        success_rate = (successful / total_entries * 100) if total_entries > 0 else 0
        
        report = {
            "summary": {
                "total_changelog_entries": total_entries,
                "processed_entries": self.migration_results["processed_entries"],
                "successful_migrations": successful,
                "failed_migrations": failed,
                "success_rate": round(success_rate, 2),
                "skipped_entries": self.migration_results["skipped_entries"],
                "orphaned_events": self.migration_results["orphaned_events"],
                "user_attribution_failures": self.migration_results["user_attribution_failures"]
            },
            "details": {
                "errors": self.migration_results["errors"],
                "warnings": self.migration_results["warnings"],
                "rails_operations_count": len(self.rails_operations)
            },
            "recommendations": self._generate_recommendations()
        }
        
        return report
        
    def _generate_recommendations(self) -> List[str]:
        """Generate recommendations based on migration results."""
        recommendations = []
        
        if self.migration_results["user_attribution_failures"] > 0:
            recommendations.append(
                "Consider running user migration again to improve user attribution in audit trails"
            )
            
        if self.migration_results["orphaned_events"] > 0:
            recommendations.append(
                "Review orphaned audit events and manually assign to appropriate work packages"
            )
            
        if len(self.migration_results["errors"]) > 0:
            recommendations.append(
                "Review error logs and retry failed audit trail migrations"
            )
            
        success_rate = (
            self.migration_results["successful_migrations"] / 
            max(self.migration_results["total_changelog_entries"], 1) * 100
        )
        
        if success_rate < 90:
            recommendations.append(
                "Audit trail migration success rate is below 90% - consider investigating common failure patterns"
            )
            
        return recommendations
        
    def save_migration_results(self) -> None:
        """Save audit trail migration results to file."""
        try:
            from src.utils import data_handler
            
            report = self.generate_audit_trail_report()
            data_handler.save(
                data=report,
                filename="audit_trail_migration_report.json",
                directory=Path("data")
            )
            
            self.logger.info("Saved audit trail migration report to data/audit_trail_migration_report.json")
            
        except Exception as e:
            self.logger.error(f"Failed to save audit trail migration results: {e}") 