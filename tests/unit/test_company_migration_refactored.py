#!/usr/bin/env python3
"""Tests for the refactored company_migration.py - _create_companies_batch method."""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.migrations.company_migration import CompanyMigration
from src.models.migration_error import MigrationError


class TestCompanyMigrationRefactored(unittest.TestCase):
    """Test cases for refactored CompanyMigration._create_companies_batch method."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Create mock clients
        self.mock_jira_client = MagicMock()
        self.mock_op_client = MagicMock()
        
        # Create migration instance with mocked clients
        self.migration = CompanyMigration(
            jira_client=self.mock_jira_client,
            op_client=self.mock_op_client
        )
        
        # Mock batch file path
        self.batch_file_path = Path("/tmp/test_batch.json")

    def test_create_companies_batch_successful_processing(self) -> None:
        """Test successful batch processing with valid Ruby script execution."""
        # Setup
        batch_index = 1
        expected_result = {
            "created": [
                {
                    "tempo_id": 123,
                    "tempo_key": "PROJ-1",
                    "tempo_name": "Project 1",
                    "openproject_id": 456,
                    "openproject_identifier": "proj-1",
                    "openproject_name": "Project 1",
                    "status": "created"
                }
            ],
            "errors": [],
            "created_count": 1,
            "error_count": 0
        }
        
        self.mock_op_client.execute_json_query.return_value = expected_result
        
        # Execute
        result = self.migration._create_companies_batch(self.batch_file_path, batch_index)
        
        # Verify
        self.assertEqual(result, expected_result)
        self.mock_op_client.execute_json_query.assert_called_once()
        
        # Verify Ruby script contains batch file path
        ruby_script = self.mock_op_client.execute_json_query.call_args[0][0]
        self.assertIn(str(self.batch_file_path), ruby_script)

    def test_create_companies_batch_with_existing_companies(self) -> None:
        """Test batch processing when some companies already exist."""
        # Setup
        batch_index = 2
        expected_result = {
            "created": [
                {
                    "tempo_id": 789,
                    "tempo_key": "EXIST-1",
                    "tempo_name": "Existing Project",
                    "openproject_id": 101,
                    "openproject_identifier": "exist-1",
                    "openproject_name": "Existing Project",
                    "status": "existing"  # Found existing, not created
                }
            ],
            "errors": [],
            "created_count": 1,
            "error_count": 0
        }
        
        self.mock_op_client.execute_json_query.return_value = expected_result
        
        # Execute
        result = self.migration._create_companies_batch(self.batch_file_path, batch_index)
        
        # Verify
        self.assertEqual(result, expected_result)
        self.assertEqual(result["created"][0]["status"], "existing")

    def test_create_companies_batch_with_validation_errors(self) -> None:
        """Test batch processing with some validation errors."""
        # Setup
        batch_index = 3
        expected_result = {
            "created": [
                {
                    "tempo_id": 111,
                    "tempo_key": "SUCCESS-1",
                    "tempo_name": "Success Project",
                    "openproject_id": 222,
                    "status": "created"
                }
            ],
            "errors": [
                {
                    "tempo_id": 333,
                    "tempo_key": "FAIL-1",
                    "tempo_name": "Failed Project",
                    "identifier": "fail-1",
                    "errors": ["Identifier has already been taken"]
                }
            ],
            "created_count": 1,
            "error_count": 1
        }
        
        self.mock_op_client.execute_json_query.return_value = expected_result
        
        # Execute
        result = self.migration._create_companies_batch(self.batch_file_path, batch_index)
        
        # Verify
        self.assertEqual(result, expected_result)
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(len(result["errors"]), 1)

    def test_create_companies_batch_raises_migration_error_on_execution_failure(self) -> None:
        """Test that MigrationError is raised when Ruby script execution fails."""
        # Setup
        batch_index = 4
        
        # Mock execute_json_query to raise an exception
        self.mock_op_client.execute_json_query.side_effect = Exception("Rails console connection failed")
        
        # Execute & Verify
        with pytest.raises(MigrationError, match="Failed to create companies batch 4.*Rails console connection failed"):
            self.migration._create_companies_batch(self.batch_file_path, batch_index)

    def test_create_companies_batch_raises_migration_error_on_invalid_result_format(self) -> None:
        """Test that MigrationError is raised when result format is invalid."""
        # Setup
        batch_index = 5
        
        # Mock execute_json_query to return invalid format (string instead of dict)
        self.mock_op_client.execute_json_query.return_value = "invalid result"
        
        # Execute & Verify
        with pytest.raises(MigrationError, match="Batch 5: Invalid result format - expected dict, got <class 'str'>"):
            self.migration._create_companies_batch(self.batch_file_path, batch_index)

    def test_create_companies_batch_raises_migration_error_on_none_result(self) -> None:
        """Test that MigrationError is raised when result is None."""
        # Setup
        batch_index = 6
        
        # Mock execute_json_query to return None
        self.mock_op_client.execute_json_query.return_value = None
        
        # Execute & Verify
        with pytest.raises(MigrationError, match="Batch 6: Invalid result format - expected dict, got <class 'NoneType'>"):
            self.migration._create_companies_batch(self.batch_file_path, batch_index)

    def test_create_companies_batch_ruby_script_structure(self) -> None:
        """Test that the generated Ruby script has correct structure."""
        # Setup
        batch_index = 7
        expected_result = {"created": [], "errors": [], "created_count": 0, "error_count": 0}
        self.mock_op_client.execute_json_query.return_value = expected_result
        
        # Execute
        self.migration._create_companies_batch(self.batch_file_path, batch_index)
        
        # Verify Ruby script structure
        ruby_script = self.mock_op_client.execute_json_query.call_args[0][0]
        
        # Should contain required Ruby elements
        self.assertIn("require 'json'", ruby_script)
        self.assertIn("JSON.parse(File.read", ruby_script)
        self.assertIn("created_companies = []", ruby_script)
        self.assertIn("errors = []", ruby_script)
        self.assertIn("CustomField.find_by", ruby_script)
        self.assertIn("Project.new", ruby_script)
        self.assertIn("result.to_json", ruby_script)
        
        # Should include batch file path
        self.assertIn(str(self.batch_file_path), ruby_script)

    def test_create_companies_batch_custom_field_queries(self) -> None:
        """Test that Ruby script includes custom field queries."""
        # Setup
        batch_index = 8
        expected_result = {"created": [], "errors": [], "created_count": 0, "error_count": 0}
        self.mock_op_client.execute_json_query.return_value = expected_result
        
        # Execute
        self.migration._create_companies_batch(self.batch_file_path, batch_index)
        
        # Verify custom field queries in Ruby script
        ruby_script = self.mock_op_client.execute_json_query.call_args[0][0]
        
        self.assertIn("jira_url_cf = CustomField.find_by", ruby_script)
        self.assertIn("tempo_id_cf = CustomField.find_by", ruby_script)
        self.assertIn("name: 'Jira URL'", ruby_script)
        self.assertIn("name: 'Tempo ID'", ruby_script)

    def test_create_companies_batch_with_timeout(self) -> None:
        """Test that batch processing uses appropriate timeout."""
        # Setup
        batch_index = 9
        expected_result = {"created": [], "errors": [], "created_count": 0, "error_count": 0}
        self.mock_op_client.execute_json_query.return_value = expected_result
        
        # Execute
        self.migration._create_companies_batch(self.batch_file_path, batch_index)
        
        # Verify timeout parameter
        call_args = self.mock_op_client.execute_json_query.call_args
        timeout = call_args[1].get("timeout")
        self.assertEqual(timeout, 30)

    def test_create_companies_batch_result_file_creation(self) -> None:
        """Test that Ruby script creates result file for debugging."""
        # Setup
        batch_index = 10
        expected_result = {"created": [], "errors": [], "created_count": 0, "error_count": 0}
        self.mock_op_client.execute_json_query.return_value = expected_result
        
        # Execute
        self.migration._create_companies_batch(self.batch_file_path, batch_index)
        
        # Verify result file creation in Ruby script
        ruby_script = self.mock_op_client.execute_json_query.call_args[0][0]
        self.assertIn(f"File.write('/tmp/batch_result_{batch_index}.json'", ruby_script)

    def test_create_companies_batch_handles_value_error(self) -> None:
        """Test that ValueError is properly converted to MigrationError."""
        # Setup
        batch_index = 11
        
        # Mock execute_json_query to raise ValueError
        self.mock_op_client.execute_json_query.side_effect = ValueError("Invalid JSON in batch file")
        
        # Execute & Verify
        with pytest.raises(MigrationError, match="Failed to create companies batch 11.*Invalid JSON in batch file"):
            self.migration._create_companies_batch(self.batch_file_path, batch_index)

    def test_create_companies_batch_empty_batch_handling(self) -> None:
        """Test handling of empty batch results."""
        # Setup
        batch_index = 12
        expected_result = {
            "created": [],
            "errors": [],
            "created_count": 0,
            "error_count": 0
        }
        
        self.mock_op_client.execute_json_query.return_value = expected_result
        
        # Execute
        result = self.migration._create_companies_batch(self.batch_file_path, batch_index)
        
        # Verify
        self.assertEqual(result, expected_result)
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(len(result["created"]), 0)
        self.assertEqual(len(result["errors"]), 0) 