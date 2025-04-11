"""
Test module for CompanyMigration.

This module contains test cases for validating the company migration from Jira Tempo to OpenProject.
"""

import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.migrations.company_migration import CompanyMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src import config
from src.utils import load_json_file, save_json_file


class TestCompanyMigration(unittest.TestCase):
    """Test cases for the CompanyMigration class."""

    def setUp(self):
        """Set up the test environment."""
        # Create mock clients
        self.jira_client = MagicMock(spec=JiraClient)
        self.op_client = MagicMock(spec=OpenProjectClient)

        # Create a test data directory
        self.test_data_dir = os.path.join(os.path.dirname(__file__), 'test_data')
        os.makedirs(self.test_data_dir, exist_ok=True)

        # Initialize the company migration
        self.company_migration = CompanyMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
            data_dir=self.test_data_dir
        )

        # Initialize _created_companies attribute
        self.company_migration._created_companies = 0

        # Create test data
        self.sample_tempo_companies = {
            "1": {
                "id": "1",
                "key": "ACME",
                "name": "ACME Corporation",
                "lead": "user1",
                "status": "ACTIVE",
                "_raw": {"id": "1", "key": "ACME", "name": "ACME Corporation", "status": "ACTIVE"}
            },
            "2": {
                "id": "2",
                "key": "GLOBEX",
                "name": "Globex Corporation",
                "lead": "user2",
                "status": "ACTIVE",
                "_raw": {"id": "2", "key": "GLOBEX", "name": "Globex Corporation", "status": "ACTIVE"}
            }
        }

        self.sample_op_projects = [
            {
                "id": 1,
                "name": "ACME Corporation",
                "identifier": "customer_acme",
                "description": {"raw": "Company imported from Tempo"},
                "_links": {"parent": {"href": None}}
            },
            {
                "id": 2,
                "name": "Another Project",
                "identifier": "another-project",
                "description": {"raw": "This is another project"},
                "_links": {"parent": {"href": "/api/v3/projects/1"}}
            }
        ]

        # Set up the mock return values
        self.jira_client.get_tempo_customers.return_value = [
            {"id": "1", "key": "ACME", "name": "ACME Corporation", "status": "ACTIVE"},
            {"id": "2", "key": "GLOBEX", "name": "Globex Corporation", "status": "ACTIVE"}
        ]
        self.op_client.get_projects.return_value = self.sample_op_projects

    def tearDown(self):
        """Clean up after each test."""
        # Remove test data files
        for filename in os.listdir(self.test_data_dir):
            os.remove(os.path.join(self.test_data_dir, filename))

        # Remove test data directory if empty
        if os.path.exists(self.test_data_dir) and not os.listdir(self.test_data_dir):
            os.rmdir(self.test_data_dir)

    def test_extract_tempo_companies(self):
        """Test extracting companies from Tempo."""
        companies = self.company_migration.extract_tempo_companies()

        # Verify that get_tempo_customers was called
        self.jira_client.get_tempo_customers.assert_called_once()

        # Verify that the correct data was returned
        self.assertEqual(len(companies), 2)
        self.assertEqual(companies["1"]["name"], "ACME Corporation")
        self.assertEqual(companies["2"]["name"], "Globex Corporation")

        # Verify that the data was saved to a file
        companies_file = os.path.join(self.test_data_dir, "tempo_companies.json")
        self.assertTrue(os.path.exists(companies_file))

        # Verify the file content
        with open(companies_file, "r") as f:
            saved_companies = json.load(f)
        self.assertEqual(saved_companies, companies)

    def test_extract_openproject_projects(self):
        """Test extracting projects from OpenProject."""
        projects = self.company_migration.extract_openproject_projects(force=True)

        # Verify that get_projects was called
        self.op_client.get_projects.assert_called_once()

        # Verify that the correct data was returned
        self.assertEqual(projects, self.sample_op_projects)

        # Verify that the data was saved to a file
        projects_file = os.path.join(self.test_data_dir, "openproject_projects.json")
        self.assertTrue(os.path.exists(projects_file))

    def test_create_company_mapping(self):
        """Test creating a mapping between Tempo companies and OpenProject projects."""
        # Set up test data
        self.company_migration.tempo_companies = self.sample_tempo_companies
        self.company_migration.op_projects = self.sample_op_projects

        # Call the method
        mapping = self.company_migration.create_company_mapping()

        # Verify that a mapping is created
        self.assertIsInstance(mapping, dict)

        # Verify that each Tempo company has a mapping
        for company_id in self.sample_tempo_companies:
            self.assertIn(company_id, mapping)

        # Check specific mappings based on our sample data
        self.assertEqual(mapping["1"]["openproject_id"], 1)  # "ACME Corporation" -> "ACME Corporation"
        self.assertEqual(mapping["1"]["matched_by"], "name")

        self.assertIsNone(mapping["2"]["openproject_id"])  # "Globex Corporation" -> None (not found)
        self.assertEqual(mapping["2"]["matched_by"], "none")

        # Verify that the mapping was saved to a file
        mapping_file = os.path.join(self.test_data_dir, "company_mapping.json")
        self.assertTrue(os.path.exists(mapping_file))

    @patch('src.migrations.company_migration.config.migration_config')
    def test_create_company_project_in_openproject(self, mock_migration_config):
        """Test creating a company project in OpenProject."""
        # Configure the mock to return False for dry_run
        mock_migration_config.get.return_value = False

        # Set up mock for create_project
        project_response = {
            "id": 3,
            "name": "Globex Corporation",
            "identifier": "customer_globex",
            "description": {"raw": "Migrated from Tempo company: GLOBEX\nCompany Lead: user2\n"},
            "_links": {"parent": {"href": None}}
        }
        self.op_client.create_project.return_value = (project_response, True)  # (project, was_created)

        # Test with a company that needs to be created
        company = self.sample_tempo_companies["2"]

        # Call the method directly to verify it returns the expected result
        result = self.company_migration.create_company_project_in_openproject(company)

        # Verify create_project was called with correct parameters
        self.op_client.create_project.assert_called_with(
            name="Globex Corporation",
            identifier="customer_globex",
            description="Migrated from Tempo company: GLOBEX\nCompany Lead: user2\n"
        )

        # Verify the result is not None and has the expected values
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 3)
        self.assertEqual(result["name"], "Globex Corporation")

    @patch('src.migrations.company_migration.config.migration_config')
    @patch('src.migrations.company_migration.process_with_progress')
    def test_migrate_companies(self, mock_process_with_progress, mock_migration_config):
        """Test the company migration process."""
        # Configure the mock to return False for dry_run
        mock_migration_config.get.return_value = False

        # Set up test data
        self.company_migration.tempo_companies = self.sample_tempo_companies
        self.company_migration.op_projects = self.sample_op_projects

        # Copy of initial mapping with one match and one unmatched
        initial_mapping = {
            "1": {
                "tempo_id": "1",
                "tempo_key": "ACME",
                "tempo_name": "ACME Corporation",
                "openproject_id": 1,
                "openproject_identifier": "customer_acme",
                "openproject_name": "ACME Corporation",
                "matched_by": "name"
            },
            "2": {
                "tempo_id": "2",
                "tempo_key": "GLOBEX",
                "tempo_name": "Globex Corporation",
                "openproject_id": None,
                "openproject_identifier": None,
                "openproject_name": None,
                "matched_by": "none"
            }
        }
        self.company_migration.company_mapping = json.loads(json.dumps(initial_mapping))  # Deep copy

        # Mock get_project_by_identifier to return None (project doesn't exist yet)
        self.op_client.get_project_by_identifier.return_value = None

        # Mock create_project to return a successful creation
        project_response = {
            "id": 3,
            "name": "Globex Corporation",
            "identifier": "customer_globex",
            "description": {"raw": "Migrated from Tempo company: GLOBEX\nCompany Lead: user2\n"},
            "_links": {"parent": {"href": None}}
        }
        self.op_client.create_project.return_value = (project_response, True)  # was_created flag

        # Configure process_with_progress to call the function for each item
        def side_effect(items, process_func, description, log_title, item_name_func):
            # Only process company "2" (GLOBEX)
            globex_company = self.sample_tempo_companies["2"]
            process_func(globex_company, {})
        mock_process_with_progress.side_effect = side_effect

        # Call the migrate method
        result = self.company_migration.migrate_companies()

        # Verify that company 1 (ACME) is still mapped to the original project
        self.assertEqual(result["1"]["openproject_id"], 1)
        self.assertEqual(result["1"]["matched_by"], "name")

        # Verify that company 2 (GLOBEX) is now mapped to the newly created project
        self.assertEqual(result["2"]["openproject_id"], 3)
        self.assertEqual(result["2"]["matched_by"], "created")

        # Verify that create_project was called for the unmapped company
        self.op_client.create_project.assert_called_once()

    def test_analyze_company_mapping(self):
        """Test analyzing the company mapping."""
        # Set up test data - a mapping with different match types
        self.company_migration.company_mapping = {
            "1": {
                "tempo_id": "1",
                "tempo_key": "ACME",
                "tempo_name": "ACME Corporation",
                "matched_by": "name"
            },
            "2": {
                "tempo_id": "2",
                "tempo_key": "GLOBEX",
                "tempo_name": "Globex Corporation",
                "matched_by": "created"
            },
            "3": {
                "tempo_id": "3",
                "tempo_key": "INITECH",
                "tempo_name": "Initech",
                "matched_by": "existing"
            },
            "4": {
                "tempo_id": "4",
                "tempo_key": "UMBRELLA",
                "tempo_name": "Umbrella Corp",
                "matched_by": "none"
            }
        }
        # Initialize the counter for created companies
        self.company_migration._created_companies = 1

        # Call the analyze method
        analysis = self.company_migration.analyze_company_mapping()

        # Verify analysis results
        self.assertEqual(analysis["total_companies"], 4)
        self.assertEqual(analysis["matched_companies"], 3)
        self.assertEqual(analysis["matched_by_name"], 1)
        self.assertEqual(analysis["matched_by_creation"], 1)
        self.assertEqual(analysis["matched_by_existing"], 1)
        self.assertEqual(analysis["unmatched_companies"], 1)
        self.assertEqual(analysis["actually_created"], 1)


if __name__ == "__main__":
    unittest.main()
