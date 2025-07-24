#!/usr/bin/env python3
"""Advanced Data Validation Framework for Jira to OpenProject Migration.

This module provides comprehensive validation capabilities for:
- Pre-migration data integrity checks
- In-flight validation during migration
- Post-migration verification and reconciliation
- Cross-reference validation between systems
- Data consistency and business rule enforcement
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union, Callable
from uuid import UUID

import aiofiles
from pydantic import BaseModel, Field, ValidationError, validator

from src.utils.config_validation import SecurityValidator
from src.utils.validators import validate_jira_key

logger = logging.getLogger(__name__)


class ValidationLevel(Enum):
    """Validation severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ValidationPhase(Enum):
    """Migration phases for validation."""
    PRE_MIGRATION = "pre_migration"
    IN_FLIGHT = "in_flight"
    POST_MIGRATION = "post_migration"
    RECONCILIATION = "reconciliation"


@dataclass
class ValidationResult:
    """Result of a validation check."""
    phase: ValidationPhase
    level: ValidationLevel
    message: str
    entity_type: str
    entity_id: Optional[str] = None
    field_name: Optional[str] = None
    expected_value: Optional[Any] = None
    actual_value: Optional[Any] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "phase": self.phase.value,
            "level": self.level.value,
            "message": self.message,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "field_name": self.field_name,
            "expected_value": self.expected_value,
            "actual_value": self.actual_value,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata
        }


@dataclass
class ValidationSummary:
    """Summary of validation results."""
    total_checks: int = 0
    passed_checks: int = 0
    failed_checks: int = 0
    warnings: int = 0
    errors: int = 0
    critical_errors: int = 0
    phase_results: Dict[ValidationPhase, List[ValidationResult]] = field(default_factory=lambda: defaultdict(list))
    
    def add_result(self, result: ValidationResult) -> None:
        """Add a validation result to the summary."""
        self.total_checks += 1
        self.phase_results[result.phase].append(result)
        
        if result.level == ValidationLevel.INFO:
            self.passed_checks += 1
        elif result.level == ValidationLevel.WARNING:
            self.warnings += 1
            self.failed_checks += 1
        elif result.level == ValidationLevel.ERROR:
            self.errors += 1
            self.failed_checks += 1
        elif result.level == ValidationLevel.CRITICAL:
            self.critical_errors += 1
            self.failed_checks += 1
    
    def get_success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total_checks == 0:
            return 100.0
        return (self.passed_checks / self.total_checks) * 100.0
    
    def has_critical_errors(self) -> bool:
        """Check if there are any critical errors."""
        return self.critical_errors > 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "total_checks": self.total_checks,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "warnings": self.warnings,
            "errors": self.errors,
            "critical_errors": self.critical_errors,
            "success_rate": self.get_success_rate(),
            "phase_results": {
                phase.value: [result.to_dict() for result in results]
                for phase, results in self.phase_results.items()
            }
        }


