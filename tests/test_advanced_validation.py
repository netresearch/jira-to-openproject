#!/usr/bin/env python3
"""Tests for the Advanced Data Validation Framework."""

import json
from datetime import datetime

import pytest

from src.utils.advanced_validation import (
    InFlightValidator,
    PostMigrationValidator,
    PreMigrationValidator,
    ValidationFramework,
    ValidationLevel,
    ValidationPhase,
    ValidationResult,
    ValidationSummary,
    validate_in_flight,
    validate_post_migration,
    validate_pre_migration,
)


class TestValidationResult:
    """Test ValidationResult class."""

    def test_validation_result_creation(self) -> None:
        """Test creating a ValidationResult."""
        result = ValidationResult(
            phase=ValidationPhase.PRE_MIGRATION,
            level=ValidationLevel.ERROR,
            message="Test error",
            entity_type="project",
            entity_id="PROJ-123",
            field_name="name",
        )

        assert result.phase == ValidationPhase.PRE_MIGRATION
        assert result.level == ValidationLevel.ERROR
        assert result.message == "Test error"
        assert result.entity_type == "project"
        assert result.entity_id == "PROJ-123"
        assert result.field_name == "name"
        assert isinstance(result.timestamp, datetime)

    def test_validation_result_to_dict(self) -> None:
        """Test converting ValidationResult to dictionary."""
        result = ValidationResult(
            phase=ValidationPhase.PRE_MIGRATION,
            level=ValidationLevel.ERROR,
            message="Test error",
            entity_type="project",
            entity_id="PROJ-123",
            field_name="name",
            expected_value="Expected Name",
            actual_value="Actual Name",
        )

        result_dict = result.to_dict()

        assert result_dict["phase"] == "pre_migration"
        assert result_dict["level"] == "error"
        assert result_dict["message"] == "Test error"
        assert result_dict["entity_type"] == "project"
        assert result_dict["entity_id"] == "PROJ-123"
        assert result_dict["field_name"] == "name"
        assert result_dict["expected_value"] == "Expected Name"
        assert result_dict["actual_value"] == "Actual Name"
        assert "timestamp" in result_dict


class TestValidationSummary:
    """Test ValidationSummary class."""

    def test_validation_summary_creation(self) -> None:
        """Test creating a ValidationSummary."""
        summary = ValidationSummary()

        assert summary.total_checks == 0
        assert summary.passed_checks == 0
        assert summary.failed_checks == 0
        assert summary.warnings == 0
        assert summary.errors == 0
        assert summary.critical_errors == 0

    def test_validation_summary_add_result(self) -> None:
        """Test adding results to ValidationSummary."""
        summary = ValidationSummary()

        # Add info result
        info_result = ValidationResult(
            phase=ValidationPhase.PRE_MIGRATION,
            level=ValidationLevel.INFO,
            message="Info message",
            entity_type="test",
        )
        summary.add_result(info_result)

        assert summary.total_checks == 1
        assert summary.passed_checks == 1
        assert summary.failed_checks == 0

        # Add warning result
        warning_result = ValidationResult(
            phase=ValidationPhase.PRE_MIGRATION,
            level=ValidationLevel.WARNING,
            message="Warning message",
            entity_type="test",
        )
        summary.add_result(warning_result)

        assert summary.total_checks == 2
        assert summary.passed_checks == 1
        assert summary.failed_checks == 1
        assert summary.warnings == 1

        # Add error result
        error_result = ValidationResult(
            phase=ValidationPhase.PRE_MIGRATION,
            level=ValidationLevel.ERROR,
            message="Error message",
            entity_type="test",
        )
        summary.add_result(error_result)

        assert summary.total_checks == 3
        assert summary.passed_checks == 1
        assert summary.failed_checks == 2
        assert summary.errors == 1

        # Add critical result
        critical_result = ValidationResult(
            phase=ValidationPhase.PRE_MIGRATION,
            level=ValidationLevel.CRITICAL,
            message="Critical message",
            entity_type="test",
        )
        summary.add_result(critical_result)

        assert summary.total_checks == 4
        assert summary.passed_checks == 1
        assert summary.failed_checks == 3
        assert summary.critical_errors == 1

    def test_validation_summary_success_rate(self) -> None:
        """Test success rate calculation."""
        summary = ValidationSummary()

        # Empty summary
        assert summary.get_success_rate() == 100.0

        # Add some results
        for i in range(10):
            level = ValidationLevel.INFO if i < 7 else ValidationLevel.ERROR
            result = ValidationResult(
                phase=ValidationPhase.PRE_MIGRATION,
                level=level,
                message=f"Test {i}",
                entity_type="test",
            )
            summary.add_result(result)

        assert summary.get_success_rate() == 70.0

    def test_validation_summary_has_critical_errors(self) -> None:
        """Test critical error detection."""
        summary = ValidationSummary()

        assert not summary.has_critical_errors()

        critical_result = ValidationResult(
            phase=ValidationPhase.PRE_MIGRATION,
            level=ValidationLevel.CRITICAL,
            message="Critical error",
            entity_type="test",
        )
        summary.add_result(critical_result)

        assert summary.has_critical_errors()


