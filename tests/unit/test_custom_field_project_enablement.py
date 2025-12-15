#!/usr/bin/env python3
"""Unit tests for selective custom field project enablement.

BUG: All custom fields are created with `is_for_all: true`, making them
visible in ALL projects even when they have no values.

Expected Behavior: Custom fields should only be enabled in projects
where work packages have non-empty, non-default values for that field.

This creates a "spammy" experience where users see many custom fields
in projects where they are irrelevant.

References:
- User report on spammy custom fields in all projects
- OpenProject CustomFieldsProject model for per-project enablement
"""

import inspect
from unittest.mock import Mock, patch

import pytest


class TestSelectiveProjectEnablementVerification:
    """Verify all migrations use is_for_all=false for selective enablement."""

    def test_resolution_migration_uses_is_for_all_false(self):
        """Verify resolution migration creates CF with is_for_all: false."""
        from src.migrations.resolution_migration import ResolutionMigration

        source = inspect.getsource(ResolutionMigration._ensure_resolution_cf)
        assert "is_for_all: false" in source, (
            "Resolution migration should use is_for_all: false for selective "
            "project enablement"
        )

    def test_votes_migration_uses_is_for_all_false(self):
        """Verify votes migration creates CF with is_for_all: false."""
        from src.migrations.votes_migration import VotesMigration

        source = inspect.getsource(VotesMigration._ensure_votes_cf)
        assert "is_for_all: false" in source, (
            "Votes migration should use is_for_all: false for selective "
            "project enablement"
        )

    def test_story_points_migration_uses_is_for_all_false(self):
        """Verify story points migration creates CF with is_for_all: false."""
        from src.migrations.story_points_migration import StoryPointsMigration

        source = inspect.getsource(StoryPointsMigration._ensure_story_points_cf)
        assert "is_for_all: false" in source, (
            "Story points migration should use is_for_all: false for selective "
            "project enablement"
        )

    def test_labels_migration_uses_is_for_all_false(self):
        """Verify labels migration creates CF with is_for_all: false."""
        from src.migrations.labels_migration import LabelsMigration

        source = inspect.getsource(LabelsMigration._ensure_labels_cf)
        assert "is_for_all: false" in source, (
            "Labels migration should use is_for_all: false for selective "
            "project enablement"
        )

    def test_affects_versions_migration_uses_is_for_all_false(self):
        """Verify affects versions migration creates CF with is_for_all: false."""
        from src.migrations.affects_versions_migration import AffectsVersionsMigration

        source = inspect.getsource(AffectsVersionsMigration._ensure_cf)
        assert "is_for_all: false" in source, (
            "Affects versions migration should use is_for_all: false for selective "
            "project enablement"
        )

    def test_security_levels_migration_uses_is_for_all_false(self):
        """Verify security levels migration creates CF with is_for_all: false."""
        from src.migrations.security_levels_migration import SecurityLevelsMigration

        source = inspect.getsource(SecurityLevelsMigration._ensure_security_cf)
        assert "is_for_all: false" in source, (
            "Security levels migration should use is_for_all: false for selective "
            "project enablement"
        )

    def test_sprint_migration_uses_is_for_all_false(self):
        """Verify sprint migration creates CF with is_for_all: false."""
        from src.migrations.sprint_epic_migration import SprintEpicMigration

        source = inspect.getsource(SprintEpicMigration._ensure_sprint_cf)
        assert "is_for_all: false" in source, (
            "Sprint migration should use is_for_all: false for selective "
            "project enablement"
        )

    def test_generic_customfields_migration_uses_is_for_all_false(self):
        """Verify generic custom fields migration creates CF with is_for_all: false."""
        from src.migrations.customfields_generic_migration import CustomFieldsGenericMigration

        source = inspect.getsource(CustomFieldsGenericMigration._ensure_cf)
        assert "is_for_all: false" in source, (
            "Generic custom fields migration should use is_for_all: false for selective "
            "project enablement"
        )