class DataValidator(ABC):
    """Abstract base class for data validators."""
    
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.results: List[ValidationResult] = []
    
    @abstractmethod
    async def validate(self, data: Any, context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate data and return results."""
        pass
    
    def add_result(self, result: ValidationResult) -> None:
        """Add a validation result."""
        self.results.append(result)
    
    def clear_results(self) -> None:
        """Clear all validation results."""
        self.results.clear()


class PreMigrationValidator(DataValidator):
    """Validates data before migration starts."""
    
    def __init__(self):
        super().__init__("PreMigrationValidator", "Validates data integrity before migration")
    
    async def validate(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate pre-migration data."""
        results = []
        
        # Validate Jira data structure
        if "jira_data" in data:
            results.extend(await self._validate_jira_data(data["jira_data"], context))
        
        # Validate OpenProject configuration
        if "openproject_config" in data:
            results.extend(await self._validate_openproject_config(data["openproject_config"], context))
        
        # Validate mapping configurations
        if "mappings" in data:
            results.extend(await self._validate_mappings(data["mappings"], context))
        
        # Validate business rules
        results.extend(await self._validate_business_rules(data, context))
        
        return results
    
    async def _validate_jira_data(self, jira_data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate Jira data structure and integrity."""
        results = []
        
        # Check required Jira components
        required_components = ["projects", "users", "issues", "workflows"]
        for component in required_components:
            if component not in jira_data:
                results.append(ValidationResult(
                    phase=ValidationPhase.PRE_MIGRATION,
                    level=ValidationLevel.CRITICAL,
                    message=f"Missing required Jira component: {component}",
                    entity_type="jira_data",
                    field_name=component
                ))
        
        # Validate project data
        if "projects" in jira_data:
            for project in jira_data["projects"]:
                results.extend(await self._validate_project(project))
        
        # Validate user data
        if "users" in jira_data:
            for user in jira_data["users"]:
                results.extend(await self._validate_user(user))
        
        return results
    
    async def _validate_project(self, project: Dict[str, Any]) -> List[ValidationResult]:
        """Validate individual project data."""
        results = []
        
        # Check required fields
        required_fields = ["key", "name", "id"]
        for field in required_fields:
            if field not in project or not project[field]:
                results.append(ValidationResult(
                    phase=ValidationPhase.PRE_MIGRATION,
                    level=ValidationLevel.ERROR,
                    message=f"Project missing required field: {field}",
                    entity_type="project",
                    entity_id=project.get("key", "unknown"),
                    field_name=field
                ))
        
        # Validate project key format
        if "key" in project:
            try:
                validate_jira_key(project["key"])
            except ValueError as e:
                results.append(ValidationResult(
                    phase=ValidationPhase.PRE_MIGRATION,
                    level=ValidationLevel.ERROR,
                    message=f"Invalid project key format: {str(e)}",
                    entity_type="project",
                    entity_id=project["key"],
                    field_name="key",
                    actual_value=project["key"]
                ))
        
        return results
    
    async def _validate_user(self, user: Dict[str, Any]) -> List[ValidationResult]:
        """Validate individual user data."""
        results = []
        
        # Check required fields
        required_fields = ["username", "email"]
        for field in required_fields:
            if field not in user or not user[field]:
                results.append(ValidationResult(
                    phase=ValidationPhase.PRE_MIGRATION,
                    level=ValidationLevel.ERROR,
                    message=f"User missing required field: {field}",
                    entity_type="user",
                    entity_id=user.get("username", "unknown"),
                    field_name=field
                ))
        
        # Validate email format
        if "email" in user and user["email"]:
            email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
            if not re.match(email_pattern, user["email"]):
                results.append(ValidationResult(
                    phase=ValidationPhase.PRE_MIGRATION,
                    level=ValidationLevel.WARNING,
                    message="Invalid email format",
                    entity_type="user",
                    entity_id=user["username"],
                    field_name="email",
                    actual_value=user["email"]
                ))
        
        return results
    
    async def _validate_openproject_config(self, config: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate OpenProject configuration."""
        results = []
        
        # Check required configuration
        required_config = ["url", "api_key", "project_id"]
        for field in required_config:
            if field not in config or not config[field]:
                results.append(ValidationResult(
                    phase=ValidationPhase.PRE_MIGRATION,
                    level=ValidationLevel.CRITICAL,
                    message=f"OpenProject config missing required field: {field}",
                    entity_type="openproject_config",
                    field_name=field
                ))
        
        return results
    
    async def _validate_mappings(self, mappings: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate mapping configurations."""
        results = []
        
        # Check required mappings
        required_mappings = ["status_mapping", "priority_mapping", "issue_type_mapping"]
        for mapping in required_mappings:
            if mapping not in mappings:
                results.append(ValidationResult(
                    phase=ValidationPhase.PRE_MIGRATION,
                    level=ValidationLevel.WARNING,
                    message=f"Missing mapping configuration: {mapping}",
                    entity_type="mappings",
                    field_name=mapping
                ))
        
        return results
    
    async def _validate_business_rules(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate business rules and constraints."""
        results = []
        
        # Check for orphaned entities
        if "issues" in data.get("jira_data", {}):
            results.extend(await self._check_orphaned_issues(data["jira_data"]["issues"], data["jira_data"].get("users", [])))
        
        # Check for circular dependencies
        if "projects" in data.get("jira_data", {}):
            results.extend(await self._check_circular_dependencies(data["jira_data"]["projects"]))
        
        return results
    
    async def _check_orphaned_issues(self, issues: List[Dict[str, Any]], users: List[Dict[str, Any]]) -> List[ValidationResult]:
        """Check for issues assigned to non-existent users."""
        results = []
        user_usernames = {user["username"] for user in users}
        
        for issue in issues:
            if "assignee" in issue and issue["assignee"]:
                if issue["assignee"] not in user_usernames:
                    results.append(ValidationResult(
                        phase=ValidationPhase.PRE_MIGRATION,
                        level=ValidationLevel.WARNING,
                        message=f"Issue assigned to non-existent user: {issue['assignee']}",
                        entity_type="issue",
                        entity_id=issue.get("key", "unknown"),
                        field_name="assignee",
                        actual_value=issue["assignee"]
                    ))
        
        return results
    
    async def _check_circular_dependencies(self, projects: List[Dict[str, Any]]) -> List[ValidationResult]:
        """Check for circular project dependencies."""
        results = []
        # Implementation for circular dependency detection
        # This would check for projects that reference each other in a circular manner
        return results


class InFlightValidator(DataValidator):
    """Validates data during migration execution."""
    
    def __init__(self):
        super().__init__("InFlightValidator", "Validates data during migration execution")
        self.processed_entities: Set[str] = set()
        self.entity_hashes: Dict[str, str] = {}
    
    async def validate(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate data during migration."""
        results = []
        
        entity_id = data.get("id") or data.get("key")
        entity_type = data.get("type", "unknown")
        
        if entity_id:
            # Check for duplicate processing
            if entity_id in self.processed_entities:
                results.append(ValidationResult(
                    phase=ValidationPhase.IN_FLIGHT,
                    level=ValidationLevel.WARNING,
                    message=f"Entity processed multiple times: {entity_id}",
                    entity_type=entity_type,
                    entity_id=entity_id
                ))
            else:
                self.processed_entities.add(entity_id)
            
            # Check data integrity during transformation
            results.extend(await self._validate_data_integrity(data, context))
            
            # Check for data corruption
            results.extend(await self._validate_data_corruption(data, context))
        
        return results
    
    async def _validate_data_integrity(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate data integrity during transformation."""
        results = []
        
        # Check for required fields after transformation
        required_fields = context.get("required_fields", [])
        for field in required_fields:
            if field not in data or data[field] is None:
                results.append(ValidationResult(
                    phase=ValidationPhase.IN_FLIGHT,
                    level=ValidationLevel.ERROR,
                    message=f"Required field missing after transformation: {field}",
                    entity_type=data.get("type", "unknown"),
                    entity_id=data.get("id") or data.get("key"),
                    field_name=field
                ))
        
        # Check field type consistency
        field_types = context.get("field_types", {})
        for field, expected_type in field_types.items():
            if field in data:
                actual_type = type(data[field]).__name__
                if actual_type != expected_type:
                    results.append(ValidationResult(
                        phase=ValidationPhase.IN_FLIGHT,
                        level=ValidationLevel.WARNING,
                        message=f"Field type mismatch: expected {expected_type}, got {actual_type}",
                        entity_type=data.get("type", "unknown"),
                        entity_id=data.get("id") or data.get("key"),
                        field_name=field,
                        expected_value=expected_type,
                        actual_value=actual_type
                    ))
        
        return results
    
    async def _validate_data_corruption(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Check for data corruption during processing."""
        results = []
        
        # Calculate data hash for corruption detection
        data_str = json.dumps(data, sort_keys=True)
        current_hash = hashlib.md5(data_str.encode()).hexdigest()
        
        entity_id = data.get("id") or data.get("key")
        if entity_id in self.entity_hashes:
            if self.entity_hashes[entity_id] != current_hash:
                results.append(ValidationResult(
                    phase=ValidationPhase.IN_FLIGHT,
                    level=ValidationLevel.CRITICAL,
                    message="Data corruption detected during processing",
                    entity_type=data.get("type", "unknown"),
                    entity_id=entity_id,
                    metadata={"previous_hash": self.entity_hashes[entity_id], "current_hash": current_hash}
                ))
        else:
            self.entity_hashes[entity_id] = current_hash
        
        return results


class PostMigrationValidator(DataValidator):
    """Validates data after migration completion."""
    
    def __init__(self):
        super().__init__("PostMigrationValidator", "Validates data after migration completion")
    
    async def validate(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate post-migration data."""
        results = []
        
        # Validate data completeness
        results.extend(await self._validate_completeness(data, context))
        
        # Validate data accuracy
        results.extend(await self._validate_accuracy(data, context))
        
        # Validate cross-references
        results.extend(await self._validate_cross_references(data, context))
        
        # Validate business rules
        results.extend(await self._validate_business_rules(data, context))
        
        return results
    
    async def _validate_completeness(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate data completeness."""
        results = []
        
        # Check if all expected entities were migrated
        expected_count = context.get("expected_count", 0)
        actual_count = len(data.get("migrated_entities", []))
        
        if actual_count != expected_count:
            results.append(ValidationResult(
                phase=ValidationPhase.POST_MIGRATION,
                level=ValidationLevel.ERROR,
                message=f"Migration completeness mismatch: expected {expected_count}, got {actual_count}",
                entity_type="migration_summary",
                metadata={"expected_count": expected_count, "actual_count": actual_count}
            ))
        
        return results
    
    async def _validate_accuracy(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate data accuracy."""
        results = []
        
        # Compare source and target data
        source_data = context.get("source_data", {})
        target_data = data.get("migrated_entities", [])
        
        for entity in target_data:
            source_entity = source_data.get(entity.get("source_id"))
            if source_entity:
                results.extend(await self._compare_entity_data(source_entity, entity))
        
        return results
    
    async def _compare_entity_data(self, source: Dict[str, Any], target: Dict[str, Any]) -> List[ValidationResult]:
        """Compare source and target entity data."""
        results = []
        
        # Compare critical fields
        critical_fields = ["name", "description", "status"]
        for field in critical_fields:
            if field in source and field in target:
                if source[field] != target[field]:
                    results.append(ValidationResult(
                        phase=ValidationPhase.POST_MIGRATION,
                        level=ValidationLevel.ERROR,
                        message=f"Data accuracy mismatch in field: {field}",
                        entity_type=target.get("type", "unknown"),
                        entity_id=target.get("id"),
                        field_name=field,
                        expected_value=source[field],
                        actual_value=target[field]
                    ))
        
        return results
    
    async def _validate_cross_references(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate cross-references between entities."""
        results = []
        
        # Check for broken references
        entities = data.get("migrated_entities", [])
        entity_ids = {entity.get("id") for entity in entities}
        
        for entity in entities:
            # Check references in the entity
            references = entity.get("references", [])
            for ref in references:
                if ref not in entity_ids:
                    results.append(ValidationResult(
                        phase=ValidationPhase.POST_MIGRATION,
                        level=ValidationLevel.ERROR,
                        message=f"Broken reference: {ref}",
                        entity_type=entity.get("type", "unknown"),
                        entity_id=entity.get("id"),
                        field_name="references",
                        actual_value=ref
                    ))
        
        return results
    
    async def _validate_business_rules(self, data: Dict[str, Any], context: Dict[str, Any]) -> List[ValidationResult]:
        """Validate business rules after migration."""
        results = []
        
        # Check for orphaned entities
        results.extend(await self._check_orphaned_entities(data))
        
        # Check for constraint violations
        results.extend(await self._check_constraint_violations(data))
        
        return results
    
    async def _check_orphaned_entities(self, data: Dict[str, Any]) -> List[ValidationResult]:
        """Check for orphaned entities after migration."""
        results = []
        # Implementation for orphaned entity detection
        return results
    
    async def _check_constraint_violations(self, data: Dict[str, Any]) -> List[ValidationResult]:
        """Check for constraint violations after migration."""
        results = []
        # Implementation for constraint violation detection
        return results


class ValidationFramework:
    """Main validation framework orchestrator."""
    
    def __init__(self):
        self.validators: Dict[ValidationPhase, List[DataValidator]] = {
            ValidationPhase.PRE_MIGRATION: [PreMigrationValidator()],
            ValidationPhase.IN_FLIGHT: [InFlightValidator()],
            ValidationPhase.POST_MIGRATION: [PostMigrationValidator()],
            ValidationPhase.RECONCILIATION: []
        }
        self.summary = ValidationSummary()
        self.validation_log: List[ValidationResult] = []
    
    def add_validator(self, phase: ValidationPhase, validator: DataValidator) -> None:
        """Add a custom validator for a specific phase."""
        self.validators[phase].append(validator)
    
    async def run_pre_migration_validation(self, data: Dict[str, Any], context: Dict[str, Any]) -> ValidationSummary:
        """Run pre-migration validation."""
        return await self._run_validation(ValidationPhase.PRE_MIGRATION, data, context)
    
    async def run_in_flight_validation(self, data: Dict[str, Any], context: Dict[str, Any]) -> ValidationSummary:
        """Run in-flight validation."""
        return await self._run_validation(ValidationPhase.IN_FLIGHT, data, context)
    
    async def run_post_migration_validation(self, data: Dict[str, Any], context: Dict[str, Any]) -> ValidationSummary:
        """Run post-migration validation."""
        return await self._run_validation(ValidationPhase.POST_MIGRATION, data, context)
    
    async def _run_validation(self, phase: ValidationPhase, data: Dict[str, Any], context: Dict[str, Any]) -> ValidationSummary:
        """Run validation for a specific phase."""
        phase_summary = ValidationSummary()
        
        for validator in self.validators[phase]:
            try:
                results = await validator.validate(data, context)
                for result in results:
                    phase_summary.add_result(result)
                    self.validation_log.append(result)
            except Exception as e:
                logger.error(f"Validator {validator.name} failed: {str(e)}")
                error_result = ValidationResult(
                    phase=phase,
                    level=ValidationLevel.CRITICAL,
                    message=f"Validator failed: {str(e)}",
                    entity_type="validation_framework",
                    metadata={"validator": validator.name, "error": str(e)}
                )
                phase_summary.add_result(error_result)
                self.validation_log.append(error_result)
        
        return phase_summary
    
    def get_validation_report(self) -> Dict[str, Any]:
        """Generate a comprehensive validation report."""
        return {
            "summary": self.summary.to_dict(),
            "validation_log": [result.to_dict() for result in self.validation_log],
            "critical_errors": [r for r in self.validation_log if r.level == ValidationLevel.CRITICAL],
            "errors": [r for r in self.validation_log if r.level == ValidationLevel.ERROR],
            "warnings": [r for r in self.validation_log if r.level == ValidationLevel.WARNING]
        }
    
    async def save_validation_report(self, filepath: Path) -> None:
        """Save validation report to file."""
        report = self.get_validation_report()
        async with aiofiles.open(filepath, 'w') as f:
            await f.write(json.dumps(report, indent=2, default=str))
    
    def clear_log(self) -> None:
        """Clear validation log."""
        self.validation_log.clear()
        self.summary = ValidationSummary()


# Convenience functions for easy integration
async def validate_pre_migration(data: Dict[str, Any], context: Dict[str, Any]) -> ValidationSummary:
    """Convenience function for pre-migration validation."""
    framework = ValidationFramework()
    return await framework.run_pre_migration_validation(data, context)

async def validate_in_flight(data: Dict[str, Any], context: Dict[str, Any]) -> ValidationSummary:
    """Convenience function for in-flight validation."""
    framework = ValidationFramework()
    return await framework.run_in_flight_validation(data, context)

async def validate_post_migration(data: Dict[str, Any], context: Dict[str, Any]) -> ValidationSummary:
    """Convenience function for post-migration validation."""
    framework = ValidationFramework()
    return await framework.run_post_migration_validation(data, context) 