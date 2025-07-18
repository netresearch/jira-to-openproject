#!/usr/bin/env python3
"""Enhanced Timestamp Migration for comprehensive datetime metadata preservation.

This module provides advanced timestamp migration capabilities that:
1. Extracts all timestamp fields from Jira issues (created, updated, due, resolved, custom dates)
2. Handles timezone conversion and DST edge cases
3. Uses Rails console integration for setting immutable timestamp fields
4. Preserves time tracking information and estimates
5. Provides comprehensive validation and reporting
"""

import json
import re
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.utils.validators import validate_jira_key


class TimestampMapping(TypedDict):
    """Represents a mapping between Jira and OpenProject timestamp fields."""
    
    jira_field: str
    jira_value: str | None
    jira_timezone: str | None
    openproject_field: str
    openproject_value: str | None
    normalized_utc: str | None
    conversion_status: str  # 'success', 'warning', 'failed'
    notes: str | None


class TimestampMigrationResult(TypedDict):
    """Result of timestamp migration for a work package."""
    
    jira_key: str
    extracted_timestamps: dict[str, Any]
    migrated_timestamps: dict[str, Any]
    rails_operations: list[dict[str, Any]]
    warnings: list[str]
    errors: list[str]
    status: str  # 'success', 'partial', 'failed'


