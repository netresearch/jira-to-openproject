"""
Company migration module for Jira to OpenProject migration.
Handles the migration of Tempo timesheet companies from Jira to OpenProject as top-level projects.
"""

import os
import sys
import json
import re
from typing import Dict, List, Any, Optional
from pathlib import Path

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src import config
from src.display import process_with_progress
from src.utils import load_json_file
from src.migrations.base_migration import BaseMigration

# Constants for filenames
COMPANY_MAPPING_FILE = "company_mapping.json"
TEMPO_COMPANIES_FILE = "tempo_companies.json"
OP_PROJECTS_FILE = "openproject_projects.json"

class CompanyMigration(BaseMigration):
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

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        data_dir: str = None,
    ):
        """
        Initialize the company migration.

        Args:
            jira_client: JiraClient instance.
            op_client: OpenProjectClient instance.
            data_dir: Path to data directory for storing mappings.
        """
        super().__init__(jira_client, op_client, None)

        # Configure paths
        self.data_dir = Path(data_dir or config.get_path("data"))
        os.makedirs(self.data_dir, exist_ok=True)

        # Setup file paths
        self.tempo_companies_file = self.data_dir / TEMPO_COMPANIES_FILE
        self.op_projects_file = self.data_dir / OP_PROJECTS_FILE
        self.company_mapping_file = self.data_dir / COMPANY_MAPPING_FILE

        # Data storage
        self.tempo_companies = {}
        self.op_projects = {}
        self.company_mapping = {}

        # Logging
        self.logger.debug(f"CompanyMigration initialized with data dir: {self.data_dir}")

        # Load existing data if available
        self.tempo_companies = self._load_from_json(TEMPO_COMPANIES_FILE) or {}
        self.op_projects = self._load_from_json(OP_PROJECTS_FILE) or {}
        self.company_mapping = self._load_from_json(COMPANY_MAPPING_FILE) or {}

    def extract_tempo_companies(self) -> Dict[str, Any]:
        """
        Extract companies from Tempo API.

        Returns:
            Dictionary of Tempo companies.
        """
        if load_json_file(self.tempo_companies_file):
            self.tempo_companies = load_json_file(self.tempo_companies_file)
            self.logger.info(f"Loaded {len(self.tempo_companies)} companies from cache")
            return self.tempo_companies

        self.logger.info("Extracting Tempo companies...")

        # Get companies from Tempo API
        companies = self.jira_client.get_tempo_customers()

        if not companies:
            self.logger.warning("No companies found in Tempo")
            self.tempo_companies = {}
            return self.tempo_companies

        self.logger.info(f"Found {len(companies)} companies in Tempo")

        # Process companies
        for company in companies:
            company_id = str(company.get("id"))
            self.tempo_companies[company_id] = {
                "id": company_id,
                "key": company.get("key", "").strip(),
                "name": company.get("name", "").strip(),
                "lead": company.get("lead", {}).get("key") if company.get("lead") else None,
                "status": company.get("status", "ACTIVE"),
                "_raw": company
            }

        # Save to file
        self._save_to_json(self.tempo_companies, TEMPO_COMPANIES_FILE)
        self.logger.info(f"Saved {len(self.tempo_companies)} companies to {self.tempo_companies_file}")

        return self.tempo_companies

    def extract_openproject_projects(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Extract projects from OpenProject.

        Returns:
            List of OpenProject project dictionaries
        """
        self.logger.info("Extracting projects from OpenProject...")

        try:
            self.op_projects = self.op_client.get_projects()
        except Exception as e:
            self.logger.warning(f"Failed to get projects from OpenProject: {str(e)}")
            self.logger.warning("Using an empty list of projects for OpenProject")
            self.op_projects = []

        self.logger.info(f"Extracted {len(self.op_projects)} projects from OpenProject")

        self._save_to_json(self.op_projects, OP_PROJECTS_FILE)

        return self.op_projects

    def create_company_mapping(self) -> Dict[str, Any]:
        """
        Create a mapping between Tempo companies and OpenProject top-level projects.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs
        """
        self.logger.info("Creating company mapping...")

        if not self.tempo_companies:
            self.extract_tempo_companies()

        if not self.op_projects:
            self.extract_openproject_projects()

        top_level_projects = [
            project
            for project in self.op_projects
            if project.get("_links", {}).get("parent", {}).get("href") is None
        ]

        op_projects_by_name = {
            project.get("name", "").lower(): project for project in top_level_projects
        }

        mapping = {}
        for tempo_company in self.tempo_companies.values():
            tempo_id = tempo_company["id"]
            tempo_key = tempo_company["key"]
            tempo_name = tempo_company["name"]
            tempo_name_lower = tempo_name.lower()

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
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "openproject_id": None,
                    "openproject_identifier": None,
                    "openproject_name": None,
                    "matched_by": "none",
                }

        self.company_mapping = mapping
        self._save_to_json(mapping, COMPANY_MAPPING_FILE)

        total_companies = len(mapping)
        matched_companies = sum(
            1 for company in mapping.values() if company["matched_by"] != "none"
        )
        match_percentage = (
            (matched_companies / total_companies) * 100 if total_companies > 0 else 0
        )

        self.logger.info(f"Company mapping created for {total_companies} companies")
        self.logger.info(
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

        description = f"Migrated from Tempo company: {key}\n"
        if lead:
            description += f"Company Lead: {lead}\n"

        base_identifier = "customer_"

        if key:
            raw_id = key.lower()
            sanitized_id = re.sub(r'[^a-z0-9_-]', '_', raw_id)
            base_identifier += sanitized_id
        else:
            raw_id = name.lower()
            sanitized_id = re.sub(r'[^a-z0-9_-]', '_', raw_id)
            base_identifier += sanitized_id

        identifier = base_identifier[:100]

        if config.migration_config.get('dry_run'):
            return {
                "id": None,
                "name": name,
                "identifier": identifier,
                "description": {"raw": description},
                "_links": {"parent": {"href": None}},
            }

        try:
            project, was_created = self.op_client.create_project(
                name=name, identifier=identifier, description=description
            )

            if project:
                if was_created:
                    self._created_companies += 1
                return project
            else:
                return {
                    "id": None,
                    "name": name,
                    "identifier": identifier,
                    "description": {"raw": description},
                    "_links": {"parent": {"href": None}},
                    "_placeholder": True
                }
        except Exception as e:
            error_msg = str(e)
            if "422" in error_msg and "taken" in error_msg:
                return {
                    "id": None,
                    "name": name,
                    "identifier": identifier,
                    "description": {"raw": description},
                    "_links": {"parent": {"href": None}},
                    "_placeholder": True
                }
            else:
                self.logger.error(f"Error creating company project {name}: {str(e)}")
                return None

    def migrate_companies(self) -> Dict[str, Any]:
        """
        Migrate companies from Tempo timesheet to OpenProject as top-level projects.

        Returns:
            Updated mapping with migration results
        """
        self.logger.info("Starting company migration...")

        if not self.tempo_companies:
            self.extract_tempo_companies()

        if not self.op_projects:
            self.extract_openproject_projects()

        if not self.company_mapping:
            self.create_company_mapping()

        companies_to_migrate = self.tempo_companies.values()

        if not companies_to_migrate:
            self.logger.warning(f"No companies found to migrate")
            return self.company_mapping

        self._created_companies = 0

        def process_company(company, context):
            tempo_id = str(company["id"])
            tempo_name = company["name"]

            identifier = None
            if company["key"]:
                base_identifier = "customer_" + re.sub(r'[^a-z0-9_-]', '_', company["key"].lower())
                identifier = base_identifier[:100]

            if identifier:
                existing = self.op_client.get_project_by_identifier(identifier)
                if existing:
                    self.company_mapping[tempo_id] = {
                        "tempo_id": tempo_id,
                        "tempo_key": company["key"],
                        "tempo_name": tempo_name,
                        "openproject_id": existing.get("id"),
                        "openproject_identifier": existing.get("identifier"),
                        "openproject_name": existing.get("name"),
                        "matched_by": "existing",
                    }
                    return tempo_name

            op_project = self.create_company_project_in_openproject(company)

            if op_project:
                self.company_mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": company["key"],
                    "tempo_name": tempo_name,
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "matched_by": "created",
                }

            return tempo_name

        self.logger.info(f"Migrating {len(companies_to_migrate)} companies to OpenProject")
        process_with_progress(
            items=companies_to_migrate,
            process_func=process_company,
            description="Migrating companies",
            log_title="Companies Being Migrated",
            item_name_func=lambda company: company.get("name", "Unknown")
        )

        self._save_to_json(self.company_mapping, COMPANY_MAPPING_FILE)

        if config.migration_config.get('dry_run'):
            self.logger.info(
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
            mapping_path = os.path.join(self.data_dir, COMPANY_MAPPING_FILE)
            if os.path.exists(mapping_path):
                with open(mapping_path, "r") as f:
                    self.company_mapping = json.load(f)
            else:
                self.logger.error(
                    "No company mapping found. Run create_company_mapping() first."
                )
                return {}

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
            "matched_by_existing": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] == "existing"
            ),
            "unmatched_companies": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] == "none"
            ),
            "actually_created": self._created_companies,
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

        total = analysis["total_companies"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_companies"] / total) * 100
        else:
            analysis["match_percentage"] = 0

        self._save_to_json(analysis, "company_mapping_analysis.json")

        self.logger.info(f"Company mapping analysis complete")
        self.logger.info(f"Total companies: {analysis['total_companies']}")
        self.logger.info(
            f"Matched companies: {analysis['matched_companies']} ({analysis['match_percentage']:.1f}%)"
        )
        self.logger.info(f"- Matched by name: {analysis['matched_by_name']}")
        self.logger.info(f"- Created in OpenProject: {analysis['actually_created']}")
        self.logger.info(f"- Already existing in OpenProject: {analysis['matched_by_existing']}")
        self.logger.info(f"Unmatched companies: {analysis['unmatched_companies']}")

        return analysis

    def _create_project_via_api(self, company_name: str) -> Optional[Dict[str, Any]]:
        # Implementation of _create_project_via_api method
        pass

    def _create_project_via_rails(self, company_name: str) -> Optional[Dict[str, Any]]:
        # Implementation of _create_project_via_rails method
        pass