class TestPreMigrationValidator:
    """Test PreMigrationValidator class."""

    @pytest.mark.asyncio
    async def test_pre_migration_validator_creation(self) -> None:
        """Test creating PreMigrationValidator."""
        validator = PreMigrationValidator()

        assert validator.name == "PreMigrationValidator"
        assert validator.description == "Validates data integrity before migration"

    @pytest.mark.asyncio
    async def test_validate_jira_data_missing_components(self) -> None:
        """Test validation of Jira data with missing components."""
        validator = PreMigrationValidator()

        jira_data = {
            "projects": [],
            "users": [],
            # Missing "issues" and "workflows"
        }

        results = await validator._validate_jira_data(jira_data, {})

        assert len(results) == 2
        assert any(
            r.level == ValidationLevel.CRITICAL and "issues" in r.message
            for r in results
        )
        assert any(
            r.level == ValidationLevel.CRITICAL and "workflows" in r.message
            for r in results
        )

    @pytest.mark.asyncio
    async def test_validate_project_missing_fields(self) -> None:
        """Test validation of project with missing required fields."""
        validator = PreMigrationValidator()

        project = {
            "key": "PROJ-123",
            # Missing "name" and "id"
        }

        results = await validator._validate_project(project)

        assert len(results) == 2
        assert any(r.field_name == "name" for r in results)
        assert any(r.field_name == "id" for r in results)

    @pytest.mark.asyncio
    async def test_validate_project_invalid_key(self) -> None:
        """Test validation of project with invalid key format."""
        validator = PreMigrationValidator()

        project = {"key": "invalid_key_format", "name": "Test Project", "id": "123"}

        results = await validator._validate_project(project)

        assert len(results) == 1
        assert results[0].level == ValidationLevel.ERROR
        assert "Invalid jira_key format" in results[0].message

    @pytest.mark.asyncio
    async def test_validate_user_missing_fields(self) -> None:
        """Test validation of user with missing required fields."""
        validator = PreMigrationValidator()

        user = {
            "username": "testuser",
            # Missing "email"
        }

        results = await validator._validate_user(user)

        assert len(results) == 1
        assert results[0].field_name == "email"

    @pytest.mark.asyncio
    async def test_validate_user_invalid_email(self) -> None:
        """Test validation of user with invalid email format."""
        validator = PreMigrationValidator()

        user = {"username": "testuser", "email": "invalid-email-format"}

        results = await validator._validate_user(user)

        assert len(results) == 1
        assert results[0].level == ValidationLevel.WARNING
        assert "Invalid email format" in results[0].message


