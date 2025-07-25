#!/usr/bin/env python3
"""Comprehensive Time Entry Migration for Jira to OpenProject migration.

This module provides complete time tracking data migration capabilities:
1. Extracts work logs from Jira with all metadata
2. Extracts Tempo time entries with account and billing information  
3. Transforms and maps time entries to OpenProject format
4. Handles bulk migration with error handling and reporting
5. Integrates with Rails console for advanced operations
6. Provides comprehensive validation and reporting
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from src.display import configure_logging
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.utils.time_entry_transformer import TimeEntryTransformer

# Get logger from config
logger = configure_logging("INFO", None)


class TimeEntryMigrationResult(TypedDict):
    """Type hint for time entry migration results."""
    
    total_work_logs_found: int
    jira_work_logs_extracted: int
    tempo_entries_extracted: int
    successful_transformations: int
    failed_transformations: int
    successful_migrations: int
    failed_migrations: int
    skipped_entries: int
    errors: List[str]
    warnings: List[str]
    processing_time_seconds: float


class TimeEntryMigrator:
    """Comprehensive time entry migrator for Jira to OpenProject migration."""

    def __init__(
        self, 
        jira_client: JiraClient, 
        op_client: OpenProjectClient,
        data_dir: Optional[Path] = None
    ) -> None:
        """Initialize the Time Entry Migrator.
        
        Args:
            jira_client: JiraClient instance for accessing Jira data
            op_client: OpenProjectClient instance for creating time entries
            data_dir: Directory for storing migration data files
        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.logger = logger
        self.data_dir = data_dir or Path(config.get_path("data"))
        
        # Data storage
        self.extracted_work_logs: Dict[str, List[Dict[str, Any]]] = {}
        self.extracted_tempo_entries: List[Dict[str, Any]] = []
        self.transformed_time_entries: List[Dict[str, Any]] = []
        
        # Migration mappings
        self.user_mapping: Dict[str, int] = {}
        self.work_package_mapping: Dict[str, int] = {}
        self.activity_mapping: Dict[str, int] = {}
        self.project_mapping: Dict[str, int] = {}
        
        # Results tracking
        self.migration_results: TimeEntryMigrationResult = {
            "total_work_logs_found": 0,
            "jira_work_logs_extracted": 0,
            "tempo_entries_extracted": 0,
            "successful_transformations": 0,
            "failed_transformations": 0,
            "successful_migrations": 0,
            "failed_migrations": 0,
            "skipped_entries": 0,
            "errors": [],
            "warnings": [],
            "processing_time_seconds": 0.0
        }
        
        # Initialize transformer (will be updated with mappings)
        self.transformer = TimeEntryTransformer()
        
        # Load mappings if available
        self._load_mappings()

    def _load_mappings(self) -> None:
        """Load existing mappings from migration data files."""
        try:
            # Load user mapping
            user_mapping_file = self.data_dir / "user_mapping.json"
            if user_mapping_file.exists():
                with open(user_mapping_file, 'r', encoding='utf-8') as f:
                    user_data = json.load(f)
                    self.user_mapping = {
                        entry.get("jira_username", ""): entry.get("openproject_id")
                        for entry in user_data.values()
                        if entry.get("jira_username") and entry.get("openproject_id")
                    }
                self.logger.info(f"Loaded {len(self.user_mapping)} user mappings")
            
            # Load work package mapping
            wp_mapping_file = self.data_dir / "work_package_mapping.json"
            if wp_mapping_file.exists():
                with open(wp_mapping_file, 'r', encoding='utf-8') as f:
                    wp_data = json.load(f)
                    self.work_package_mapping = {
                        entry.get("jira_key", ""): entry.get("openproject_id")
                        for entry in wp_data.values()
                        if entry.get("jira_key") and entry.get("openproject_id")
                    }
                self.logger.info(f"Loaded {len(self.work_package_mapping)} work package mappings")
                
            # Load activity mapping from OpenProject
            self._load_activity_mapping()
            
        except Exception as e:
            self.logger.warning(f"Failed to load some mappings: {e}")

    def _load_activity_mapping(self) -> None:
        """Load activity mapping from OpenProject."""
        try:
            activities = self.op_client.get_time_entry_activities()
            self.activity_mapping = {
                activity.get("name", "").lower(): activity.get("id")
                for activity in activities
                if activity.get("name") and activity.get("id")
            }
            
            # Set default activity ID (usually first one)
            if self.activity_mapping:
                self.default_activity_id = next(iter(self.activity_mapping.values()))
            else:
                self.default_activity_id = None
                
            self.logger.info(f"Loaded {len(self.activity_mapping)} activity mappings")
            
        except Exception as e:
            self.logger.warning(f"Failed to load activity mappings: {e}")
            self.activity_mapping = {}
            self.default_activity_id = None

    def extract_jira_work_logs_for_issues(
        self, 
        issue_keys: List[str],
        save_to_file: bool = True
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Extract work logs from Jira for a list of issues.
        
        Args:
            issue_keys: List of Jira issue keys to extract work logs for
            save_to_file: Whether to save extracted data to file
            
        Returns:
            Dictionary mapping issue keys to their work logs
        """
        self.logger.info(f"Extracting work logs for {len(issue_keys)} Jira issues")
        start_time = datetime.now()
        
        extracted_logs = {}
        total_logs = 0
        
        for issue_key in issue_keys:
            try:
                work_logs = self.jira_client.get_work_logs_for_issue(issue_key)
                
                if work_logs:
                    # Add issue_key to each work log for later processing
                    for log in work_logs:
                        log["issue_key"] = issue_key
                    
                    extracted_logs[issue_key] = work_logs
                    total_logs += len(work_logs)
                    self.logger.debug(f"Extracted {len(work_logs)} work logs for {issue_key}")
                else:
                    self.logger.debug(f"No work logs found for {issue_key}")
                    
            except Exception as e:
                error_msg = f"Failed to extract work logs for {issue_key}: {e}"
                self.logger.error(error_msg)
                self.migration_results["errors"].append(error_msg)
                continue
        
        self.extracted_work_logs = extracted_logs
        self.migration_results["jira_work_logs_extracted"] = total_logs
        self.migration_results["total_work_logs_found"] += total_logs
        
        # Save to file if requested
        if save_to_file:
            self._save_extracted_work_logs()
        
        processing_time = (datetime.now() - start_time).total_seconds()
        self.logger.success(
            f"Extracted {total_logs} work logs from {len(extracted_logs)} issues in {processing_time:.2f}s"
        )
        
        return extracted_logs

    def extract_tempo_time_entries(
        self, 
        project_keys: Optional[List[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        save_to_file: bool = True
    ) -> List[Dict[str, Any]]:
        """Extract Tempo time entries with enhanced metadata.
        
        Args:
            project_keys: List of project keys to extract entries for (None for all)
            date_from: Start date for extraction (YYYY-MM-DD format)
            date_to: End date for extraction (YYYY-MM-DD format)
            save_to_file: Whether to save extracted data to file
            
        Returns:
            List of Tempo time entry dictionaries
        """
        self.logger.info("Extracting Tempo time entries")
        start_time = datetime.now()
        
        try:
            # Check if Tempo is available
            if not hasattr(self.jira_client, 'get_tempo_time_entries'):
                self.logger.warning("Tempo integration not available - skipping Tempo extraction")
                return []
            
            # Extract Tempo entries with all metadata
            tempo_entries = self.jira_client.get_tempo_time_entries(
                project_keys=project_keys,
                date_from=date_from,
                date_to=date_to
            )
            
            if tempo_entries:
                self.extracted_tempo_entries = tempo_entries
                self.migration_results["tempo_entries_extracted"] = len(tempo_entries)
                self.migration_results["total_work_logs_found"] += len(tempo_entries)
                
                # Save to file if requested
                if save_to_file:
                    self._save_extracted_tempo_entries()
                
                processing_time = (datetime.now() - start_time).total_seconds()
                self.logger.success(
                    f"Extracted {len(tempo_entries)} Tempo entries in {processing_time:.2f}s"
                )
            else:
                self.logger.info("No Tempo time entries found")
                
            return tempo_entries
            
        except Exception as e:
            error_msg = f"Failed to extract Tempo time entries: {e}"
            self.logger.error(error_msg)
            self.migration_results["errors"].append(error_msg)
            return []

    def transform_all_time_entries(self) -> List[Dict[str, Any]]:
        """Transform all extracted work logs and Tempo entries to OpenProject format.
        
        Returns:
            List of transformed time entries ready for migration
        """
        self.logger.info("Transforming extracted time entries to OpenProject format")
        start_time = datetime.now()
        
        # Update transformer with current mappings
        self.transformer = TimeEntryTransformer(
            user_mapping=self.user_mapping,
            work_package_mapping=self.work_package_mapping,
            activity_mapping=self.activity_mapping,
            default_activity_id=getattr(self, 'default_activity_id', None)
        )
        
        transformed_entries = []
        
        # Transform Jira work logs
        if self.extracted_work_logs:
            jira_work_logs = []
            for issue_key, work_logs in self.extracted_work_logs.items():
                jira_work_logs.extend(work_logs)
            
            if jira_work_logs:
                self.logger.info(f"Transforming {len(jira_work_logs)} Jira work logs")
                try:
                    jira_transformed = self.transformer.batch_transform_work_logs(
                        jira_work_logs, source_type="jira"
                    )
                    transformed_entries.extend(jira_transformed)
                    self.migration_results["successful_transformations"] += len(jira_transformed)
                    
                except Exception as e:
                    error_msg = f"Failed to transform Jira work logs: {e}"
                    self.logger.error(error_msg)
                    self.migration_results["errors"].append(error_msg)
        
        # Transform Tempo time entries
        if self.extracted_tempo_entries:
            self.logger.info(f"Transforming {len(self.extracted_tempo_entries)} Tempo entries")
            try:
                tempo_transformed = self.transformer.batch_transform_work_logs(
                    self.extracted_tempo_entries, source_type="tempo"
                )
                transformed_entries.extend(tempo_transformed)
                self.migration_results["successful_transformations"] += len(tempo_transformed)
                
            except Exception as e:
                error_msg = f"Failed to transform Tempo entries: {e}"
                self.logger.error(error_msg)
                self.migration_results["errors"].append(error_msg)
        
        self.transformed_time_entries = transformed_entries
        
        # Calculate failed transformations
        total_extracted = (
            self.migration_results["jira_work_logs_extracted"] + 
            self.migration_results["tempo_entries_extracted"]
        )
        self.migration_results["failed_transformations"] = (
            total_extracted - self.migration_results["successful_transformations"]
        )
        
        processing_time = (datetime.now() - start_time).total_seconds()
        self.logger.success(
            f"Transformed {len(transformed_entries)} time entries in {processing_time:.2f}s"
        )
        
        return transformed_entries

    def migrate_time_entries_to_openproject(
        self, 
        time_entries: Optional[List[Dict[str, Any]]] = None,
        batch_size: int = 50,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """Migrate time entries to OpenProject.
        
        Args:
            time_entries: Time entries to migrate (uses transformed_time_entries if None)
            batch_size: Number of entries to process in each batch
            dry_run: If True, don't actually create entries in OpenProject
            
        Returns:
            Migration results summary
        """
        entries_to_migrate = time_entries or self.transformed_time_entries
        if not entries_to_migrate:
            self.logger.warning("No time entries to migrate")
            return {}
        
        self.logger.info(f"Migrating {len(entries_to_migrate)} time entries to OpenProject")
        start_time = datetime.now()
        
        migration_summary = {
            "total_entries": len(entries_to_migrate),
            "successful_migrations": 0,
            "failed_migrations": 0,
            "skipped_entries": 0,
            "created_time_entry_ids": [],
            "errors": [],
            "warnings": []
        }
        
        if dry_run:
            self.logger.warning("DRY RUN mode - no time entries will be created")
            migration_summary["successful_migrations"] = len(entries_to_migrate)
            return migration_summary
        
        # Process in batches
        for i in range(0, len(entries_to_migrate), batch_size):
            batch = entries_to_migrate[i:i + batch_size]
            batch_num = i // batch_size + 1
            
            self.logger.info(f"Processing batch {batch_num} ({len(batch)} entries)")
            
            for entry in batch:
                try:
                    # Validate entry has required fields
                    if not self._validate_time_entry(entry):
                        migration_summary["skipped_entries"] += 1
                        continue
                    
                    # Create time entry in OpenProject
                    created_entry = self.op_client.create_time_entry(entry)
                    
                    if created_entry and created_entry.get("id"):
                        migration_summary["successful_migrations"] += 1
                        migration_summary["created_time_entry_ids"].append(created_entry["id"])
                        
                        self.logger.debug(
                            f"Created time entry {created_entry['id']} for work package "
                            f"{entry.get('_embedded', {}).get('workPackage', {}).get('href', 'unknown')}"
                        )
                    else:
                        migration_summary["failed_migrations"] += 1
                        error_msg = f"Failed to create time entry: No ID returned"
                        migration_summary["errors"].append(error_msg)
                        
                except Exception as e:
                    migration_summary["failed_migrations"] += 1
                    error_msg = f"Failed to create time entry: {e}"
                    migration_summary["errors"].append(error_msg)
                    self.logger.error(error_msg)
        
        # Update global results
        self.migration_results["successful_migrations"] = migration_summary["successful_migrations"]
        self.migration_results["failed_migrations"] = migration_summary["failed_migrations"]
        self.migration_results["skipped_entries"] = migration_summary["skipped_entries"]
        self.migration_results["errors"].extend(migration_summary["errors"])
        
        processing_time = (datetime.now() - start_time).total_seconds()
        self.migration_results["processing_time_seconds"] += processing_time
        
        self.logger.success(
            f"Migration completed: {migration_summary['successful_migrations']} successful, "
            f"{migration_summary['failed_migrations']} failed, "
            f"{migration_summary['skipped_entries']} skipped in {processing_time:.2f}s"
        )
        
        return migration_summary

    def migrate_time_entries_for_issues(
        self,
        migrated_issues: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Migrate time entries for a list of migrated work packages.
        
        This is the main entry point called by the migration workflow.
        
        Args:
            migrated_issues: List of dictionaries with jira_key, work_package_id, project_id
            
        Returns:
            Dictionary with migration results
        """
        self.logger.info(f"Starting time entry migration for {len(migrated_issues)} work packages")
        overall_start_time = datetime.now()
        
        try:
            # Extract issue keys for processing
            issue_keys = [issue["jira_key"] for issue in migrated_issues]
            
            # Update work package mapping from migrated issues
            for issue in migrated_issues:
                if issue.get("jira_key") and issue.get("work_package_id"):
                    self.work_package_mapping[issue["jira_key"]] = issue["work_package_id"]
            
            # Run complete migration process
            migration_result = self.run_complete_migration(
                issue_keys=issue_keys,
                include_tempo=True,
                batch_size=50,
                dry_run=False
            )
            
            # Generate comprehensive report
            self._generate_migration_report()
            
            processing_time = (datetime.now() - overall_start_time).total_seconds()
            self.migration_results["processing_time_seconds"] = processing_time
            
            return {
                "jira_work_logs": {
                    "extracted": self.migration_results["jira_work_logs_extracted"],
                    "migrated": migration_result.get("successful_migrations", 0),
                    "errors": self.migration_results["errors"]
                },
                "tempo_time_entries": {
                    "extracted": self.migration_results["tempo_entries_extracted"],
                    "migrated": migration_result.get("successful_migrations", 0),
                    "errors": self.migration_results["errors"]
                },
                "total_time_entries": {
                    "migrated": migration_result.get("successful_migrations", 0),
                    "failed": migration_result.get("failed_migrations", 0)
                },
                "processing_time_seconds": processing_time,
                "status": "success" if not self.migration_results["errors"] else "partial_success"
            }
            
        except Exception as e:
            self.logger.error(f"Time entry migration failed: {e}")
            return {
                "jira_work_logs": {"extracted": 0, "migrated": 0, "errors": [str(e)]},
                "tempo_time_entries": {"extracted": 0, "migrated": 0, "errors": [str(e)]},
                "total_time_entries": {"migrated": 0, "failed": 0},
                "processing_time_seconds": (datetime.now() - overall_start_time).total_seconds(),
                "status": "failed",
                "error": str(e)
            }

    def run_complete_migration(
        self,
        issue_keys: List[str],
        include_tempo: bool = True,
        batch_size: int = 50,
        dry_run: bool = False
    ) -> TimeEntryMigrationResult:
        """Run the complete time entry migration process.
        
        Args:
            issue_keys: List of Jira issue keys to migrate time entries for
            include_tempo: Whether to include Tempo time entries
            batch_size: Batch size for migration
            dry_run: If True, don't actually create entries in OpenProject
            
        Returns:
            Complete migration results
        """
        self.logger.info("Starting complete time entry migration")
        overall_start_time = datetime.now()
        
        try:
            # Step 1: Extract Jira work logs
            self.extract_jira_work_logs_for_issues(issue_keys)
            
            # Step 2: Extract Tempo entries if requested
            if include_tempo:
                self.extract_tempo_time_entries()
            
            # Step 3: Transform all entries
            self.transform_all_time_entries()
            
            # Step 4: Migrate to OpenProject
            self.migrate_time_entries_to_openproject(
                batch_size=batch_size,
                dry_run=dry_run
            )
            
            # Calculate total processing time
            total_time = (datetime.now() - overall_start_time).total_seconds()
            self.migration_results["processing_time_seconds"] = total_time
            
            # Generate summary report
            self._generate_migration_report()
            
            self.logger.success(
                f"Complete time entry migration finished in {total_time:.2f}s. "
                f"Extracted: {self.migration_results['total_work_logs_found']}, "
                f"Transformed: {self.migration_results['successful_transformations']}, "
                f"Migrated: {self.migration_results['successful_migrations']}"
            )
            
        except Exception as e:
            error_msg = f"Complete migration failed: {e}"
            self.logger.error(error_msg)
            self.migration_results["errors"].append(error_msg)
            
        return self.migration_results

    def _validate_time_entry(self, entry: Dict[str, Any]) -> bool:
        """Validate a time entry before migration.
        
        Args:
            entry: Time entry to validate
            
        Returns:
            True if entry is valid for migration
        """
        # Check required fields
        if not entry.get("hours") or entry["hours"] <= 0:
            self.logger.debug("Skipping entry with invalid hours")
            return False
            
        if not entry.get("spentOn"):
            self.logger.debug("Skipping entry without spentOn date")
            return False
            
        # Check for required associations
        embedded = entry.get("_embedded", {})
        if not embedded.get("workPackage", {}).get("href"):
            self.logger.debug("Skipping entry without work package association")
            return False
            
        if not embedded.get("user", {}).get("href"):
            self.logger.debug("Skipping entry without user association")
            return False
            
        return True

    def _save_extracted_work_logs(self) -> None:
        """Save extracted work logs to file."""
        try:
            work_logs_file = self.data_dir / "extracted_work_logs.json"
            with open(work_logs_file, 'w', encoding='utf-8') as f:
                json.dump(self.extracted_work_logs, f, indent=2, ensure_ascii=False)
            self.logger.debug(f"Saved extracted work logs to {work_logs_file}")
        except Exception as e:
            self.logger.warning(f"Failed to save extracted work logs: {e}")

    def _save_extracted_tempo_entries(self) -> None:
        """Save extracted Tempo entries to file."""
        try:
            tempo_file = self.data_dir / "extracted_tempo_entries.json"
            with open(tempo_file, 'w', encoding='utf-8') as f:
                json.dump(self.extracted_tempo_entries, f, indent=2, ensure_ascii=False)
            self.logger.debug(f"Saved extracted Tempo entries to {tempo_file}")
        except Exception as e:
            self.logger.warning(f"Failed to save extracted Tempo entries: {e}")

    def _generate_migration_report(self) -> None:
        """Generate a comprehensive migration report."""
        try:
            report = {
                "migration_summary": self.migration_results,
                "extraction_details": {
                    "jira_work_logs_by_issue": {
                        issue: len(logs) for issue, logs in self.extracted_work_logs.items()
                    },
                    "tempo_entries_total": len(self.extracted_tempo_entries)
                },
                "mapping_statistics": {
                    "user_mappings": len(self.user_mapping),
                    "work_package_mappings": len(self.work_package_mapping),
                    "activity_mappings": len(self.activity_mapping)
                },
                "transformation_details": {
                    "total_transformed": len(self.transformed_time_entries),
                    "jira_entries": len([e for e in self.transformed_time_entries 
                                       if e.get("_meta", {}).get("jira_work_log_id")]),
                    "tempo_entries": len([e for e in self.transformed_time_entries 
                                        if e.get("_meta", {}).get("tempo_worklog_id")])
                },
                "generated_at": datetime.now().isoformat()
            }
            
            report_file = self.data_dir / "time_entry_migration_report.json"
            with open(report_file, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
                
            self.logger.info(f"Generated migration report: {report_file}")
            
        except Exception as e:
            self.logger.warning(f"Failed to generate migration report: {e}")

    def get_migration_summary(self) -> Dict[str, Any]:
        """Get a summary of the migration results.
        
        Returns:
            Dictionary with migration summary statistics
        """
        return {
            "total_work_logs_found": self.migration_results["total_work_logs_found"],
            "successful_transformations": self.migration_results["successful_transformations"],
            "successful_migrations": self.migration_results["successful_migrations"],
            "failed_migrations": self.migration_results["failed_migrations"],
            "error_count": len(self.migration_results["errors"]),
            "warning_count": len(self.migration_results["warnings"]),
            "processing_time_seconds": self.migration_results["processing_time_seconds"],
            "success_rate": (
                self.migration_results["successful_migrations"] / 
                max(self.migration_results["total_work_logs_found"], 1) * 100
            )
        } 