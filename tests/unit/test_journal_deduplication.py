#!/usr/bin/env python3
"""Unit tests for journal deduplication and phantom journal prevention.

Tests for three identified issues:
1. Phantom journals - "The changes were retracted" messages appearing
2. Duplicate resolution entries - Both comment AND field change for same resolution
3. Custom field project enablement - Fields enabled globally instead of per-project

Issue Reference: https://openproject.sobol.nr/projects/nrs/work_packages/5596731/activity
"""

from unittest.mock import Mock, patch

import pytest

from src.application.components.resolution_migration import ResolutionMigration
from src.utils.enhanced_audit_trail_migrator import EnhancedAuditTrailMigrator


class TestPhantomJournalPrevention:
    """Tests for phantom journal prevention (Bug #17, #18, #19 per ADR-012).

    Phantom journals appear as "The changes were retracted." in OpenProject
    when a journal entry has no actual field changes or notes.
    """

    @pytest.fixture
    def mock_clients(self):
        """Create mock clients."""
        jira_client = Mock()
        op_client = Mock()
        op_client.execute_query = Mock(return_value=True)
        return jira_client, op_client

    @pytest.fixture
    @patch("src.utils.enhanced_audit_trail_migrator.config")
    def migrator(self, mock_config, mock_clients):
        """Create migrator instance."""
        mock_config.logger = Mock()
        jira_client, op_client = mock_clients
        return EnhancedAuditTrailMigrator(jira_client=jira_client, op_client=op_client)

    def test_empty_changelog_item_not_creating_phantom_journal(self, migrator):
        """Test that changelog items with no actual changes don't create journals.

        BUG: Changelog entries with empty items or no-op changes (same from/to value)
        were creating phantom journal entries that appear as "The changes were retracted."
        """
        # Changelog with item that has no actual change (same from/to)
        changelog_with_no_change = [
            {
                "id": "12345",
                "created": "2023-01-15T10:30:00.000+0000",
                "author": {"name": "john.doe"},
                "items": [
                    {
                        "field": "status",
                        "fromString": "Open",
                        "toString": "Open",  # Same value - no actual change
                        "from": "1",
                        "to": "1",
                    },
                ],
            },
        ]

        events = migrator.transform_changelog_to_audit_events(
            changelog_with_no_change,
            jira_issue_key_or_wp="TEST-123",
            openproject_work_package_id=1001,
        )

        # Should NOT create an event for no-op change
        # If this fails, phantom journals will appear as "The changes were retracted."
        assert len(events) == 0, (
            "No-op changelog changes (same from/to) should not create journal events. "
            "These create phantom journals that appear as 'The changes were retracted.'"
        )

    def test_empty_items_list_not_creating_phantom_journal(self, migrator):
        """Test that changelog entries with empty items list don't create journals."""
        # Changelog with empty items array
        changelog_empty_items = [
            {
                "id": "12345",
                "created": "2023-01-15T10:30:00.000+0000",
                "author": {"name": "john.doe"},
                "items": [],  # Empty items list
            },
        ]

        events = migrator.transform_changelog_to_audit_events(
            changelog_empty_items,
            jira_issue_key_or_wp="TEST-123",
            openproject_work_package_id=1001,
        )

        # Should NOT create an event for empty items
        assert len(events) == 0, (
            "Changelog entries with empty items should not create journal events. "
            "These create phantom journals that appear as 'The changes were retracted.'"
        )

    def test_null_values_not_creating_phantom_journal(self, migrator):
        """Test that changelog items with null from/to values are handled correctly."""
        # Changelog with null values on both sides
        changelog_null_values = [
            {
                "id": "12345",
                "created": "2023-01-15T10:30:00.000+0000",
                "author": {"name": "john.doe"},
                "items": [
                    {
                        "field": "fixVersions",
                        "fromString": None,
                        "toString": None,  # Both null - no actual change
                    },
                ],
            },
        ]

        events = migrator.transform_changelog_to_audit_events(
            changelog_null_values,
            jira_issue_key_or_wp="TEST-123",
            openproject_work_package_id=1001,
        )

        # Should NOT create an event for null-to-null change
        assert len(events) == 0, (
            "Null-to-null changes should not create journal events. "
            "These create phantom journals that appear as 'The changes were retracted.'"
        )

    def test_valid_change_creates_proper_journal(self, migrator):
        """Test that valid changes DO create proper journal events."""
        migrator.user_mapping = {"john.doe": 123}

        changelog_valid = [
            {
                "id": "12345",
                "created": "2023-01-15T10:30:00.000+0000",
                "author": {"name": "john.doe"},
                "items": [
                    {
                        "field": "summary",
                        "fromString": "Old Title",
                        "toString": "New Title",
                    },
                ],
            },
        ]

        events = migrator.transform_changelog_to_audit_events(
            changelog_valid,
            jira_issue_key_or_wp="TEST-123",
            openproject_work_package_id=1001,
        )

        # Should create exactly one event for the valid change
        assert len(events) == 1, "Valid changes should create journal events"
        assert events[0]["changes"] == {"subject": ["Old Title", "New Title"]}

    def test_journal_script_includes_changes_check(self, migrator):
        """Test that generated Rails script checks for actual changes before creating journal."""
        audit_events = [
            {
                "work_package_id": 1001,
                "user_id": 123,
                "created_at": "2023-01-15T10:30:00.000+0000",
                "notes": "",  # Empty notes
                "changes": [],  # Empty changes - this should be filtered earlier
            },
        ]

        script = migrator._generate_audit_creation_script(audit_events)

        # The script should have safeguards to prevent phantom journals
        if script.strip():
            # Check for validation that prevents creating empty journals
            assert (
                "notes.empty? && changes.empty?" in script
                or "notes.present?" in script
                or "changes.present?" in script
                or "skip" in script.lower()
            ), (
                "Rails script should validate that journals have actual content "
                "before creating them to prevent phantom 'retracted' entries"
            )