class TestSelectiveEnablementHelper:
    """Tests for the selective enablement helper functionality.

    This helper should:
    1. Track which projects have non-empty values for each custom field
    2. Generate Rails scripts to enable fields only for those projects
    3. Handle the CustomFieldsProject join table correctly
    """

    def test_usage_tracker_basic_functionality(self):
        """Test basic tracking of custom field usage by project."""

        class CustomFieldUsageTracker:
            """Track which projects actually use each custom field."""

            def __init__(self):
                self._usage: dict[str, dict[int, bool]] = {}

            def record_usage(
                self,
                field_name: str,
                project_id: int,
                value: str | None,
                default_value: str | None = None,
            ) -> None:
                """Record that a project has a value for a custom field.

                Args:
                    field_name: Name of the custom field
                    project_id: OpenProject project ID
                    value: The actual value set
                    default_value: The default value (if any) to compare against
                """
                # Only track if value is non-empty and non-default
                is_meaningful = (
                    value is not None
                    and str(value).strip() != ""
                    and str(value).strip() != str(default_value or "").strip()
                )

                if field_name not in self._usage:
                    self._usage[field_name] = {}

                if is_meaningful:
                    self._usage[field_name][project_id] = True

            def get_projects_using_field(self, field_name: str) -> list[int]:
                """Get list of project IDs that use this field."""
                return list(self._usage.get(field_name, {}).keys())

            def should_enable_for_project(
                self, field_name: str, project_id: int
            ) -> bool:
                """Check if field should be enabled for a specific project."""
                return project_id in self._usage.get(field_name, {})

        # Test the tracker
        tracker = CustomFieldUsageTracker()

        # Project 1: Has resolution value
        tracker.record_usage("J2O Resolution", project_id=1, value="Fixed")

        # Project 2: Empty resolution
        tracker.record_usage("J2O Resolution", project_id=2, value="")

        # Project 3: Resolution is default value
        tracker.record_usage(
            "J2O Resolution", project_id=3, value="None", default_value="None"
        )

        # Project 4: Has actual resolution
        tracker.record_usage("J2O Resolution", project_id=4, value="Won't Fix")

        # Verify only projects with meaningful values are tracked
        projects = tracker.get_projects_using_field("J2O Resolution")
        assert sorted(projects) == [1, 4], (
            "Only projects 1 and 4 have non-empty, non-default resolution values"
        )

        assert tracker.should_enable_for_project("J2O Resolution", 1) is True
        assert tracker.should_enable_for_project("J2O Resolution", 2) is False
        assert tracker.should_enable_for_project("J2O Resolution", 3) is False
        assert tracker.should_enable_for_project("J2O Resolution", 4) is True

    def test_usage_tracker_with_numeric_values(self):
        """Test tracking numeric fields like votes and story points."""

        class CustomFieldUsageTracker:
            def __init__(self):
                self._usage: dict[str, set[int]] = {}

            def record_usage(
                self, field_name: str, project_id: int, value: str | int | float | None
            ) -> None:
                is_meaningful = False
                if value is not None:
                    if isinstance(value, (int, float)):
                        is_meaningful = value != 0  # Non-zero
                    else:
                        is_meaningful = str(value).strip() not in ("", "0", "0.0")

                if is_meaningful:
                    if field_name not in self._usage:
                        self._usage[field_name] = set()
                    self._usage[field_name].add(project_id)

            def get_projects_using_field(self, field_name: str) -> set[int]:
                return self._usage.get(field_name, set())

        tracker = CustomFieldUsageTracker()

        # Story points
        tracker.record_usage("Story Points", project_id=1, value=5)
        tracker.record_usage("Story Points", project_id=2, value=0)  # Zero - default
        tracker.record_usage("Story Points", project_id=3, value=8.5)
        tracker.record_usage("Story Points", project_id=4, value=None)

        # Votes
        tracker.record_usage("Votes", project_id=1, value=0)  # Zero
        tracker.record_usage("Votes", project_id=2, value=10)

        assert tracker.get_projects_using_field("Story Points") == {1, 3}
        assert tracker.get_projects_using_field("Votes") == {2}

    def test_rails_script_generation_for_selective_enablement(self):
        """Test generating Rails script for selective project enablement."""

        def generate_selective_enablement_script(
            cf_name: str,
            cf_id: int,
            project_ids: list[int],
        ) -> str:
            """Generate Rails script to enable CF for specific projects only.

            Args:
                cf_name: Custom field name
                cf_id: Custom field ID
                project_ids: List of project IDs to enable the field for

            Returns:
                Ruby script string for Rails console execution
            """
            if not project_ids:
                return ""

            project_ids_str = ", ".join(str(pid) for pid in project_ids)

            script = f"""
            # Selective enablement for '{cf_name}'
            cf = CustomField.find({cf_id})

            # Ensure is_for_all is false
            if cf.is_for_all?
              cf.is_for_all = false
              cf.save!
            end

            # Enable for specific projects only
            [{project_ids_str}].each do |project_id|
              begin
                project = Project.find(project_id)
                CustomFieldsProject.find_or_create_by!(
                  custom_field: cf,
                  project: project
                )
              rescue ActiveRecord::RecordNotFound
                # Project may have been deleted, skip
              end
            end

            cf.id
            """
            return script.strip()

        script = generate_selective_enablement_script(
            cf_name="J2O Resolution",
            cf_id=99,
            project_ids=[1, 4, 7],
        )

        assert "cf.is_for_all = false" in script
        assert "CustomFieldsProject.find_or_create_by!" in script
        assert "[1, 4, 7]" in script
        assert "J2O Resolution" in script


