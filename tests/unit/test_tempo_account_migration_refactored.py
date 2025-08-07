#!/usr/bin/env python3
"""Tests for the refactored tempo_account_migration.py - create_company_in_openproject method."""

import unittest
from unittest.mock import MagicMock, patch

import pytest

from src.clients.openproject_client import OpenProjectError
from src.migrations.tempo_account_migration import TempoAccountMigration


class TestTempoAccountMigrationRefactored(unittest.TestCase):
    """Test cases for refactored TempoAccountMigration.create_company_in_openproject method."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Create migration instance with mocked clients
        self.migration = TempoAccountMigration()
        self.mock_op_client = MagicMock()
        self.migration.op_client = self.mock_op_client

    def test_create_company_successful_creation(self) -> None:
        """Test successful company creation with valid tempo account data."""
        # Setup
        tempo_account = {
            "name": "Test Company",
            "key": "TEST-KEY",
            "leadDisplayName": "John Doe",
            "customerName": "Customer Corp",
        }

        expected_company = {
            "id": 123,
            "name": "Test Company",
            "identifier": "test-key",
            "description": "Migrated from Tempo account: TEST-KEY\nCustomer: Customer Corp\nAccount Lead: John Doe\n",
        }

        self.mock_op_client.create_company.return_value = (expected_company, True)

        # Execute
        result = self.migration.create_company_in_openproject(tempo_account)

        # Verify
        assert result == expected_company
        self.mock_op_client.create_company.assert_called_once_with(
            name="Test Company",
            identifier="test-key",
            description=expected_company["description"],
        )

    def test_create_company_successful_existing_found(self) -> None:
        """Test successful company creation when existing company is found."""
        # Setup
        tempo_account = {"name": "Existing Company", "key": "EXIST-KEY"}

        existing_company = {
            "id": 456,
            "name": "Existing Company",
            "identifier": "exist-key",
        }

        self.mock_op_client.create_company.return_value = (existing_company, False)

        # Execute
        result = self.migration.create_company_in_openproject(tempo_account)

        # Verify
        assert result == existing_company

    def test_create_company_raises_openproject_error_on_failure(self) -> None:
        """Test that OpenProjectError is raised when company creation fails."""
        # Setup
        tempo_account = {"name": "Failed Company", "key": "FAIL-KEY"}

        # op_client.create_company returns None to indicate failure
        self.mock_op_client.create_company.return_value = (None, False)

        # Execute & Verify
        with pytest.raises(
            OpenProjectError,
            match="Failed to create company: Failed Company",
        ):
            self.migration.create_company_in_openproject(tempo_account)

    @patch("src.migrations.tempo_account_migration.config")
    def test_create_company_dry_run_mode(self, mock_config) -> None:
        """Test company creation in dry run mode returns placeholder."""
        # Setup
        mock_config.migration_config.get.return_value = True  # dry_run = True

        tempo_account = {
            "name": "Dry Run Company",
            "key": "DRY-KEY",
            "customerName": "Dry Customer",
        }

        # Execute
        result = self.migration.create_company_in_openproject(tempo_account)

        # Verify
        expected_result = {
            "id": None,
            "name": "Dry Run Company",
            "identifier": "dry-key",
            "description": "Migrated from Tempo account: DRY-KEY\nCustomer: Dry Customer\n",
        }
        assert result == expected_result

        # Should not call actual create_company in dry run
        self.mock_op_client.create_company.assert_not_called()

    def test_create_company_identifier_generation_special_chars(self) -> None:
        """Test identifier generation with special characters."""
        # Setup
        tempo_account = {
            "name": "Company with Special!@# Characters",
            "key": "",  # Empty key to test name-based identifier generation
        }

        expected_company = {"id": 789, "name": "Company with Special!@# Characters"}
        self.mock_op_client.create_company.return_value = (expected_company, True)

        # Execute
        self.migration.create_company_in_openproject(tempo_account)

        # Verify identifier generation
        self.mock_op_client.create_company.assert_called_once()
        call_args = self.mock_op_client.create_company.call_args[1]
        identifier = call_args["identifier"]

        # Should convert special chars to hyphens and ensure it starts with letter
        assert identifier[0].isalpha()
        assert "!" not in identifier
        assert "@" not in identifier
        assert "#" not in identifier

    def test_create_company_identifier_max_length(self) -> None:
        """Test identifier generation respects 100 character limit."""
        # Setup
        long_name = "A" * 150  # Very long name
        tempo_account = {"name": long_name, "key": "A" * 150}  # Very long key

        expected_company = {"id": 999, "name": long_name}
        self.mock_op_client.create_company.return_value = (expected_company, True)

        # Execute
        self.migration.create_company_in_openproject(tempo_account)

        # Verify identifier length limit
        call_args = self.mock_op_client.create_company.call_args[1]
        identifier = call_args["identifier"]
        assert len(identifier) <= 100

    def test_create_company_identifier_starts_with_letter(self) -> None:
        """Test identifier generation ensures it starts with a letter."""
        # Setup
        tempo_account = {"name": "123 Numeric Company", "key": "123-KEY"}

        expected_company = {"id": 111, "name": "123 Numeric Company"}
        self.mock_op_client.create_company.return_value = (expected_company, True)

        # Execute
        self.migration.create_company_in_openproject(tempo_account)

        # Verify identifier starts with letter
        call_args = self.mock_op_client.create_company.call_args[1]
        identifier = call_args["identifier"]
        assert identifier.startswith("a-")

    def test_create_company_minimal_tempo_account(self) -> None:
        """Test company creation with minimal tempo account data."""
        # Setup
        tempo_account = {
            "name": "Minimal Company",
            # Missing key, leadDisplayName, customerName
        }

        expected_company = {"id": 222, "name": "Minimal Company"}
        self.mock_op_client.create_company.return_value = (expected_company, True)

        # Execute
        result = self.migration.create_company_in_openproject(tempo_account)

        # Verify
        assert result == expected_company
        call_args = self.mock_op_client.create_company.call_args[1]

        # Should handle missing fields gracefully
        assert call_args["name"] == "Minimal Company"
        assert "Migrated from Tempo account:" in call_args["description"]

    def test_create_company_op_client_exception_propagates(self) -> None:
        """Test that exceptions from OpenProjectClient propagate correctly."""
        # Setup
        tempo_account = {"name": "Exception Company", "key": "EXCEPT-KEY"}

        # Mock op_client to raise an exception
        self.mock_op_client.create_company.side_effect = Exception("Connection failed")

        # Execute & Verify
        with pytest.raises(Exception, match="Connection failed"):
            self.migration.create_company_in_openproject(tempo_account)