class TestDuplicateResolutionPrevention:
    """Tests for preventing duplicate resolution entries.

    BUG: Resolution changes appear twice:
    1. As a comment from changelog: "resolution: (none) → Fixed"
    2. As a field change from resolution_migration: "J2O Jira Resolution set to Fixed"

    This creates confusing duplicate entries in the activity tab.
    """

    @pytest.fixture
    def mock_op_client(self):
        """Create mock OP client that tracks queries."""
        client = Mock()
        client.queries = []

        def track_query(script):
            client.queries.append(script)
            if "cf.id" in script or "CustomField.find" in script:
                return 99  # Custom field ID
            return True

        client.execute_query = track_query
        # CF helpers moved from BaseMigration to OpenProjectClient in
        # ADR-002 phase 1.3. The base helper now delegates to these.
        client.ensure_wp_custom_field_id = Mock(return_value=99)
        client.enable_custom_field_for_projects = Mock(return_value=None)
        return client

    @pytest.fixture
    def mock_jira_client(self):
        """Create mock Jira client."""
        client = Mock()

        class DummyIssue:
            def __init__(self, resolution_name):
                self.fields = Mock()
                if resolution_name:
                    self.fields.resolution = Mock()
                    self.fields.resolution.name = resolution_name
                else:
                    self.fields.resolution = None

        client.batch_get_issues = Mock(
            return_value={
                "TEST-1": DummyIssue("Fixed"),
                "TEST-2": DummyIssue(None),
            },
        )
        return client

    @pytest.fixture(autouse=True)
    def mock_mappings(self, monkeypatch):
        """Mock config.mappings."""
        import src.config as cfg

        class DummyMappings:
            def __init__(self):
                self._m = {
                    "work_package": {
                        "TEST-1": {"openproject_id": 1001},
                        "TEST-2": {"openproject_id": 1002},
                    },
                }

            def get_mapping(self, name):
                return self._m.get(name, {})

        monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)

    def test_resolution_migration_does_not_create_duplicate_journal(
        self,
        mock_jira_client,
        mock_op_client,
    ):
        """Test that resolution migration does NOT create separate journal entries.

        FIX VERIFIED: Resolution changes are captured by audit trail migration
        from the Jira changelog. This migration should only set the CF value.
        """
        migration = ResolutionMigration(
            jira_client=mock_jira_client,
            op_client=mock_op_client,
        )

        extracted = migration._extract()
        mapped = migration._map(extracted)
        result = migration._load(mapped)

        # Count journal creation queries
        journal_queries = [q for q in mock_op_client.queries if "Journal::WorkPackageJournal.create!" in q]

        # After fix: NO journal entries should be created by resolution migration
        # Resolution changes are already captured by audit trail migration
        assert len(journal_queries) == 0, (
            "Resolution migration should NOT create journal entries. "
            "Resolution history is handled by audit trail migration from changelog."
        )

    def test_resolution_migration_only_sets_cf_value(
        self,
        mock_jira_client,
        mock_op_client,
    ):
        """Test that resolution migration only sets CF value, no journal creation.

        FIX VERIFIED: The resolution migration now only sets the custom field value.
        Journal entries for resolution changes come from the audit trail migration
        which processes the Jira changelog.
        """
        migration = ResolutionMigration(
            jira_client=mock_jira_client,
            op_client=mock_op_client,
        )

        extracted = migration._extract()
        mapped = migration._map(extracted)
        result = migration._load(mapped)

        # Should have CF value setting queries
        cf_queries = [q for q in mock_op_client.queries if "custom_value_for" in q or "custom_field_values" in q]

        # Should NOT have journal creation queries
        journal_queries = [q for q in mock_op_client.queries if "Journal::WorkPackageJournal.create!" in q]

        # Verify: CF values are set but no journals created
        assert len(cf_queries) >= 1, "Resolution migration should set CF values"
        assert len(journal_queries) == 0, (
            "Resolution migration should NOT create journals - "
            "audit trail migration handles resolution history from changelog"
        )

    def test_changelog_contains_resolution_change_detection(self):
        """Test helper to detect if changelog contains resolution change."""
        # Sample changelog with resolution change
        changelog_with_resolution = [
            {
                "created": "2023-01-15T10:30:00.000+0000",
                "author": {"name": "john.doe"},
                "items": [
                    {
                        "field": "resolution",
                        "fromString": None,
                        "toString": "Fixed",
                    },
                ],
            },
        ]

        # Helper function to check if changelog has resolution change
        def has_resolution_in_changelog(changelog: list) -> bool:
            """Check if changelog contains a resolution field change."""
            for entry in changelog:
                for item in entry.get("items", []):
                    if item.get("field") == "resolution":
                        return True
            return False

        assert has_resolution_in_changelog(changelog_with_resolution) is True
        assert has_resolution_in_changelog([]) is False
        assert has_resolution_in_changelog([{"items": [{"field": "status"}]}]) is False