class TestInFlightValidator:
    """Test InFlightValidator class."""

    @pytest.mark.asyncio
    async def test_in_flight_validator_creation(self) -> None:
        """Test creating InFlightValidator."""
        validator = InFlightValidator()

        assert validator.name == "InFlightValidator"
        assert validator.description == "Validates data during migration execution"
        assert len(validator.processed_entities) == 0
        assert len(validator.entity_hashes) == 0

    @pytest.mark.asyncio
    async def test_validate_duplicate_processing(self) -> None:
        """Test detection of duplicate entity processing."""
        validator = InFlightValidator()

        data = {"id": "PROJ-123", "type": "project", "name": "Test Project"}

        context = {}

        # First validation
        results1 = await validator.validate(data, context)
        assert len(results1) == 0  # No issues on first run

        # Second validation of same entity
        results2 = await validator.validate(data, context)
        assert len(results2) == 1
        assert results2[0].level == ValidationLevel.WARNING
        assert "processed multiple times" in results2[0].message

    @pytest.mark.asyncio
    async def test_validate_data_integrity_missing_fields(self) -> None:
        """Test validation of data integrity with missing required fields."""
        validator = InFlightValidator()

        data = {
            "id": "PROJ-123",
            "type": "project",
            # Missing required fields
        }

        context = {"required_fields": ["name", "description"]}

        results = await validator._validate_data_integrity(data, context)

        assert len(results) == 2
        assert any(r.field_name == "name" for r in results)
        assert any(r.field_name == "description" for r in results)

    @pytest.mark.asyncio
    async def test_validate_data_integrity_type_mismatch(self) -> None:
        """Test validation of data integrity with type mismatches."""
        validator = InFlightValidator()

        data = {
            "id": "PROJ-123",
            "type": "project",
            "name": "Test Project",
            "priority": "high",  # Should be integer
        }

        context = {"field_types": {"name": "str", "priority": "int"}}

        results = await validator._validate_data_integrity(data, context)

        assert len(results) == 1
        assert results[0].field_name == "priority"
        assert "type mismatch" in results[0].message


class TestPostMigrationValidator:
    """Test PostMigrationValidator class."""

    @pytest.mark.asyncio
    async def test_post_migration_validator_creation(self) -> None:
        """Test creating PostMigrationValidator."""
        validator = PostMigrationValidator()

        assert validator.name == "PostMigrationValidator"
        assert validator.description == "Validates data after migration completion"

    @pytest.mark.asyncio
    async def test_validate_completeness_mismatch(self) -> None:
        """Test validation of completeness with count mismatch."""
        validator = PostMigrationValidator()

        data = {"migrated_entities": [{"id": "1"}, {"id": "2"}]}  # 2 entities

        context = {"expected_count": 5}  # Expected 5 entities

        results = await validator._validate_completeness(data, context)

        assert len(results) == 1
        assert results[0].level == ValidationLevel.ERROR
        assert "completeness mismatch" in results[0].message
        assert results[0].metadata["expected_count"] == 5
        assert results[0].metadata["actual_count"] == 2

    @pytest.mark.asyncio
    async def test_validate_accuracy_mismatch(self) -> None:
        """Test validation of accuracy with data mismatches."""
        validator = PostMigrationValidator()

        source_data = {
            "PROJ-123": {
                "name": "Original Name",
                "description": "Original Description",
                "status": "active",
            },
        }

        target_data = [
            {
                "source_id": "PROJ-123",
                "id": "456",
                "name": "Different Name",  # Mismatch
                "description": "Original Description",
                "status": "active",
            },
        ]

        data = {"migrated_entities": target_data}

        context = {"source_data": source_data}

        results = await validator._validate_accuracy(data, context)

        assert len(results) == 1
        assert results[0].level == ValidationLevel.ERROR
        assert "Data accuracy mismatch" in results[0].message
        assert results[0].field_name == "name"
        assert results[0].expected_value == "Original Name"
        assert results[0].actual_value == "Different Name"


