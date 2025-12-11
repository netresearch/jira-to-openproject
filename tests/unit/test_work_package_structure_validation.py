"""Structural validation tests for WorkPackageMigration.

These tests ensure critical methods exist in the class and are callable.
They would have caught the orphaned code bug where methods were outside the class.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.migrations.work_package_migration import WorkPackageMigration


@pytest.mark.unit
class TestWorkPackageStructure:
    """Tests for WorkPackageMigration class structure and method existence."""

    @patch("src.utils.data_handler.load_dict")
    def test_get_current_entities_for_type_exists_and_callable(
        self,
        mock_load_dict,
    ):
        """Test that _get_current_entities_for_type exists as instance method."""
        # Setup mocks
        mock_load_dict.return_value = {}

        mock_jira = MagicMock()
        mock_op = MagicMock()

        # Create instance
        migration = WorkPackageMigration(
            jira_client=mock_jira,
            op_client=mock_op,
        )

        # CRITICAL: This attribute must exist on the instance
        assert hasattr(migration, "_get_current_entities_for_type"), (
            "_get_current_entities_for_type not found as instance method! Method may be orphaned outside the class."
        )

        # Verify it's callable
        assert callable(migration._get_current_entities_for_type), (
            "_get_current_entities_for_type exists but is not callable"
        )

    @patch("src.utils.data_handler.load_dict")
    def test_load_custom_field_mapping_exists_and_callable(
        self,
        mock_load_dict,
    ):
        """Test that _load_custom_field_mapping exists as instance method."""
        # Setup mocks
        mock_load_dict.return_value = {}

        mock_jira = MagicMock()
        mock_op = MagicMock()

        # Create instance
        migration = WorkPackageMigration(
            jira_client=mock_jira,
            op_client=mock_op,
        )

        # CRITICAL: This attribute must exist on the instance
        assert hasattr(migration, "_load_custom_field_mapping"), (
            "_load_custom_field_mapping not found as instance method! "
            "Method may be orphaned outside the class or nested in another function."
        )

        # Verify it's callable
        assert callable(migration._load_custom_field_mapping), "_load_custom_field_mapping exists but is not callable"

    @patch("src.utils.data_handler.load_dict")
    def test_prepare_work_package_can_call_load_custom_field_mapping(
        self,
        mock_load_dict,
    ):
        """Test that prepare_work_package can successfully call _load_custom_field_mapping.

        This would fail if _load_custom_field_mapping is orphaned outside the class.
        """
        # Setup mocks
        mock_load_dict.return_value = {}

        mock_jira = MagicMock()
        mock_op = MagicMock()
        mock_op.get_custom_fields.return_value = []

        # Create instance
        migration = WorkPackageMigration(
            jira_client=mock_jira,
            op_client=mock_op,
        )

        # Mock the file existence check
        with patch("pathlib.Path.exists", return_value=False):
            # This should NOT raise AttributeError if method is in class
            try:
                result = migration._load_custom_field_mapping()
                # If we get here, the method exists and is callable
                assert isinstance(result, dict), "Should return a dict"
            except AttributeError as e:
                pytest.fail(
                    f"AttributeError calling _load_custom_field_mapping: {e}. "
                    f"This indicates the method is not an instance method of the class!",
                )

    @patch("src.utils.data_handler.load_dict")
    def test_all_critical_methods_exist(self, mock_load_dict):
        """Verify all critical instance methods exist on the class."""
        mock_load_dict.return_value = {}

        mock_jira = MagicMock()
        mock_op = MagicMock()

        migration = WorkPackageMigration(
            jira_client=mock_jira,
            op_client=mock_op,
        )

        critical_methods = [
            "_get_current_entities_for_type",
            "_load_custom_field_mapping",
            "prepare_work_package",
            "run",
        ]

        missing_methods = []
        for method_name in critical_methods:
            if not hasattr(migration, method_name):
                missing_methods.append(method_name)
            elif not callable(getattr(migration, method_name)):
                missing_methods.append(f"{method_name} (not callable)")

        assert not missing_methods, (
            f"Critical methods missing from WorkPackageMigration instance: {missing_methods}. "
            f"These may be orphaned outside the class or nested in other functions."
        )