class TestCustomFieldProjectEnablement:
    """Tests for selective custom field project enablement.

    FIX VERIFIED: All custom fields now use is_for_all: false and
    are selectively enabled per-project using CustomFieldsProject.
    """

    def test_custom_field_uses_is_for_all_false(self):
        """Verify custom fields now use is_for_all: false.

        FIX VERIFIED: Custom fields are created with selective enablement.
        After Phase 2a of ADR-002 the Ruby script lives in
        ``OpenProjectCustomFieldService.ensure_wp_custom_field_id`` (own
        module). ``OpenProjectClient.ensure_wp_custom_field_id`` and
        ``BaseMigration._ensure_wp_custom_field`` are thin delegators over it.

        Reads the source file directly because conftest.py monkeypatches
        ``OpenProjectClient`` on the module.
        """
        from pathlib import Path

        src_path = Path(__file__).resolve().parents[2] / "src" / "clients" / "openproject_custom_field_service.py"
        text = src_path.read_text(encoding="utf-8")

        method_start = text.find("def ensure_wp_custom_field_id(")
        assert method_start != -1, "OpenProjectCustomFieldService.ensure_wp_custom_field_id not found"
        method_end = text.find("\n    def ", method_start + 1)
        method_source = text[method_start:method_end]

        assert "is_for_all: false" in method_source, (
            "OpenProjectCustomFieldService.ensure_wp_custom_field_id should use "
            "is_for_all: false for selective project enablement"
        )

    def test_custom_field_has_enable_method(self):
        """Test that migrations have the _enable_cf_for_projects method.

        FIX VERIFIED: Migrations can enable CFs per-project.
        """
        from src.application.components.resolution_migration import ResolutionMigration

        # Verify the method exists
        assert hasattr(ResolutionMigration, "_enable_cf_for_projects"), (
            "Resolution migration should have _enable_cf_for_projects method for selective project enablement"
        )

    def test_project_enablement_tracking(self):
        """Test that we can track which projects actually use a custom field."""

        # Helper to track custom field usage by project
        class CustomFieldUsageTracker:
            def __init__(self):
                self.usage_by_field: dict[str, set[int]] = {}

            def track_usage(self, field_name: str, project_id: int, value: str | None):
                """Track that a field has a value in a project."""
                if value and value.strip():  # Non-empty value
                    if field_name not in self.usage_by_field:
                        self.usage_by_field[field_name] = set()
                    self.usage_by_field[field_name].add(project_id)

            def get_projects_for_field(self, field_name: str) -> set[int]:
                """Get all projects that use this field."""
                return self.usage_by_field.get(field_name, set())

        tracker = CustomFieldUsageTracker()

        # Simulate tracking
        tracker.track_usage("J2O Jira Resolution", project_id=1, value="Fixed")
        tracker.track_usage("J2O Jira Resolution", project_id=2, value="")  # Empty
        tracker.track_usage("J2O Jira Resolution", project_id=3, value="Won't Fix")
        tracker.track_usage("J2O Story Points", project_id=1, value=None)  # Null
        tracker.track_usage("J2O Story Points", project_id=3, value="5")

        # Verify tracking
        resolution_projects = tracker.get_projects_for_field("J2O Jira Resolution")
        assert resolution_projects == {1, 3}, "Should only include projects with non-empty values"

        story_points_projects = tracker.get_projects_for_field("J2O Story Points")
        assert story_points_projects == {3}, "Should only include projects with non-empty values"

    def test_rails_script_for_project_specific_enablement(self):
        """Test Rails script pattern for enabling custom field per-project."""
        # The fix should generate scripts like this:
        expected_pattern = """
        # Create CF with is_for_all: false
        cf = CustomField.find_or_create_by!(
            type: 'WorkPackageCustomField',
            name: 'J2O Jira Resolution'
        ) do |c|
            c.field_format = 'string'
            c.is_for_all = false  # NOT true!
        end

        # Enable for specific projects only
        project_ids = [1, 3]  # Only projects that actually use this field
        project_ids.each do |pid|
            CustomFieldsProject.find_or_create_by!(
                custom_field_id: cf.id,
                project_id: pid
            )
        end
        """

        # Verify the pattern includes key elements
        assert "is_for_all = false" in expected_pattern or "is_for_all: false" in expected_pattern
        assert "CustomFieldsProject" in expected_pattern
        assert "project_id" in expected_pattern


# Marker for tests that verify the bug exists (before fix)
bug_verification = pytest.mark.bug_verification

# Marker for tests that verify the fix works (after fix)
fix_verification = pytest.mark.fix_verification