class TestValidationFramework:
    """Test ValidationFramework class."""

    @pytest.mark.asyncio
    async def test_validation_framework_creation(self) -> None:
        """Test creating ValidationFramework."""
        framework = ValidationFramework()

        assert ValidationPhase.PRE_MIGRATION in framework.validators
        assert ValidationPhase.IN_FLIGHT in framework.validators
        assert ValidationPhase.POST_MIGRATION in framework.validators
        assert ValidationPhase.RECONCILIATION in framework.validators

    @pytest.mark.asyncio
    async def test_add_validator(self) -> None:
        """Test adding a custom validator."""
        framework = ValidationFramework()

        custom_validator = PreMigrationValidator()
        framework.add_validator(ValidationPhase.PRE_MIGRATION, custom_validator)

        assert len(framework.validators[ValidationPhase.PRE_MIGRATION]) == 2

    @pytest.mark.asyncio
    async def test_run_pre_migration_validation(self) -> None:
        """Test running pre-migration validation."""
        framework = ValidationFramework()

        data = {
            "jira_data": {
                "projects": [{"key": "PROJ-123", "name": "Test Project", "id": "123"}],
                "users": [{"username": "testuser", "email": "test@example.com"}],
            },
            "openproject_config": {
                "url": "http://localhost:8080",
                "api_key": "test_key",
                "project_id": "123",
            },
        }

        context = {}

        summary = await framework.run_pre_migration_validation(data, context)

        assert summary.total_checks > 0
        assert summary.passed_checks > 0

    @pytest.mark.asyncio
    async def test_get_validation_report(self) -> None:
        """Test generating validation report."""
        framework = ValidationFramework()

        # Add some test results
        result = ValidationResult(
            phase=ValidationPhase.PRE_MIGRATION,
            level=ValidationLevel.ERROR,
            message="Test error",
            entity_type="test",
        )
        framework.validation_log.append(result)
        framework.summary.add_result(result)

        report = framework.get_validation_report()

        assert "summary" in report
        assert "validation_log" in report
        assert "critical_errors" in report
        assert "errors" in report
        assert "warnings" in report

    @pytest.mark.asyncio
    async def test_save_validation_report(self, tmp_path) -> None:
        """Test saving validation report to file."""
        framework = ValidationFramework()

        # Add some test results
        result = ValidationResult(
            phase=ValidationPhase.PRE_MIGRATION,
            level=ValidationLevel.ERROR,
            message="Test error",
            entity_type="test",
        )
        framework.validation_log.append(result)
        framework.summary.add_result(result)

        filepath = tmp_path / "validation_report.json"
        await framework.save_validation_report(filepath)

        assert filepath.exists()

        # Verify file content
        with open(filepath) as f:
            content = json.load(f)

        assert "summary" in content
        assert "validation_log" in content


class TestConvenienceFunctions:
    """Test convenience functions."""

    @pytest.mark.asyncio
    async def test_validate_pre_migration(self) -> None:
        """Test validate_pre_migration convenience function."""
        data = {
            "jira_data": {
                "projects": [{"key": "PROJ-123", "name": "Test Project", "id": "123"}],
            },
        }

        context = {}

        summary = await validate_pre_migration(data, context)

        assert isinstance(summary, ValidationSummary)
        assert summary.total_checks > 0

    @pytest.mark.asyncio
    async def test_validate_in_flight(self) -> None:
        """Test validate_in_flight convenience function."""
        data = {"id": "PROJ-123", "type": "project", "name": "Test Project"}

        context = {}

        summary = await validate_in_flight(data, context)

        assert isinstance(summary, ValidationSummary)

    @pytest.mark.asyncio
    async def test_validate_post_migration(self) -> None:
        """Test validate_post_migration convenience function."""
        data = {"migrated_entities": [{"id": "456", "name": "Test Project"}]}

        context = {"expected_count": 1}

        summary = await validate_post_migration(data, context)

        assert isinstance(summary, ValidationSummary)


if __name__ == "__main__":
    pytest.main([__file__])
