#!/usr/bin/env python3
"""Enhanced Data Preservation Manager with comprehensive safeguards."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.utils.advanced_config_manager import ConfigurationManager

logger = logging.getLogger(__name__)
console = Console()


class EntityChangeType:
    """Types of changes that can occur to entities."""
    
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


class ConflictResolution:
    """Conflict resolution strategies."""
    
    JIRA_WINS = "jira_wins"
    OPENPROJECT_WINS = "openproject_wins"
    MERGE = "merge"
    PROMPT_USER = "prompt_user"
    SKIP = "skip"


class MergeStrategy:
    """Merge strategies for conflicting data."""
    
    LATEST_TIMESTAMP = "latest_timestamp"
    LONGEST_VALUE = "longest_value"
    CONCATENATE = "concatenate"
    CUSTOM = "custom"


class DataPreservationManager:
    """Enhanced manager for preserving manually imported or modified data in OpenProject."""
    
    def __init__(self, config_manager: ConfigurationManager, preservation_dir: Optional[Path] = None):
        """Initialize the data preservation manager.
        
        Args:
            config_manager: Configuration manager instance
            preservation_dir: Directory for storing preservation data
        """
        self.config = config_manager
        self.preservation_dir = preservation_dir or Path("data_preservation")
        self.preservation_dir.mkdir(exist_ok=True)
        
        # Enhanced preservation policies with more granular control
        self.preservation_policies = {
            "users": {
                "resolution_strategy": ConflictResolution.OPENPROJECT_WINS,
                "merge_strategy": MergeStrategy.LATEST_TIMESTAMP,
                "protected_fields": ["firstname", "lastname", "email", "title", "department"],
                "allow_merge": True,
                "backup_before_update": True,
                "notify_on_conflict": True,
            },
            "projects": {
                "resolution_strategy": ConflictResolution.OPENPROJECT_WINS,
                "merge_strategy": MergeStrategy.LATEST_TIMESTAMP,
                "protected_fields": ["name", "description", "status"],
                "allow_merge": True,
                "backup_before_update": True,
                "notify_on_conflict": True,
            },
            "issues": {
                "resolution_strategy": ConflictResolution.MERGE,
                "merge_strategy": MergeStrategy.LATEST_TIMESTAMP,
                "protected_fields": ["subject", "description", "status", "priority"],
                "allow_merge": True,
                "backup_before_update": True,
                "notify_on_conflict": True,
            },
            "work_packages": {
                "resolution_strategy": ConflictResolution.MERGE,
                "merge_strategy": MergeStrategy.LATEST_TIMESTAMP,
                "protected_fields": ["subject", "description", "status", "priority"],
                "allow_merge": True,
                "backup_before_update": True,
                "notify_on_conflict": True,
            },
            "comments": {
                "resolution_strategy": ConflictResolution.OPENPROJECT_WINS,
                "merge_strategy": MergeStrategy.CONCATENATE,
                "protected_fields": ["comment"],
                "allow_merge": False,  # Comments should not be merged
                "backup_before_update": True,
                "notify_on_conflict": True,
            },
            "attachments": {
                "resolution_strategy": ConflictResolution.OPENPROJECT_WINS,
                "merge_strategy": MergeStrategy.LATEST_TIMESTAMP,
                "protected_fields": ["filename", "description"],
                "allow_merge": False,  # Attachments should not be merged
                "backup_before_update": True,
                "notify_on_conflict": True,
            },
        }
        
        # Enhanced conflict detection settings
        self.conflict_detection_settings = {
            "ignore_case": True,
            "ignore_whitespace": True,
            "treat_empty_as_null": True,
            "timestamp_tolerance_seconds": 300,  # 5 minutes
            "field_specific_rules": {
                "email": {"normalize": True, "case_sensitive": False},
                "name": {"normalize": True, "case_sensitive": False},
                "description": {"normalize": True, "case_sensitive": False},
            }
        }
        
        # Initialize directories
        self._init_directories()
        
        # Load custom policies from config if available
        self._load_custom_policies()
    
    def _init_directories(self) -> None:
        """Initialize preservation directories."""
        dirs = [
            "original_states",
            "backups",
            "conflicts",
            "logs",
            "policies",
            "snapshots",
        ]
        
        for dir_name in dirs:
            (self.preservation_dir / dir_name).mkdir(exist_ok=True)
    
    def _load_custom_policies(self) -> None:
        """Load custom preservation policies from configuration."""
        policies_file = self.preservation_dir / "policies" / "custom_policies.json"
        
        if policies_file.exists():
            try:
                with policies_file.open("r") as f:
                    custom_policies = json.load(f)
                
                # Merge custom policies with defaults
                for entity_type, policy in custom_policies.items():
                    if entity_type in self.preservation_policies:
                        self.preservation_policies[entity_type].update(policy)
                    else:
                        self.preservation_policies[entity_type] = policy
                
                logger.info(f"Loaded {len(custom_policies)} custom preservation policies")
            except Exception as e:
                logger.warning(f"Failed to load custom policies: {e}")
    
    def store_original_state(self, entity_id: str, entity_type: str, data: Dict[str, Any]) -> None:
        """Store the original state of an entity before any modifications.
        
        Args:
            entity_id: Unique identifier for the entity
            entity_type: Type of entity (users, projects, etc.)
            data: Entity data to store
        """
        entity_dir = self.preservation_dir / "original_states" / entity_type
        entity_dir.mkdir(parents=True, exist_ok=True)
        
        state_file = entity_dir / f"{entity_id}.json"
        
        state_data = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "data": data,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "version": "1.0",
        }
        
        with state_file.open("w") as f:
            json.dump(state_data, f, indent=2, default=str)
        
        logger.debug(f"Stored original state for {entity_type}:{entity_id}")
    
    def get_original_state(self, entity_id: str, entity_type: str) -> Optional[Dict[str, Any]]:
        """Retrieve the original state of an entity.
        
        Args:
            entity_id: Unique identifier for the entity
            entity_type: Type of entity
            
        Returns:
            Original state data or None if not found
        """
        state_file = self.preservation_dir / "original_states" / entity_type / f"{entity_id}.json"
        
        if not state_file.exists():
            return None
        
        try:
            with state_file.open("r") as f:
                state_data = json.load(f)
            return state_data.get("data")
        except Exception as e:
            logger.warning(f"Failed to load original state for {entity_type}:{entity_id}: {e}")
            return None
    
    def detect_openproject_changes(self, entity_id: str, entity_type: str, current_data: Dict[str, Any]) -> str:
        """Detect if an entity has been manually modified in OpenProject.
        
        Args:
            entity_id: Unique identifier for the entity
            entity_type: Type of entity
            current_data: Current data in OpenProject
            
        Returns:
            Change type (CREATED, UPDATED, UNCHANGED)
        """
        original_state = self.get_original_state(entity_id, entity_type)
        
        if not original_state:
            return EntityChangeType.CREATED
        
        # Enhanced change detection with field-specific rules
        changes = self._detect_field_changes(original_state, current_data, entity_type)
        
        if not changes:
            return EntityChangeType.UNCHANGED
        
        # Log detected changes
        logger.info(f"Detected changes in {entity_type}:{entity_id}: {list(changes.keys())}")
        
        return EntityChangeType.UPDATED
    
    def _detect_field_changes(self, original: Dict[str, Any], current: Dict[str, Any], entity_type: str) -> Dict[str, Any]:
        """Detect specific field changes with enhanced logic.
        
        Args:
            original: Original data
            current: Current data
            entity_type: Type of entity
            
        Returns:
            Dictionary of changed fields and their values
        """
        changes = {}
        field_rules = self.conflict_detection_settings.get("field_specific_rules", {})
        
        # Get protected fields for this entity type
        protected_fields = self.preservation_policies.get(entity_type, {}).get("protected_fields", [])
        
        for field in protected_fields:
            if field not in original and field not in current:
                continue
            
            original_value = original.get(field)
            current_value = current.get(field)
            
            # Apply field-specific normalization
            if field in field_rules:
                rule = field_rules[field]
                if rule.get("normalize", False):
                    original_value = self._normalize_field_value(original_value, rule)
                    current_value = self._normalize_field_value(current_value, rule)
            
            # Enhanced comparison logic
            if self._values_differ(original_value, current_value, field):
                changes[field] = {
                    "original": original_value,
                    "current": current_value,
                    "change_type": "modified" if field in original else "added"
                }
        
        return changes
    
    def _normalize_field_value(self, value: Any, rule: Dict[str, Any]) -> Any:
        """Normalize field value based on rules.
        
        Args:
            value: Field value to normalize
            rule: Normalization rule
            
        Returns:
            Normalized value
        """
        if value is None:
            return None
        
        value_str = str(value)
        
        # Apply case sensitivity rule
        if not rule.get("case_sensitive", True):
            value_str = value_str.lower()
        
        # Apply whitespace normalization
        if self.conflict_detection_settings.get("ignore_whitespace", True):
            value_str = value_str.strip()
        
        return value_str
    
    def _values_differ(self, original: Any, current: Any, field: str) -> bool:
        """Enhanced value comparison with field-specific logic.
        
        Args:
            original: Original value
            current: Current value
            field: Field name for context
            
        Returns:
            True if values differ significantly
        """
        # Handle None/empty values
        if self.conflict_detection_settings.get("treat_empty_as_null", True):
            if original in [None, "", "null"] and current in [None, "", "null"]:
                return False
        
        # Basic comparison
        if original == current:
            return False
        
        # Handle case-insensitive comparison
        if self.conflict_detection_settings.get("ignore_case", True):
            if str(original).lower() == str(current).lower():
                return False
        
        # Field-specific comparison logic
        if field == "email":
            # Normalize email addresses
            orig_email = str(original).lower().strip() if original else ""
            curr_email = str(current).lower().strip() if current else ""
            return orig_email != curr_email
        
        return True
    
    def detect_conflicts(self, jira_data: Dict[str, Any], openproject_data: Dict[str, Any], entity_type: str) -> List[str]:
        """Detect conflicts between Jira and OpenProject data.
        
        Args:
            jira_data: Data from Jira
            openproject_data: Data from OpenProject
            entity_type: Type of entity
            
        Returns:
            List of conflicting field names
        """
        conflicts = []
        protected_fields = self.preservation_policies.get(entity_type, {}).get("protected_fields", [])
        
        for field in protected_fields:
            jira_value = jira_data.get(field)
            op_value = openproject_data.get(field)
            
            if self._values_differ(jira_value, op_value, field):
                conflicts.append(field)
        
        return conflicts
    
    def resolve_conflict(self, conflict: Dict[str, Any], jira_data: Dict[str, Any], openproject_data: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve a conflict between Jira and OpenProject data.
        
        Args:
            conflict: Conflict information
            jira_data: Data from Jira
            openproject_data: Data from OpenProject
            
        Returns:
            Resolved data
        """
        entity_type = conflict.get("entity_type")
        resolution_strategy = conflict.get("resolution_strategy")
        
        if resolution_strategy == ConflictResolution.JIRA_WINS:
            return self._merge_data(openproject_data, jira_data, entity_type)
        elif resolution_strategy == ConflictResolution.OPENPROJECT_WINS:
            return self._merge_data(jira_data, openproject_data, entity_type)
        elif resolution_strategy == ConflictResolution.MERGE:
            return self._merge_data(jira_data, openproject_data, entity_type, merge_conflicts=True)
        elif resolution_strategy == ConflictResolution.SKIP:
            return openproject_data
        else:
            # Default to OpenProject wins for safety
            return self._merge_data(jira_data, openproject_data, entity_type)
    
    def _merge_data(self, base_data: Dict[str, Any], update_data: Dict[str, Any], entity_type: str, merge_conflicts: bool = False) -> Dict[str, Any]:
        """Merge data with conflict resolution.
        
        Args:
            base_data: Base data (higher priority)
            update_data: Update data (lower priority)
            entity_type: Type of entity
            merge_conflicts: Whether to merge conflicting fields
            
        Returns:
            Merged data
        """
        merged = base_data.copy()
        merge_strategy = self.preservation_policies.get(entity_type, {}).get("merge_strategy", MergeStrategy.LATEST_TIMESTAMP)
        
        for field, value in update_data.items():
            if field not in merged:
                # New field, always add
                merged[field] = value
            elif merge_conflicts and field in self.preservation_policies.get(entity_type, {}).get("protected_fields", []):
                # Merge conflicting fields based on strategy
                merged[field] = self._apply_merge_strategy(
                    merged[field], value, merge_strategy, field
                )
            # Otherwise, keep base_data value (higher priority)
        
        return merged
    
    def _apply_merge_strategy(self, value1: Any, value2: Any, strategy: str, field: str) -> Any:
        """Apply merge strategy to conflicting values.
        
        Args:
            value1: First value
            value2: Second value
            strategy: Merge strategy to apply
            field: Field name for context
            
        Returns:
            Merged value
        """
        if strategy == MergeStrategy.LATEST_TIMESTAMP:
            # For now, prefer the second value (more recent)
            return value2
        elif strategy == MergeStrategy.LONGEST_VALUE:
            str1 = str(value1) if value1 else ""
            str2 = str(value2) if value2 else ""
            return value1 if len(str1) >= len(str2) else value2
        elif strategy == MergeStrategy.CONCATENATE:
            str1 = str(value1) if value1 else ""
            str2 = str(value2) if value2 else ""
            if str1 and str2:
                return f"{str1} | {str2}"
            return str1 or str2
        else:
            # Default to second value
            return value2
    
    def create_backup(self, entity_id: str, entity_type: str, data: Dict[str, Any]) -> str:
        """Create a backup of current data before modification.
        
        Args:
            entity_id: Unique identifier for the entity
            entity_type: Type of entity
            data: Data to backup
            
        Returns:
            Backup file path
        """
        backup_dir = self.preservation_dir / "backups" / entity_type
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir / f"{entity_id}_{timestamp}.json"
        
        backup_data = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "data": data,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "backup_reason": "pre_update",
        }
        
        with backup_file.open("w") as f:
            json.dump(backup_data, f, indent=2, default=str)
        
        logger.info(f"Created backup for {entity_type}:{entity_id} at {backup_file}")
        return str(backup_file)
    
    def restore_from_backup(self, backup_file: str) -> Optional[Dict[str, Any]]:
        """Restore data from a backup file.
        
        Args:
            backup_file: Path to backup file
            
        Returns:
            Restored data or None if failed
        """
        try:
            with open(backup_file, "r") as f:
                backup_data = json.load(f)
            return backup_data.get("data")
        except Exception as e:
            logger.error(f"Failed to restore from backup {backup_file}: {e}")
            return None
    
    def analyze_preservation_status(self, jira_changes: Dict[str, Any], entity_type: str, openproject_client=None) -> Dict[str, Any]:
        """Analyze preservation status for a batch of entities.
        
        Args:
            jira_changes: Dictionary of Jira changes by entity ID
            entity_type: Type of entity
            openproject_client: OpenProject client for fetching current data
            
        Returns:
            Analysis report
        """
        if not jira_changes:
            return {
                "total_entities": 0,
                "total_conflicts": 0,
                "conflicts": [],
                "analysis_time": 0,
            }
        
        start_time = datetime.now(tz=UTC)
        conflicts = []
        batch_size = 1000
        
        # Process in batches for performance
        entity_ids = list(jira_changes.keys())
        total_batches = (len(entity_ids) + batch_size - 1) // batch_size
        
        logger.info(f"Starting batch analysis for {len(entity_ids)} {entity_type} entities (batch_size={batch_size}, ~{total_batches} batches)")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Analyzing preservation status...", total=total_batches)
            
            for i in range(0, len(entity_ids), batch_size):
                batch_ids = entity_ids[i:i + batch_size]
                
                # Fetch current OpenProject data for this batch
                if openproject_client:
                    try:
                        current_data = openproject_client.batch_find_records(entity_type, batch_ids)
                        batch_success = len(current_data)
                        logger.info(f"Phase 1 complete: Batch fetch returned {batch_success}/{len(batch_ids)} entities ({batch_success/len(batch_ids)*100:.1f}% success) in {(datetime.now(tz=UTC) - start_time).total_seconds():.2f}s")
                    except Exception as e:
                        logger.warning(f"Failed to fetch batch data: {e}")
                        current_data = {}
                else:
                    current_data = {}
                
                # Analyze each entity in the batch
                for entity_id in batch_ids:
                    jira_data = jira_changes.get(entity_id, {})
                    op_data = current_data.get(entity_id, {})
                    
                    # Detect conflicts
                    conflicting_fields = self.detect_conflicts(jira_data, op_data, entity_type)
                    
                    if conflicting_fields:
                        # Get original state for context
                        original_state = self.get_original_state(entity_id, entity_type)
                        
                        conflict_info = {
                            "entity_id": entity_id,
                            "entity_type": entity_type,
                            "conflicted_fields": conflicting_fields,
                            "jira_data": jira_data,
                            "openproject_data": op_data,
                            "original_state": original_state,
                            "resolution_strategy": self.preservation_policies.get(entity_type, {}).get("resolution_strategy", ConflictResolution.OPENPROJECT_WINS),
                            "merge_strategy": self.preservation_policies.get(entity_type, {}).get("merge_strategy", MergeStrategy.LATEST_TIMESTAMP),
                            "detected_at": datetime.now(tz=UTC).isoformat(),
                        }
                        
                        conflicts.append(conflict_info)
                
                progress.update(task, advance=1)
        
        analysis_time = (datetime.now(tz=UTC) - start_time).total_seconds()
        
        logger.info(f"Analysis complete for {entity_type}: {len(conflicts)} conflicts found out of {len(entity_ids)} entities ({analysis_time:.2f}s total, {len(entity_ids)/analysis_time:.1f} entities/sec, batch_size={batch_size})")
        
        return {
            "total_entities": len(entity_ids),
            "total_conflicts": len(conflicts),
            "conflicts": conflicts,
            "analysis_time": analysis_time,
            "entity_type": entity_type,
        }
    
    def get_preservation_policy(self, entity_type: str) -> Dict[str, Any]:
        """Get preservation policy for an entity type.
        
        Args:
            entity_type: Type of entity
            
        Returns:
            Preservation policy
        """
        return self.preservation_policies.get(entity_type, {})
    
    def update_preservation_policy(self, entity_type: str, policy: Dict[str, Any]) -> None:
        """Update preservation policy for an entity type.
        
        Args:
            entity_type: Type of entity
            policy: New policy configuration
        """
        if entity_type not in self.preservation_policies:
            self.preservation_policies[entity_type] = {}
        
        self.preservation_policies[entity_type].update(policy)
        
        # Save to custom policies file
        policies_file = self.preservation_dir / "policies" / "custom_policies.json"
        policies_file.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            if policies_file.exists():
                with policies_file.open("r") as f:
                    custom_policies = json.load(f)
            else:
                custom_policies = {}
            
            custom_policies[entity_type] = self.preservation_policies[entity_type]
            
            with policies_file.open("w") as f:
                json.dump(custom_policies, f, indent=2, default=str)
            
            logger.info(f"Updated preservation policy for {entity_type}")
        except Exception as e:
            logger.error(f"Failed to save preservation policy: {e}")
    
    def get_preservation_summary(self) -> Dict[str, Any]:
        """Get a summary of preservation data.
        
        Returns:
            Summary information
        """
        summary = {
            "total_entities": 0,
            "entity_types": {},
            "recent_conflicts": [],
            "backup_count": 0,
        }
        
        # Count entities by type
        original_states_dir = self.preservation_dir / "original_states"
        if original_states_dir.exists():
            for entity_type_dir in original_states_dir.iterdir():
                if entity_type_dir.is_dir():
                    entity_count = len(list(entity_type_dir.glob("*.json")))
                    summary["entity_types"][entity_type_dir.name] = entity_count
                    summary["total_entities"] += entity_count
        
        # Count backups
        backup_dir = self.preservation_dir / "backups"
        if backup_dir.exists():
            summary["backup_count"] = len(list(backup_dir.rglob("*.json")))
        
        # Get recent conflicts
        conflicts_dir = self.preservation_dir / "conflicts"
        if conflicts_dir.exists():
            conflict_files = sorted(conflicts_dir.rglob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
            for conflict_file in conflict_files[:10]:  # Last 10 conflicts
                try:
                    with conflict_file.open("r") as f:
                        conflict_data = json.load(f)
                    summary["recent_conflicts"].append({
                        "entity_id": conflict_data.get("entity_id"),
                        "entity_type": conflict_data.get("entity_type"),
                        "detected_at": conflict_data.get("detected_at"),
                    })
                except Exception:
                    continue
        
        return summary
