"""Utility for selective custom field project enablement.

Custom fields should only be visible in projects where they have non-empty,
non-default values. This prevents "spammy" custom fields appearing in all projects.

Usage:
    tracker = CustomFieldProjectTracker()
    # Track usage during migration
    tracker.record_usage("J2O Resolution", project_id=1, value="Fixed")
    tracker.record_usage("J2O Resolution", project_id=2, value="")  # Empty - ignored

    # Generate enablement scripts
    scripts = tracker.generate_enablement_scripts(cf_id=99)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


class CustomFieldProjectTracker:
    """Track which projects actually use each custom field."""

    def __init__(self) -> None:
        """Initialize the tracker."""
        self._usage: dict[str, set[int]] = {}

    def record_usage(
        self,
        field_name: str,
        project_id: int,
        value: str | int | float | None,
        default_value: str | int | float | None = None,
    ) -> None:
        """Record that a project has a value for a custom field.

        Only records if value is non-empty and different from default.

        Args:
            field_name: Name of the custom field
            project_id: OpenProject project ID
            value: The actual value set
            default_value: The default value (if any) to compare against

        """
        is_meaningful = self._is_meaningful_value(value, default_value)

        if is_meaningful:
            if field_name not in self._usage:
                self._usage[field_name] = set()
            self._usage[field_name].add(project_id)

    def _is_meaningful_value(
        self,
        value: str | int | float | None,
        default_value: str | int | float | None = None,
    ) -> bool:
        """Check if a value is meaningful (non-empty, non-default)."""
        if value is None:
            return False

        if isinstance(value, (int, float)):
            # Numeric: non-zero is meaningful
            if value == 0:
                return False
            # Unless default is also non-zero
            if default_value is not None and value == default_value:
                return False
            return True

        # String: non-empty is meaningful
        str_value = str(value).strip()
        if str_value == "":
            return False

        # Check against default
        if default_value is not None:
            str_default = str(default_value).strip()
            if str_value == str_default:
                return False

        return True

    def get_projects_for_field(self, field_name: str) -> set[int]:
        """Get all projects that use this field."""
        return self._usage.get(field_name, set())

    def should_enable_for_project(self, field_name: str, project_id: int) -> bool:
        """Check if field should be enabled for a specific project."""
        return project_id in self._usage.get(field_name, {})

    def generate_enablement_script(
        self,
        cf_id: int,
        field_name: str,
    ) -> str:
        """Generate Rails script to enable CF for specific projects only.

        Args:
            cf_id: Custom field ID in OpenProject
            field_name: Custom field name (for logging)

        Returns:
            Ruby script string for Rails console execution

        """
        project_ids = self.get_projects_for_field(field_name)
        if not project_ids:
            return ""

        project_ids_str = ", ".join(str(pid) for pid in sorted(project_ids))

        return f"""
# Selective enablement for '{field_name}'
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
""".strip()


def generate_post_migration_fix_script() -> str:
    """Generate Rails script to fix already-created J2O custom fields.

    This script can be run after migration to:
    1. Find all J2O custom fields
    2. Set is_for_all = false
    3. Enable only for projects that have non-empty values

    Returns:
        Ruby script string for Rails console execution

    """
    return """
# Post-migration fix for J2O custom fields
# Sets is_for_all = false and enables per-project based on actual usage

j2o_fields = CustomField.where(type: 'WorkPackageCustomField')
                        .where("name LIKE 'J2O %' OR name IN ('Resolution', 'Story Points', 'Votes', 'Labels', 'Sprint', 'Affects Versions', 'Security Level')")

puts "Found #{j2o_fields.count} J2O custom fields to fix"

j2o_fields.each do |cf|
  # Skip if already fixed
  next unless cf.is_for_all?

  # Find projects that actually have values for this field
  projects_with_values = Project.joins(:work_packages)
    .joins("INNER JOIN custom_values cv ON cv.customized_id = work_packages.id AND cv.customized_type = 'WorkPackage'")
    .where("cv.custom_field_id = ? AND cv.value IS NOT NULL AND cv.value != ''", cf.id)
    .distinct

  puts "Field '#{cf.name}': found #{projects_with_values.count} projects with values"

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

  puts "  -> Fixed: enabled for #{projects_with_values.count} projects"
end

puts "\\nDone! All J2O custom fields now have selective project enablement."
""".strip()


def create_cf_with_selective_enablement(
    op_client: "OpenProjectClient",
    cf_name: str,
    field_format: str,
    project_ids: list[int] | set[int],
) -> int:
    """Create a custom field with selective project enablement.

    Creates the CF with is_for_all=false and enables for specific projects.

    Args:
        op_client: OpenProject client for executing queries
        cf_name: Name of the custom field
        field_format: Field format (string, int, float, text)
        project_ids: List of project IDs to enable the field for

    Returns:
        Custom field ID

    """
    project_ids_list = sorted(project_ids) if project_ids else []
    project_ids_str = ", ".join(str(pid) for pid in project_ids_list)

    # Create CF with is_for_all: false
    script = f"""
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{cf_name}')
if !cf
  cf = CustomField.new(
    name: '{cf_name}',
    field_format: '{field_format}',
    is_required: false,
    is_for_all: false,
    type: 'WorkPackageCustomField'
  )
  cf.save!
end

# Enable for specific projects
[{project_ids_str}].each do |project_id|
  begin
    project = Project.find(project_id)
    CustomFieldsProject.find_or_create_by!(
      custom_field: cf,
      project: project
    )
  rescue ActiveRecord::RecordNotFound
    # Skip missing projects
  end
end if [{project_ids_str}].any?

cf.id
""".strip()

    result = op_client.execute_query(script)
    return int(result) if isinstance(result, (int, str)) else 0