class EnhancedTimestampMigrator:
    """Enhanced timestamp migrator with comprehensive datetime preservation.
    
    This class provides advanced capabilities for migrating timestamp data
    from Jira to OpenProject with robust handling of:
    - All Jira timestamp fields (created, updated, due, resolved, custom dates)
    - Timezone conversion and DST edge cases
    - Rails console integration for immutable field setting
    - Time tracking and estimate preservation
    - Custom date field migration
    """

    # Jira timezone mappings (common timezone IDs)
    JIRA_TIMEZONE_MAPPINGS = {
        "UTC": "UTC",
        "GMT": "GMT", 
        "EST": "America/New_York",
        "PST": "America/Los_Angeles",
        "CST": "America/Chicago",
        "MST": "America/Denver",
        "CET": "Europe/Paris",
        "JST": "Asia/Tokyo",
        "AEST": "Australia/Sydney",
        "IST": "Asia/Kolkata",
    }

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        target_timezone: str = "UTC",
    ) -> None:
        """Initialize the enhanced timestamp migrator.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            target_timezone: Target timezone for normalized timestamps (default: UTC)
        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.logger = config.logger
        self.target_timezone = target_timezone
        
        # Rails operations cache for immutable field setting
        self._rails_operations_cache: list[dict[str, Any]] = []
        
        # Timestamp migration results
        self.migration_results: dict[str, TimestampMigrationResult] = {}
        
        # Load Jira timezone configuration
        self.jira_timezone = self._detect_jira_timezone()

    def _detect_jira_timezone(self) -> str:
        """Detect the timezone used by the Jira instance.
        
        Returns:
            str: Timezone identifier (e.g., "Europe/Berlin", "UTC")
        """
        # Default timezone from configuration
        default_timezone = config.jira_config.get("default_timezone", "UTC")
        
        try:
            # Use correct Jira library method to get server info
            if not self.jira_client.jira:
                self.logger.warning("Jira client not initialized, using default timezone: %s", default_timezone)
                return default_timezone
                
            server_info = self.jira_client.jira.server_info()
            
            if not server_info:
                self.logger.warning("Jira server_info() returned empty response, using default timezone: %s", default_timezone)
                return default_timezone
            
            # Extract timezone from server info
            if "serverTimeZone" in server_info:
                timezone_info = server_info["serverTimeZone"]
                
                # Handle different possible structures
                if isinstance(timezone_info, dict):
                    # Jira Cloud/Server format: {"timeZoneId": "Europe/Berlin", "displayName": "..."}
                    timezone_id = timezone_info.get("timeZoneId")
                    if timezone_id:
                        self.logger.info("Detected Jira timezone from server info: %s", timezone_id)
                        return self._normalize_timezone_id(timezone_id)
                        
                elif isinstance(timezone_info, str):
                    # Simple string format
                    self.logger.info("Detected Jira timezone from server info: %s", timezone_info)
                    return self._normalize_timezone_id(timezone_info)
            
            # Check alternative field names
            for field in ["timeZone", "timezone", "serverTz"]:
                if field in server_info:
                    tz_value = server_info[field]
                    if isinstance(tz_value, str) and tz_value:
                        self.logger.info("Detected Jira timezone from %s field: %s", field, tz_value)
                        return self._normalize_timezone_id(tz_value)
            
            # Log available fields for debugging
            self.logger.debug("Available server info fields: %s", list(server_info.keys()))
            self.logger.warning("No timezone information found in Jira server info, using default: %s", default_timezone)
            return default_timezone
            
        except Exception as e:
            self.logger.error("Failed to detect Jira timezone: %s", e)
            self.logger.warning("Falling back to configured default timezone: %s", default_timezone)
            return default_timezone

    def _normalize_timezone_id(self, timezone_id: str) -> str:
        """Normalize timezone ID to ensure compatibility.
        
        Args:
            timezone_id: Raw timezone identifier from Jira
            
        Returns:
            str: Normalized timezone identifier
        """
        if not timezone_id:
            return "UTC"
            
        # Handle common timezone abbreviations
        if timezone_id in self.JIRA_TIMEZONE_MAPPINGS:
            normalized = self.JIRA_TIMEZONE_MAPPINGS[timezone_id]
            self.logger.debug("Mapped timezone %s to %s", timezone_id, normalized)
            return normalized
        
        # Validate timezone exists in zoneinfo
        try:
            ZoneInfo(timezone_id)
            return timezone_id
        except Exception as e:
            self.logger.warning("Invalid timezone ID '%s': %s, falling back to UTC", timezone_id, e)
            return "UTC"

    def _validate_jira_key(self, jira_key: str) -> None:
        """Validate JIRA key format using the centralized validator.
        
        Args:
            jira_key: The JIRA key to validate
            
        Raises:
            ValueError: If jira_key format is invalid or contains potentially dangerous characters
        """
        validate_jira_key(jira_key)

    def migrate_timestamps(
        self,
        jira_issue: dict[str, Any],
        work_package_data: dict[str, Any],
        use_rails_for_immutable: bool = True,
    ) -> TimestampMigrationResult:
        """Migrate all timestamp data for a work package with enhanced handling.

        Args:
            jira_issue: Jira issue data with timestamp fields
            work_package_data: Work package data to enhance with timestamps
            use_rails_for_immutable: Whether to use Rails console for immutable fields

        Returns:
            TimestampMigrationResult with migration results and metadata
        """
        jira_key = getattr(jira_issue, 'key', work_package_data.get('jira_key', 'unknown'))
        
        result = TimestampMigrationResult(
            jira_key=jira_key,
            extracted_timestamps={},
            migrated_timestamps={},
            rails_operations=[],
            warnings=[],
            errors=[],
            status="success"
        )

        try:
            # Extract all timestamp fields from Jira issue
            extracted = self._extract_all_timestamps(jira_issue)
            result["extracted_timestamps"] = extracted

            # Migrate creation timestamp
            creation_result = self._migrate_creation_timestamp(
                extracted, work_package_data, use_rails_for_immutable
            )
            if creation_result["rails_operation"]:
                result["rails_operations"].append(creation_result["rails_operation"])
            if creation_result["warnings"]:
                result["warnings"].extend(creation_result["warnings"])

            # Migrate update timestamp
            update_result = self._migrate_update_timestamp(
                extracted, work_package_data, use_rails_for_immutable
            )
            if update_result["rails_operation"]:
                result["rails_operations"].append(update_result["rails_operation"])
            if update_result["warnings"]:
                result["warnings"].extend(update_result["warnings"])

            # Migrate due date
            due_result = self._migrate_due_date(extracted, work_package_data)
            if due_result["warnings"]:
                result["warnings"].extend(due_result["warnings"])

            # Migrate resolution/closed date
            resolution_result = self._migrate_resolution_date(
                extracted, work_package_data, use_rails_for_immutable
            )
            if resolution_result["rails_operation"]:
                result["rails_operations"].append(resolution_result["rails_operation"])
            if resolution_result["warnings"]:
                result["warnings"].extend(resolution_result["warnings"])

            # Migrate custom date fields
            custom_result = self._migrate_custom_date_fields(extracted, work_package_data)
            if custom_result["warnings"]:
                result["warnings"].extend(custom_result["warnings"])

            # Store migrated timestamps
            result["migrated_timestamps"] = {
                k: v for k, v in work_package_data.items() 
                if k.endswith(('_at', '_on', '_date')) or 'date' in k.lower()
            }

            # Update status based on warnings/errors
            if result["warnings"] and not result["errors"]:
                result["status"] = "partial"
            elif result["errors"]:
                result["status"] = "failed"

        except Exception as e:
            self.logger.error("Failed to migrate timestamps for %s: %s", jira_key, e)
            result["errors"].append(str(e))
            result["status"] = "failed"

        # Store result for reporting
        self.migration_results[jira_key] = result
        return result

    def _extract_all_timestamps(self, jira_issue: dict[str, Any]) -> dict[str, Any]:
        """Extract all timestamp fields from Jira issue."""
        timestamps = {}

        # Standard Jira timestamp fields
        standard_fields = {
            "created": "created_at",
            "updated": "updated_at",
            "duedate": "due_date",
            "resolutiondate": "resolution_date",
        }

        for jira_field, op_field in standard_fields.items():
            if hasattr(jira_issue.fields, jira_field):
                raw_value = getattr(jira_issue.fields, jira_field)
                if raw_value:
                    timestamps[op_field] = {
                        "raw_value": str(raw_value),
                        "jira_field": jira_field,
                        "openproject_field": op_field,
                        "normalized_utc": self._normalize_timestamp(raw_value),
                    }

        # Extract custom date fields
        custom_fields = getattr(jira_issue, 'raw', {}).get('fields', {})
        for field_name, field_value in custom_fields.items():
            if field_name.startswith('customfield_') and field_value:
                # Check if this looks like a date/datetime field
                if self._is_date_field(field_value):
                    timestamps[f"custom_{field_name}"] = {
                        "raw_value": str(field_value),
                        "jira_field": field_name,
                        "openproject_field": f"custom_{field_name}",
                        "normalized_utc": self._normalize_timestamp(field_value),
                    }

        # Extract time tracking information
        if hasattr(jira_issue.fields, 'timetracking'):
            timetracking = jira_issue.fields.timetracking
            if timetracking:
                if hasattr(timetracking, 'originalEstimate'):
                    timestamps["original_estimate"] = {
                        "raw_value": str(timetracking.originalEstimate),
                        "jira_field": "timetracking.originalEstimate",
                        "openproject_field": "estimated_hours",
                        "normalized_utc": None,  # Duration, not timestamp
                    }
                if hasattr(timetracking, 'timeSpent'):
                    timestamps["time_spent"] = {
                        "raw_value": str(timetracking.timeSpent),
                        "jira_field": "timetracking.timeSpent", 
                        "openproject_field": "spent_hours",
                        "normalized_utc": None,  # Duration, not timestamp
                    }

        return timestamps

    def _is_date_field(self, value: Any) -> bool:
        """Check if a field value looks like a date/datetime."""
        if not isinstance(value, str):
            return False
        
        # Common date patterns
        date_patterns = [
            r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}',  # ISO datetime
            r'^\d{4}-\d{2}-\d{2}$',  # ISO date
            r'^\d{2}/\d{2}/\d{4}$',  # MM/DD/YYYY
            r'^\d{2}-\d{2}-\d{4}$',  # MM-DD-YYYY
        ]
        
        return any(re.match(pattern, value) for pattern in date_patterns)

    def _normalize_timestamp(self, timestamp_str: str) -> str | None:
        """Normalize a timestamp string to UTC ISO format."""
        if not timestamp_str:
            return None

        try:
            # Parse the timestamp - handle various formats
            if isinstance(timestamp_str, datetime):
                dt = timestamp_str
            else:
                # Try to parse as ISO format first
                try:
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                except ValueError:
                    # Try other common formats
                    formats = [
                        '%Y-%m-%dT%H:%M:%S.%f%z',
                        '%Y-%m-%dT%H:%M:%S%z',
                        '%Y-%m-%d %H:%M:%S',
                        '%Y-%m-%d',
                    ]
                    dt = None
                    for fmt in formats:
                        try:
                            dt = datetime.strptime(timestamp_str, fmt)
                            break
                        except ValueError:
                            continue
                    
                    if dt is None:
                        raise ValueError(f"Could not parse timestamp: {timestamp_str}")

            # Ensure timezone aware
            if dt.tzinfo is None:
                # Assume Jira timezone if no timezone info
                jira_tz = ZoneInfo(self.jira_timezone)
                dt = dt.replace(tzinfo=jira_tz)

            # Convert to UTC
            utc_dt = dt.astimezone(UTC)
            return utc_dt.isoformat()

        except Exception as e:
            self.logger.warning("Failed to normalize timestamp '%s': %s", timestamp_str, e)
            return None

    def _migrate_creation_timestamp(
        self,
        extracted: dict[str, Any],
        work_package_data: dict[str, Any],
        use_rails: bool,
    ) -> dict[str, Any]:
        """Migrate creation timestamp with Rails console integration."""
        result = {"rails_operation": None, "warnings": []}

        if "created_at" not in extracted:
            result["warnings"].append("No creation timestamp found in Jira issue")
            return result

        created_data = extracted["created_at"]
        normalized_utc = created_data["normalized_utc"]

        if not normalized_utc:
            result["warnings"].append("Could not normalize creation timestamp")
            return result

        if use_rails:
            # Queue Rails operation for immutable field setting
            rails_op = {
                "type": "set_created_at",
                "jira_key": work_package_data.get("jira_key", "unknown"),
                "timestamp": normalized_utc,
                "original_value": created_data["raw_value"],
            }
            result["rails_operation"] = rails_op
        else:
            # Set via API (may not work for immutable fields)
            work_package_data["created_at"] = normalized_utc

        return result

    def _migrate_update_timestamp(
        self,
        extracted: dict[str, Any],
        work_package_data: dict[str, Any],
        use_rails: bool,
    ) -> dict[str, Any]:
        """Migrate update timestamp."""
        result = {"rails_operation": None, "warnings": []}

        if "updated_at" not in extracted:
            result["warnings"].append("No update timestamp found in Jira issue")
            return result

        updated_data = extracted["updated_at"]
        normalized_utc = updated_data["normalized_utc"]

        if not normalized_utc:
            result["warnings"].append("Could not normalize update timestamp")
            return result

        if use_rails:
            # Queue Rails operation for setting update timestamp
            rails_op = {
                "type": "set_updated_at",
                "jira_key": work_package_data.get("jira_key", "unknown"),
                "timestamp": normalized_utc,
                "original_value": updated_data["raw_value"],
            }
            result["rails_operation"] = rails_op
        else:
            # Set via API
            work_package_data["updated_at"] = normalized_utc

        return result

    def _migrate_due_date(
        self,
        extracted: dict[str, Any],
        work_package_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate due date (can be set via API)."""
        result = {"warnings": []}

        if "due_date" not in extracted:
            return result

        due_data = extracted["due_date"]
        normalized_utc = due_data["normalized_utc"]

        if normalized_utc:
            # Extract just the date part for due_date field
            due_date = normalized_utc.split('T')[0]
            work_package_data["due_date"] = due_date
        else:
            result["warnings"].append("Could not normalize due date")

        return result

    def _migrate_resolution_date(
        self,
        extracted: dict[str, Any],
        work_package_data: dict[str, Any],
        use_rails: bool,
    ) -> dict[str, Any]:
        """Migrate resolution/closed date."""
        result = {"rails_operation": None, "warnings": []}

        if "resolution_date" not in extracted:
            return result

        resolution_data = extracted["resolution_date"]
        normalized_utc = resolution_data["normalized_utc"]

        if not normalized_utc:
            result["warnings"].append("Could not normalize resolution date")
            return result

        if use_rails:
            # Queue Rails operation for setting closed_at
            rails_op = {
                "type": "set_closed_at",
                "jira_key": work_package_data.get("jira_key", "unknown"),
                "timestamp": normalized_utc,
                "original_value": resolution_data["raw_value"],
            }
            result["rails_operation"] = rails_op
        else:
            # Set closed_at via work package data
            work_package_data["closed_at"] = normalized_utc

        return result

    def _migrate_custom_date_fields(
        self,
        extracted: dict[str, Any],
        work_package_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate custom date fields."""
        result = {"warnings": []}

        custom_dates = {k: v for k, v in extracted.items() if k.startswith("custom_")}
        
        for field_name, date_data in custom_dates.items():
            normalized_utc = date_data["normalized_utc"]
            if normalized_utc:
                # Store as custom field reference for later processing
                if "custom_fields" not in work_package_data:
                    work_package_data["custom_fields"] = {}
                
                work_package_data["custom_fields"][field_name] = {
                    "value": normalized_utc,
                    "jira_field": date_data["jira_field"],
                    "type": "date"
                }
            else:
                result["warnings"].append(f"Could not normalize custom date field {field_name}")

        return result

    def execute_rails_timestamp_operations(
        self, work_package_mapping: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute queued Rails operations for timestamp preservation."""
        if not self._rails_operations_cache:
            return {"processed": 0, "errors": []}

        try:
            # Generate Rails script for timestamp updates
            script = self._generate_timestamp_preservation_script(work_package_mapping)
            
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
            self.logger.error("Failed to execute Rails timestamp operations: %s", e)
            return {
                "processed": 0,
                "errors": [str(e)]
            }

    def _generate_timestamp_preservation_script(
        self, work_package_mapping: dict[str, Any]
    ) -> str:
        """Generate Rails script for preserving timestamp information.
        
        SECURITY: This method generates Ruby code that will be executed in the Rails
        console. To prevent injection attacks, all user-provided data (especially 
        jira_key values) must be validated and properly escaped before inclusion.
        
        Security Measures Implemented:
        1. Validates all jira_key values via _validate_jira_key() before use
        2. Uses json.dumps() to escape jira_key AND field_name for safe Ruby hash literals  
        3. Wraps each operation in begin/rescue blocks for error isolation
        4. Uses parameterized database queries (WorkPackage.find(id))
        5. Timestamp values are pre-validated and passed as string literals
        
        Generated Script Structure:
        - Ruby requires json library for safe data handling
        - Each operation wrapped in begin/rescue for error isolation
        - Operations and errors tracked in arrays for audit trail
        - Human-readable output for monitoring and debugging
        - Field assignment uses DateTime.parse() for safe timestamp parsing
        
        Args:
            work_package_mapping: Dict mapping work package IDs to their metadata,
                                 including jira_key for cross-reference
                                 
        Returns:
            str: Safe Ruby script ready for Rails console execution
            
        Raises:
            ValueError: If any jira_key fails security validation
            
        Note:
            This method assumes _rails_operations_cache contains validated operations.
            Field names are derived from operation types and also escaped for safety.
        """
        script_lines = [
            "# Enhanced Timestamp Preservation Script",
            "require 'json'",
            "",
            "operations = []",
            "errors = []",
            "",
        ]

        for operation in self._rails_operations_cache:
            jira_key = operation["jira_key"]
            op_type = operation["type"]
            timestamp = operation["timestamp"]
            
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
                field_name = op_type.replace("set_", "")
                # SECURITY: Escape jira_key and field_name to prevent injection in Ruby hash literals
                # json.dumps() ensures quotes, newlines, and special chars are properly escaped
                # Example: "TEST'; DROP TABLE users;" becomes "\"TEST'; DROP TABLE users;\""
                escaped_jira_key = json.dumps(jira_key)
                script_lines.extend([
                    f"# Update {field_name} for work package {wp_id} (Jira: {jira_key})",
                    f"begin",
                    f"  wp = WorkPackage.find({wp_id})",
                    f"  wp.{field_name} = DateTime.parse('{timestamp}')",
                    f"  wp.save(validate: false)  # Skip validations for metadata updates",
                    f"  operations << {{jira_key: {escaped_jira_key}, wp_id: {wp_id}, field: {json.dumps(field_name)}, status: 'success'}}",
                    f"rescue => e",
                    f"  errors << {{jira_key: {escaped_jira_key}, wp_id: {wp_id}, field: {json.dumps(field_name)}, error: e.message}}",
                    f"end",
                    "",
                ])

        script_lines.extend([
            "puts \"Timestamp preservation completed:\"",
            "puts \"Successful operations: #{operations.length}\"",
            "puts \"Errors: #{errors.length}\"",
            "",
            "if errors.any?",
            "  puts \"Errors encountered:\"",
            "  errors.each { |error| puts \"  #{error[:jira_key]} (#{error[:field]}): #{error[:error]}\" }",
            "end",
            "",
            "# Return results",
            "{operations: operations, errors: errors}",
        ])

        return "\n".join(script_lines)

    def queue_rails_operation(self, operation: dict[str, Any]) -> None:
        """Queue a Rails operation for later execution."""
        self._rails_operations_cache.append(operation)

    def generate_timestamp_report(self) -> dict[str, Any]:
        """Generate comprehensive report on timestamp migration."""
        total_issues = len(self.migration_results)
        successful = sum(1 for r in self.migration_results.values() if r["status"] == "success")
        partial = sum(1 for r in self.migration_results.values() if r["status"] == "partial")
        failed = sum(1 for r in self.migration_results.values() if r["status"] == "failed")

        # Analyze timestamp types migrated
        timestamp_types = {}
        for result in self.migration_results.values():
            for ts_type in result["migrated_timestamps"].keys():
                timestamp_types[ts_type] = timestamp_types.get(ts_type, 0) + 1

        report = {
            "summary": {
                "total_issues": total_issues,
                "successful_migrations": successful,
                "partial_migrations": partial,
                "failed_migrations": failed,
                "success_percentage": (successful / total_issues * 100) if total_issues > 0 else 0,
            },
            "timestamp_types_migrated": timestamp_types,
            "rails_operations_pending": len(self._rails_operations_cache),
            "jira_timezone": self.jira_timezone,
            "target_timezone": self.target_timezone,
            "detailed_results": dict(self.migration_results),
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }

        return report

    def save_migration_results(self) -> None:
        """Save timestamp migration results to file."""
        try:
            results_file = config.get_path("data") / "timestamp_migration_results.json"
            
            # Convert to serializable format
            serializable_results = {
                k: dict(v) for k, v in self.migration_results.items()
            }
            
            with results_file.open("w") as f:
                json.dump(serializable_results, f, indent=2)
            
            self.logger.info("Saved timestamp migration results to %s", results_file)
        except Exception as e:
            self.logger.error("Failed to save timestamp migration results: %s", e) 