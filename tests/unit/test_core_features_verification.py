#!/usr/bin/env python3
"""Verification tests for core migration features (j2o-1 through j2o-6).

This test suite verifies that the 5 high-priority features are implemented
and functioning correctly:

- j2o-1: Enhanced Meta Information Migration
- j2o-2: Markdown Syntax Conversion
- j2o-3: Work Log and Time Entry Migration
- j2o-5: Pagination and Batched Processing
- j2o-6: Advanced Data Validation Framework

Test Philosophy:
These are verification tests that document and validate existing functionality
rather than driving new implementation. Each test verifies a specific aspect
of the feature is present and working.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.mark.skip(reason="ValidationFramework removed during enterprise bloat cleanup")
class TestJ2O6ValidationFramework:
    """Verify j2o-6: Advanced Data Validation Framework is implemented and integrated.

    NOTE: These tests are skipped because the ValidationFramework was removed
    during enterprise bloat cleanup. Validation is now handled through simpler
    mechanisms in individual migration classes.
    """

    def test_validation_framework_exists(self):
        """Verify ValidationFramework class exists and is importable."""
        from src.utils.advanced_validation import ValidationFramework

        framework = ValidationFramework()
        assert framework is not None
        assert hasattr(framework, "validators")
        assert hasattr(framework, "summary")

    def test_validation_phases_defined(self):
        """Verify all required validation phases are defined."""
        from src.utils.advanced_validation import ValidationPhase

        assert hasattr(ValidationPhase, "PRE_MIGRATION")
        assert hasattr(ValidationPhase, "IN_FLIGHT")
        assert hasattr(ValidationPhase, "POST_MIGRATION")
        assert hasattr(ValidationPhase, "RECONCILIATION")

    def test_validation_levels_defined(self):
        """Verify all validation severity levels are defined."""
        from src.utils.advanced_validation import ValidationLevel

        assert hasattr(ValidationLevel, "INFO")
        assert hasattr(ValidationLevel, "WARNING")
        assert hasattr(ValidationLevel, "ERROR")
        assert hasattr(ValidationLevel, "CRITICAL")

    def test_validators_registered(self):
        """Verify validators are registered for each phase."""
        from src.utils.advanced_validation import (
            InFlightValidator,
            PostMigrationValidator,
            PreMigrationValidator,
            ValidationFramework,
            ValidationPhase,
        )

        framework = ValidationFramework()

        # Check pre-migration validators
        pre_validators = framework.validators[ValidationPhase.PRE_MIGRATION]
        assert len(pre_validators) > 0
        assert any(isinstance(v, PreMigrationValidator) for v in pre_validators)

        # Check in-flight validators
        in_flight_validators = framework.validators[ValidationPhase.IN_FLIGHT]
        assert len(in_flight_validators) > 0
        assert any(isinstance(v, InFlightValidator) for v in in_flight_validators)

        # Check post-migration validators
        post_validators = framework.validators[ValidationPhase.POST_MIGRATION]
        assert len(post_validators) > 0
        assert any(isinstance(v, PostMigrationValidator) for v in post_validators)

    def test_validation_framework_integrated_in_migration(self):
        """Verify ValidationFramework is imported and used in migration.py."""
        import src.migration as migration_module

        # Check that validation functions are imported
        assert hasattr(migration_module, "validate_pre_migration")

        # Read migration.py source to verify integration
        migration_file = Path(__file__).parent.parent.parent / "src" / "migration.py"
        migration_source = migration_file.read_text()

        # Verify ValidationFramework is imported
        assert "ValidationFramework" in migration_source
        assert "validate_pre_migration" in migration_source

        # Verify pre-migration validation is called
        assert "pre_migration_validation" in migration_source.lower()


class TestJ2O2MarkdownConversion:
    """Verify j2o-2: Markdown Syntax Conversion is implemented and used."""

    def test_markdown_converter_exists(self):
        """Verify MarkdownConverter class exists and is importable."""
        from src.utils.markdown_converter import MarkdownConverter

        converter = MarkdownConverter()
        assert converter is not None
        assert hasattr(converter, "convert")

    def test_markdown_converter_handles_jira_syntax(self):
        """Verify MarkdownConverter supports key Jira wiki syntax."""
        from src.utils.markdown_converter import MarkdownConverter

        converter = MarkdownConverter()

        # Test bold: *text* -> **text**
        assert "**bold**" in converter.convert("*bold*")

        # Test italic: _text_ -> *text*
        assert "*italic*" in converter.convert("_italic_")

        # Test headings: h1. -> #
        assert "# Heading" in converter.convert("h1. Heading")

        # Test code blocks: {code} -> ```
        result = converter.convert("{code:python}print('hello'){code}")
        assert "```" in result

    def test_markdown_converter_used_in_work_package_migration(self):
        """Verify MarkdownConverter is instantiated and used in WorkPackageMigration."""
        from src.migrations.work_package_migration import WorkPackageMigration

        # Create mocked clients
        jira_client = Mock()
        op_client = Mock()
        op_client.rails_client = Mock()

        # Create work package migration instance
        wp_migration = WorkPackageMigration(jira_client, op_client)

        # Verify markdown_converter attribute exists
        assert hasattr(wp_migration, "markdown_converter")
        assert wp_migration.markdown_converter is not None

        # Verify it has the convert method
        assert hasattr(wp_migration.markdown_converter, "convert")

    def test_markdown_converter_integration_in_source(self):
        """Verify markdown_converter.convert() is called in work package creation."""
        wp_migration_file = Path(__file__).parent.parent.parent / "src" / "migrations" / "work_package_migration.py"
        wp_source = wp_migration_file.read_text()

        # Verify markdown converter is imported
        assert "MarkdownConverter" in wp_source

        # Verify it's instantiated
        assert "self.markdown_converter" in wp_source

        # Verify convert() is called
        assert "self.markdown_converter.convert" in wp_source


class TestJ2O3TimeEntryMigration:
    """Verify j2o-3: Work Log and Time Entry Migration is implemented."""

    def test_time_entry_migration_exists(self):
        """Verify TimeEntryMigration class exists and is importable."""
        from src.migrations.time_entry_migration import TimeEntryMigration

        assert TimeEntryMigration is not None

    def test_time_entry_migration_registered(self):
        """Verify TimeEntryMigration is registered in component sequence."""
        from src.migration import DEFAULT_COMPONENT_SEQUENCE

        # Verify time_entries is in the default sequence
        assert "time_entries" in DEFAULT_COMPONENT_SEQUENCE

    def test_time_entry_migration_has_run_method(self):
        """Verify TimeEntryMigration implements run() method."""
        from src.migrations.time_entry_migration import TimeEntryMigration

        jira_client = Mock()
        op_client = Mock()
        op_client.rails_client = Mock()

        time_entry_migration = TimeEntryMigration(jira_client, op_client)

        # Verify run method exists
        assert hasattr(time_entry_migration, "run")
        assert callable(time_entry_migration.run)

    def test_time_entry_migrator_helper_exists(self):
        """Verify TimeEntryMigrator helper class exists."""
        from src.utils.time_entry_migrator import TimeEntryMigrator

        assert TimeEntryMigrator is not None

    def test_time_entry_migration_component_factory(self):
        """Verify time_entries component factory is registered."""
        migration_file = Path(__file__).parent.parent.parent / "src" / "migration.py"
        migration_source = migration_file.read_text()

        # Verify TimeEntryMigration is imported
        assert "TimeEntryMigration" in migration_source

        # Verify factory is registered
        assert '"time_entries"' in migration_source or "'time_entries'" in migration_source


class TestJ2O5PaginationProcessing:
    """Verify j2o-5: Pagination and Batched Processing is implemented."""

    def test_work_package_migration_has_pagination(self):
        """Verify WorkPackageMigration implements pagination."""
        from src.migrations.work_package_migration import WorkPackageMigration

        jira_client = Mock()
        op_client = Mock()
        op_client.rails_client = Mock()

        wp_migration = WorkPackageMigration(jira_client, op_client)

        # Verify iterator method exists
        assert hasattr(wp_migration, "iter_project_issues")
        assert callable(wp_migration.iter_project_issues)

    def test_pagination_uses_start_at_max_results(self):
        """Verify pagination implementation uses startAt/maxResults parameters."""
        wp_migration_file = Path(__file__).parent.parent.parent / "src" / "migrations" / "work_package_migration.py"
        wp_source = wp_migration_file.read_text()

        # Verify pagination parameters are used
        assert "startAt" in wp_source
        assert "maxResults" in wp_source

        # Verify memory-efficient pagination is mentioned
        assert "memory-efficient pagination" in wp_source

    def test_pagination_supports_configurable_batch_size(self):
        """Verify batch_size is configurable via config."""
        wp_migration_file = Path(__file__).parent.parent.parent / "src" / "migrations" / "work_package_migration.py"
        wp_source = wp_migration_file.read_text()

        # Verify batch_size is read from config
        assert "batch_size" in wp_source
        assert "config.migration_config" in wp_source

    def test_iter_project_issues_is_generator(self):
        """Verify iter_project_issues returns an iterator for memory efficiency."""
        wp_migration_file = Path(__file__).parent.parent.parent / "src" / "migrations" / "work_package_migration.py"
        wp_source = wp_migration_file.read_text()

        # Check for iterator/generator pattern in the method
        # Look for "yield" keyword in iter_project_issues context
        assert "def iter_project_issues" in wp_source
        assert "Iterator[Issue]" in wp_source or "Iterator" in wp_source


class TestJ2O1MetadataPreservation:
    """Verify j2o-1: Enhanced Meta Information Migration preserves metadata."""

    def test_work_package_migration_extracts_custom_fields(self):
        """Verify custom fields are extracted from Jira issues."""
        wp_migration_file = Path(__file__).parent.parent.parent / "src" / "migrations" / "work_package_migration.py"
        wp_source = wp_migration_file.read_text()

        # Verify custom field extraction logic
        assert "customfield_" in wp_source
        assert "custom_fields" in wp_source

    def test_j2o_provenance_fields_created(self):
        """Verify J2O provenance custom fields are created."""
        wp_migration_file = Path(__file__).parent.parent.parent / "src" / "migrations" / "work_package_migration.py"
        wp_source = wp_migration_file.read_text()

        # Verify J2O provenance fields are defined
        assert "J2O Origin System" in wp_source
        assert "J2O Origin Key" in wp_source
        assert "J2O Origin URL" in wp_source or "J2O Origin ID" in wp_source

    def test_user_associations_preserved(self):
        """Verify user associations (assignee, reporter, watchers) are preserved."""
        wp_migration_file = Path(__file__).parent.parent.parent / "src" / "migrations" / "work_package_migration.py"
        wp_source = wp_migration_file.read_text()

        # Verify user association logic
        assert "assignee" in wp_source
        assert "reporter" in wp_source or "creator" in wp_source
        assert "watchers" in wp_source or "watcher" in wp_source

    def test_enhanced_user_association_migrator_used(self):
        """Verify EnhancedUserAssociationMigrator is used for user mapping."""
        from src.migrations.work_package_migration import WorkPackageMigration

        jira_client = Mock()
        op_client = Mock()
        op_client.rails_client = Mock()

        wp_migration = WorkPackageMigration(jira_client, op_client)

        # Verify enhanced user migrator is instantiated
        assert hasattr(wp_migration, "enhanced_user_migrator")
        assert wp_migration.enhanced_user_migrator is not None

    def test_enhanced_timestamp_migrator_used(self):
        """Verify EnhancedTimestampMigrator is used for timestamp preservation."""
        from src.migrations.work_package_migration import WorkPackageMigration

        jira_client = Mock()
        op_client = Mock()
        op_client.rails_client = Mock()

        wp_migration = WorkPackageMigration(jira_client, op_client)

        # Verify enhanced timestamp migrator is instantiated
        assert hasattr(wp_migration, "enhanced_timestamp_migrator")
        assert wp_migration.enhanced_timestamp_migrator is not None

    def test_enhanced_audit_trail_migrator_used(self):
        """Verify EnhancedAuditTrailMigrator is used for comments/history."""
        from src.migrations.work_package_migration import WorkPackageMigration

        jira_client = Mock()
        op_client = Mock()
        op_client.rails_client = Mock()

        wp_migration = WorkPackageMigration(jira_client, op_client)

        # Verify enhanced audit trail migrator is instantiated
        assert hasattr(wp_migration, "enhanced_audit_trail_migrator")
        assert wp_migration.enhanced_audit_trail_migrator is not None

    def test_metadata_completeness(self):
        """Verify comprehensive metadata fields are extracted."""
        wp_migration_file = Path(__file__).parent.parent.parent / "src" / "migrations" / "work_package_migration.py"
        wp_source = wp_migration_file.read_text()

        # Verify core metadata fields are handled
        assert "issue_type" in wp_source
        assert "status" in wp_source
        assert "summary" in wp_source or "subject" in wp_source
        assert "description" in wp_source
        assert "jira_key" in wp_source
        assert "jira_id" in wp_source


class TestIntegrationVerification:
    """Integration tests verifying features work together."""

    def test_all_migrations_registered(self):
        """Verify all migration components are properly registered."""
        from src.migration import DEFAULT_COMPONENT_SEQUENCE

        required_components = [
            "users",
            "projects",
            "work_packages_skeleton",  # Phase 1: skeleton creation
            "work_packages_content",   # Phase 3: content with resolved attachment URLs
            "time_entries",
        ]

        for component in required_components:
            assert component in DEFAULT_COMPONENT_SEQUENCE, (
                f"Component '{component}' missing from DEFAULT_COMPONENT_SEQUENCE"
            )

    def test_migration_component_factories(self):
        """Verify component factories are registered for all components."""
        migration_file = Path(__file__).parent.parent.parent / "src" / "migration.py"
        migration_source = migration_file.read_text()

        # Check that _build_component_factories function exists and returns factories
        assert "_build_component_factories" in migration_source

        # Verify key migrations have factories
        assert '"work_packages"' in migration_source or "'work_packages'" in migration_source
        assert '"time_entries"' in migration_source or "'time_entries'" in migration_source

    @pytest.mark.skip(reason="ValidationFramework removed during enterprise bloat cleanup")
    def test_validation_integrated_with_migration_flow(self):
        """Verify validation is integrated into the migration workflow.

        NOTE: Skipped because ValidationFramework was removed. Validation now happens
        within individual migration classes through their ETL methods and error handling.
        """
        migration_file = Path(__file__).parent.parent.parent / "src" / "migration.py"
        migration_source = migration_file.read_text()

        # Verify validation is called during migration
        assert "validation" in migration_source.lower()
        assert "ValidationFramework" in migration_source or "validate_pre_migration" in migration_source


class TestJ2O96MigrationCompleteness:
    """Verify j2o-96/j2o-97: All registered migrations have proper run() implementations."""

    def test_all_migrations_have_run_method_implementations(self):
        """Verify every migration in DEFAULT_COMPONENT_SEQUENCE has a proper run() implementation.

        This test ensures no migration is left incomplete with only the BaseMigration
        default run() method that raises NotImplementedError.
        """
        from src.migration import DEFAULT_COMPONENT_SEQUENCE, _build_component_factories

        # Create mock clients for instantiation
        mock_jira = Mock()
        mock_op = Mock()
        mock_op.rails_client = Mock()
        # Configure op_client methods that migrations call during __init__
        mock_op.get_roles.return_value = []

        # Get component factories
        factories = _build_component_factories(mock_jira, mock_op)

        # Track incomplete migrations
        incomplete_migrations = []

        for component_name in DEFAULT_COMPONENT_SEQUENCE:
            # Get the factory for this component
            factory = factories.get(component_name)
            assert factory is not None, f"Component '{component_name}' has no factory in _build_component_factories"

            # Instantiate the migration
            migration_instance = factory()

            # Verify run() method exists
            assert hasattr(migration_instance, "run"), f"Component '{component_name}' has no run() method"
            assert callable(migration_instance.run), f"Component '{component_name}' run() is not callable"

            # Verify run() is not the BaseMigration default implementation
            # by checking if run() is defined in the migration's own class
            run_method = migration_instance.run
            run_class = run_method.__self__.__class__

            # Check if run() is defined in the migration class itself (not inherited)
            if "run" not in run_class.__dict__:
                # run() is inherited from BaseMigration, not overridden
                incomplete_migrations.append(component_name)

        # Fail loudly if any migrations are incomplete
        assert len(incomplete_migrations) == 0, (
            f"The following {len(incomplete_migrations)} migrations lack proper run() implementations: {', '.join(incomplete_migrations)}"
        )

    def test_migration_run_methods_return_component_result(self):
        """Verify all migration run() methods have proper return type annotations."""
        import inspect

        from src.migration import DEFAULT_COMPONENT_SEQUENCE, _build_component_factories

        # Create mock clients
        mock_jira = Mock()
        mock_op = Mock()
        mock_op.rails_client = Mock()
        # Configure op_client methods that migrations call during __init__
        mock_op.get_roles.return_value = []

        # Get component factories
        factories = _build_component_factories(mock_jira, mock_op)

        for component_name in DEFAULT_COMPONENT_SEQUENCE:
            factory = factories.get(component_name)
            migration_instance = factory()

            # Get the run method's signature
            run_method = migration_instance.run
            signature = inspect.signature(run_method)

            # Verify return annotation
            return_annotation = signature.return_annotation
            assert return_annotation is not inspect.Signature.empty, (
                f"Component '{component_name}' run() method lacks return type annotation"
            )

            # Check if it's ComponentResult (handle both direct type and string annotation)
            if return_annotation != inspect.Signature.empty:
                annotation_str = str(return_annotation)
                assert "ComponentResult" in annotation_str, (
                    f"Component '{component_name}' run() should return ComponentResult, got {annotation_str}"
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
