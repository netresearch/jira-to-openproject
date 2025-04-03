"""
Company migration module for Jira to OpenProject migration.
Handles the migration of Tempo timesheet companies from Jira to OpenProject as top-level projects.
"""

import os
import sys
import json
import re
import requests
import time
from typing import Dict, List, Any, Optional
from collections import deque

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src import config
from src.display import ProgressTracker, process_with_progress, console

# Get logger from config
logger = config.logger


class CompanyMigration:
    """
    Handles the migration of companies from Tempo timesheet to OpenProject.

    This class is responsible for:
    1. Extracting company information from Tempo timesheet in Jira
    2. Creating corresponding top-level projects in OpenProject
    3. Mapping these companies to be used later when creating projects with account metadata

    The approach is:
    - Tempo Company → OpenProject top-level project
    - Tempo Account → Custom field in OpenProject projects and work packages
    - Jira Project → OpenProject project with account information stored in custom fields
    """

    def __init__(self, dry_run: bool = False):
        """
        Initialize the company migration tools.

        Args:
            dry_run: If True, no changes will be made to OpenProject
        """
        self.jira_client = JiraClient()
        self.op_client = OpenProjectClient()
        self.tempo_companies = []
        self.op_projects = []
        self.company_mapping = {}
        self.dry_run = dry_run

        # Use the centralized config for var directories
        self.data_dir = config.get_path("data")

        # Base Tempo API URL - typically {JIRA_URL}/rest/tempo-accounts/1 for Server
        self.tempo_api_base = f"{config.jira_config.get('url', '').rstrip('/')}/rest/tempo-accounts/1"

        # Setup auth for Tempo API
        self.tempo_auth_headers = {
            "Authorization": f"Bearer {config.jira_config.get('api_token', '')}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def extract_tempo_companies(self) -> List[Dict[str, Any]]:
        """
        Extract company information from Tempo timesheet in Jira.

        Returns:
            List of Tempo company dictionaries
        """
        logger.info("Extracting companies from Tempo timesheet...")

        # Connect to Jira
        if not self.jira_client.connect():
            logger.error("Failed to connect to Jira")
            return []

        try:
            # Call the Tempo API to get all companies
            customers_endpoint = f"{self.tempo_api_base}/customer"

            response = requests.get(
                customers_endpoint,
                headers=self.tempo_auth_headers,
                verify=config.migration_config.get("ssl_verify", True),
            )

            if response.status_code == 200:
                companies = response.json()
                logger.info(f"Retrieved {len(companies)} Tempo companies")

                # Save the companies to a file
                self.tempo_companies = companies
                self._save_to_json(companies, "tempo_companies.json")

                return companies
            else:
                logger.error(
                    f"Failed to get Tempo companies. Status code: {response.status_code}, Response: {response.text}"
                )

                return []

        except Exception as e:
            logger.error(f"Failed to extract Tempo companies: {str(e)}")

            return []

    def extract_openproject_projects(self) -> List[Dict[str, Any]]:
        """
        Extract projects from OpenProject.

        Returns:
            List of OpenProject project dictionaries
        """
        logger.info("Extracting projects from OpenProject...")

        # Get projects from OpenProject
        try:
            self.op_projects = self.op_client.get_projects()
        except Exception as e:
            logger.warning(f"Failed to get projects from OpenProject: {str(e)}")
            logger.warning("Using an empty list of projects for OpenProject")
            self.op_projects = []

        # Log the number of projects found
        logger.info(f"Extracted {len(self.op_projects)} projects from OpenProject")

        # Save projects to file for later reference
        self._save_to_json(self.op_projects, "openproject_projects.json")

        return self.op_projects

    def create_company_mapping(self) -> Dict[str, Any]:
        """
        Create a mapping between Tempo companies and OpenProject top-level projects.

        This method creates a mapping based on company names.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs
        """
        logger.info("Creating company mapping...")

        # Make sure we have companies and projects from both systems
        if not self.tempo_companies:
            self.extract_tempo_companies()

        if not self.op_projects:
            self.extract_openproject_projects()

        # Filter out only top-level projects from OpenProject
        top_level_projects = [
            project
            for project in self.op_projects
            if project.get("_links", {}).get("parent", {}).get("href") is None
        ]

        # Create a lookup dictionary for OpenProject top-level projects by name
        op_projects_by_name = {
            project.get("name", "").lower(): project for project in top_level_projects
        }

        mapping = {}
        for tempo_company in self.tempo_companies:
            tempo_id = tempo_company.get("id")
            tempo_key = tempo_company.get("key")
            tempo_name = tempo_company.get("name", "")
            tempo_name_lower = tempo_name.lower()

            # Try to find a corresponding OpenProject top-level project by name
            op_project = op_projects_by_name.get(tempo_name_lower, None)

            if op_project:
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "matched_by": "name",
                }
            else:
                # No match found, add to mapping with empty OpenProject data
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "openproject_id": None,
                    "openproject_identifier": None,
                    "openproject_name": None,
                    "matched_by": "none",
                }

        # Save mapping to file
        self.company_mapping = mapping
        self._save_to_json(mapping, "company_mapping.json")

        # Log statistics
        total_companies = len(mapping)
        matched_companies = sum(
            1 for company in mapping.values() if company["matched_by"] != "none"
        )
        match_percentage = (
            (matched_companies / total_companies) * 100 if total_companies > 0 else 0
        )

        logger.info(f"Company mapping created for {total_companies} companies")
        logger.info(
            f"Successfully matched {matched_companies} companies ({match_percentage:.1f}%)"
        )

        return mapping

    def create_company_project_in_openproject(
        self, tempo_company: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Create a top-level project in OpenProject based on a Tempo company.

        Args:
            tempo_company: The Tempo company data

        Returns:
            The created OpenProject project or None if creation failed
        """
        name = tempo_company.get("name")
        key = tempo_company.get("key", "")
        lead = tempo_company.get("lead", "")

        # Create a description from the Tempo company data
        description = f"Migrated from Tempo company: {key}\n"
        if lead:
            description += f"Company Lead: {lead}\n"

        # Create a valid OpenProject identifier from the company key
        # OpenProject identifiers must follow these rules:
        # - Length between 1 and 100 characters
        # - Only lowercase letters (a-z), numbers, dashes and underscores
        # - Must start with a lowercase letter

        # For Tempo company projects, add the 'customer_' prefix
        base_identifier = "customer_"

        if key:
            # Use the Tempo key, converted to lowercase and sanitized
            raw_id = key.lower()
            # Replace any invalid characters with underscores
            sanitized_id = re.sub(r'[^a-z0-9_-]', '_', raw_id)
            base_identifier += sanitized_id
        else:
            # Fall back to sanitized name
            raw_id = name.lower()
            # Replace any invalid characters with underscores
            sanitized_id = re.sub(r'[^a-z0-9_-]', '_', raw_id)
            base_identifier += sanitized_id

        # Ensure maximum length respects the 100 char limit
        identifier = base_identifier[:100]

        # Since we're using a progress bar, minimize logging
        if self.dry_run:
            # Return a placeholder for dry run
            return {
                "id": None,
                "name": name,
                "identifier": identifier,
                "description": {"raw": description},
                "_links": {"parent": {"href": None}},
            }

        # Create the top-level project in OpenProject
        try:
            project, was_created = self.op_client.create_project(
                name=name, identifier=identifier, description=description
            )

            if project:
                return project
            else:
                # For company projects, if we get a "identifier taken" error,
                # treat it as success since we just need the container to exist
                return {
                    "id": None,  # We'll use a placeholder ID
                    "name": name,
                    "identifier": identifier,
                    "description": {"raw": description},
                    "_links": {"parent": {"href": None}},
                    "_placeholder": True  # Mark as placeholder
                }
        except Exception as e:
            error_msg = str(e)
            # If this is an "identifier already taken" error, treat it as success
            if "422" in error_msg and "taken" in error_msg:
                # Return a placeholder project that can be used for mapping
                return {
                    "id": None,  # We'll use a placeholder ID
                    "name": name,
                    "identifier": identifier,
                    "description": {"raw": description},
                    "_links": {"parent": {"href": None}},
                    "_placeholder": True  # Mark as placeholder
                }
            else:
                logger.error(f"Error creating company project {name}: {str(e)}")
                return None

    def migrate_companies(self) -> Dict[str, Any]:
        """
        Migrate companies from Tempo timesheet to OpenProject as top-level projects.

        Returns:
            Updated mapping with migration results
        """
        logger.info("Starting company migration...")

        # Make sure we have companies from Tempo
        if not self.tempo_companies:
            self.extract_tempo_companies()

        # Make sure we have projects from OpenProject
        if not self.op_projects:
            self.extract_openproject_projects()

        # Create a mapping between Tempo companies and OpenProject projects
        if not self.company_mapping:
            self.create_company_mapping()

        # Log some debug information about the Tempo companies and mapping
        logger.info(f"Total Tempo companies: {len(self.tempo_companies)}")
        tempo_company_ids = [str(company.get("id")) for company in self.tempo_companies]
        logger.debug(f"Tempo company IDs: {tempo_company_ids[:5]}...")

        mapping_keys = list(self.company_mapping.keys())
        logger.debug(f"Total keys in mapping: {len(mapping_keys)}")
        logger.debug(f"Mapping keys sample: {mapping_keys[:5]}...")

        # Instead of filtering, let's just use all Tempo companies directly
        # This bypasses any issues with ID mapping
        companies_to_migrate = self.tempo_companies

        if not companies_to_migrate:
            logger.warning(f"No companies found to migrate")
            return self.company_mapping

        # Process companies with our centralized progress tracker
        def process_company(company, context):
            tempo_id = str(company.get("id"))
            tempo_name = company.get("name")

            # Create the company as a top-level project in OpenProject
            op_project = self.create_company_project_in_openproject(company)

            if op_project:
                # Update the mapping with the created project
                self.company_mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": company.get("key"),
                    "tempo_name": tempo_name,
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "matched_by": "created",
                }

            return tempo_name  # Return the company name for the log

        # Use our centralized utility to process companies with progress tracking
        logger.info(f"Migrating {len(companies_to_migrate)} companies to OpenProject")
        process_with_progress(
            items=companies_to_migrate,
            process_func=process_company,
            description="Migrating companies",
            log_title="Companies Being Migrated",
            item_name_func=lambda company: company.get("name", "Unknown")
        )

        # Save updated mapping to file
        self._save_to_json(self.company_mapping, "company_mapping.json")

        if self.dry_run:
            logger.info(
                "DRY RUN: No company projects were actually created in OpenProject"
            )

        return self.company_mapping

    def analyze_company_mapping(self) -> Dict[str, Any]:
        """
        Analyze the company mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.company_mapping:
            mapping_path = os.path.join(self.data_dir, "company_mapping.json")
            if os.path.exists(mapping_path):
                with open(mapping_path, "r") as f:
                    self.company_mapping = json.load(f)
            else:
                logger.error(
                    "No company mapping found. Run create_company_mapping() first."
                )
                return {}

        # Analyze the mapping
        analysis = {
            "total_companies": len(self.company_mapping),
            "matched_companies": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] != "none"
            ),
            "matched_by_name": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] == "name"
            ),
            "matched_by_creation": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] == "created"
            ),
            "unmatched_companies": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] == "none"
            ),
            "unmatched_details": [
                {
                    "tempo_id": company["tempo_id"],
                    "tempo_key": company["tempo_key"],
                    "tempo_name": company["tempo_name"],
                }
                for company in self.company_mapping.values()
                if company["matched_by"] == "none"
            ],
        }

        # Calculate percentages
        total = analysis["total_companies"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_companies"] / total) * 100
        else:
            analysis["match_percentage"] = 0

        # Save analysis to file
        self._save_to_json(analysis, "company_mapping_analysis.json")

        # Log analysis summary
        logger.info(f"Company mapping analysis complete")
        logger.info(f"Total companies: {analysis['total_companies']}")
        logger.info(
            f"Matched companies: {analysis['matched_companies']} ({analysis['match_percentage']:.1f}%)"
        )
        logger.info(f"- Matched by name: {analysis['matched_by_name']}")
        logger.info(f"- Created in OpenProject: {analysis['matched_by_creation']}")
        logger.info(f"Unmatched companies: {analysis['unmatched_companies']}")

        return analysis

    def _save_to_json(self, data: Any, filename: str):
        """
        Save data to a JSON file.

        Args:
            data: Data to save
            filename: Name of the file to save to
        """
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved data to {filepath}")


def run_company_migration(dry_run: bool = False, force: bool = False):
    """
    Run the company migration as a standalone script.

    Args:
        dry_run: If True, no changes will be made to OpenProject
        force: If True, force extraction and re-creation of mappings even if they exist
    """
    logger.info("Starting company migration")
    migration = CompanyMigration(dry_run=dry_run)

    # Extract companies from both systems
    migration.extract_tempo_companies()
    migration.extract_openproject_projects()

    # Create mapping and migrate companies
    # If force is True, clear the company mapping to force re-creation
    if force and os.path.exists(os.path.join(migration.data_dir, "company_mapping.json")):
        logger.info("Forcing re-creation of company mapping")
        migration.company_mapping = {}

    migration.create_company_mapping()

    # If force is True, mark all companies as unmatched to force migration
    if force:
        logger.info("Force parameter is set - marking all companies for migration")
        for company_id in migration.company_mapping:
            migration.company_mapping[company_id]["matched_by"] = "none"

    migration.migrate_companies()

    # Analyze company mapping
    migration.analyze_company_mapping()

    logger.info("Company migration complete")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate Tempo companies from Jira to OpenProject as top-level projects"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no changes to OpenProject)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-creation of company mapping and marking all companies for migration",
    )
    args = parser.parse_args()

    run_company_migration(dry_run=args.dry_run, force=args.force)