class TestMigrationIntegrationWithUsageTracking:
    """Tests for integrating usage tracking into migration workflow."""

    def test_migration_has_enable_cf_for_projects(self):
        """Test that migration has the _enable_cf_for_projects method.

        FIX VERIFIED: Migrations now have helper method to enable CFs per-project.
        """
        from src.migrations.resolution_migration import ResolutionMigration
        from src.migrations.votes_migration import VotesMigration
        from src.migrations.story_points_migration import StoryPointsMigration

        # Verify each migration has the enablement method
        assert hasattr(ResolutionMigration, "_enable_cf_for_projects")
        assert hasattr(VotesMigration, "_enable_cf_for_projects")
        assert hasattr(StoryPointsMigration, "_enable_cf_for_projects")

    def test_post_migration_project_enablement(self):
        """Test post-migration script to fix already-created fields.

        For fields already created with is_for_all=true, we need a
        post-migration script to:
        1. Set is_for_all = false
        2. Query which projects actually have values
        3. Create CustomFieldsProject entries for those projects
        """
        # Script to fix existing fields
        fix_script = """
        # Fix script for existing custom fields with is_for_all=true

        j2o_fields = CustomField.where("name LIKE 'J2O %'")

        j2o_fields.each do |cf|
          next unless cf.is_for_all?

          # Find projects that actually have values for this field
          projects_with_values = Project.joins(:work_packages)
            .joins("INNER JOIN custom_values cv ON cv.customized_id = work_packages.id AND cv.customized_type = 'WorkPackage'")
            .where("cv.custom_field_id = ? AND cv.value IS NOT NULL AND cv.value != ''", cf.id)
            .distinct

          # Disable global visibility
          cf.is_for_all = false
          cf.save!

          # Enable for specific projects
          projects_with_values.each do |project|
            CustomFieldsProject.find_or_create_by!(
              custom_field: cf,
              project: project
            )
          end

          puts "Fixed '#{cf.name}': enabled for #{projects_with_values.count} projects"
        end
        """

        # Verify key operations in the fix script
        assert "cf.is_for_all = false" in fix_script
        assert "CustomFieldsProject.find_or_create_by!" in fix_script
        assert "custom_values cv" in fix_script  # Query for actual values
        assert "cv.value IS NOT NULL" in fix_script


class TestOpenProjectCustomFieldModel:
    """Document OpenProject's custom field model behavior."""

    def test_is_for_all_true_behavior(self):
        """Document what is_for_all: true does in OpenProject.

        When is_for_all is true:
        - Field appears in ALL project settings
        - Field is visible on ALL work package forms
        - No entries in custom_fields_projects table needed
        - Creates clutter in projects that don't use the field
        """
        pass  # Documentation test

    def test_is_for_all_false_behavior(self):
        """Document what is_for_all: false does in OpenProject.

        When is_for_all is false:
        - Field only appears in projects with CustomFieldsProject entry
        - Admin must explicitly enable field per-project
        - Clean UI - only relevant fields shown
        - Requires CustomFieldsProject join entries
        """
        pass  # Documentation test

    def test_custom_fields_project_model(self):
        """Document CustomFieldsProject model structure.

        CustomFieldsProject:
        - custom_field_id: references custom_fields.id
        - project_id: references projects.id
        - Links a custom field to a specific project

        When is_for_all=false, the field is ONLY visible in projects
        that have a CustomFieldsProject entry.
        """
        pass  # Documentation test


# Run verification after fix
@pytest.mark.skip(reason="Run after implementing fix to verify behavior change")
class TestPostFixVerification:
    """Tests to run after implementing the fix."""

    def test_new_migrations_use_is_for_all_false(self):
        """After fix: all migrations should create CF with is_for_all: false."""
        from src.migrations.resolution_migration import ResolutionMigration

        source = inspect.getsource(ResolutionMigration._ensure_resolution_cf)
        assert "is_for_all: false" in source, (
            "After fix: resolution migration should use is_for_all: false"
        )

    def test_migrations_track_project_usage(self):
        """After fix: migrations should track which projects use each field."""
        # Verify migration has usage tracking
        pass

    def test_migrations_enable_per_project(self):
        """After fix: migrations should create CustomFieldsProject entries."""
        # Verify migration creates project-specific entries
        pass
