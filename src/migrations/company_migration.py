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

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient):
        """
        Initialize the company migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.
        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.tempo_companies = []
        self.op_projects = []
        self.company_mapping = {}
        self._created_companies = 0

        self.data_dir = config.get_path("data")

        # Base Tempo API URL - typically {JIRA_URL}/rest/tempo-accounts/1 for Server
        self.tempo_api_base = f"{config.jira_config.get('url', '').rstrip('/')}/rest/tempo-accounts/1"

        self.tempo_auth_headers = {
            "Authorization": f"Bearer {self.jira_client.jira_config.get('api_token', '')}",
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

        if not self.jira_client.connect():
            logger.error("Failed to connect to Jira")
            return []

        try:
            customers_endpoint = f"{self.tempo_api_base}/customer"

            response = requests.get(
                customers_endpoint,
                headers=self.tempo_auth_headers,
                verify=config.migration_config.get("ssl_verify", True),
            )

            if response.status_code == 200:
                companies = response.json()
                logger.info(f"Retrieved {len(companies)} Tempo companies")

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

        try:
            self.op_projects = self.op_client.get_projects()
        except Exception as e:
            logger.warning(f"Failed to get projects from OpenProject: {str(e)}")
            logger.warning("Using an empty list of projects for OpenProject")
            self.op_projects = []

        logger.info(f"Extracted {len(self.op_projects)} projects from OpenProject")

        self._save_to_json(self.op_projects, "openproject_projects.json")

        return self.op_projects

    def create_company_mapping(self) -> Dict[str, Any]:
        """
        Create a mapping between Tempo companies and OpenProject top-level projects.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs
        """
        logger.info("Creating company mapping...")

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
        for tempo_company in self.tempo_companies:
            tempo_id = tempo_company.get("id")
            tempo_key = tempo_company.get("key")
            tempo_name = tempo_company.get("name", "")
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
        self._save_to_json(mapping, "company_mapping.json")

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
                logger.error(f"Error creating company project {name}: {str(e)}")
                return None

    def migrate_companies(self) -> Dict[str, Any]:
        """
        Migrate companies from Tempo timesheet to OpenProject as top-level projects.

        Returns:
            Updated mapping with migration results
        """
        logger.info("Starting company migration...")

        if not self.tempo_companies:
            self.extract_tempo_companies()

        if not self.op_projects:
            self.extract_openproject_projects()

        if not self.company_mapping:
            self.create_company_mapping()

        companies_to_migrate = self.tempo_companies

        if not companies_to_migrate:
            logger.warning(f"No companies found to migrate")
            return self.company_mapping

        self._created_companies = 0

        def process_company(company, context):
            tempo_id = str(company.get("id"))
            tempo_name = company.get("name")

            identifier = None
            if company.get("key"):
                base_identifier = "customer_" + re.sub(r'[^a-z0-9_-]', '_', company.get("key").lower())
                identifier = base_identifier[:100]

            if identifier:
                existing = self.op_client.get_project_by_identifier(identifier)
                if existing:
                    self.company_mapping[tempo_id] = {
                        "tempo_id": tempo_id,
                        "tempo_key": company.get("key"),
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
                    "tempo_key": company.get("key"),
                    "tempo_name": tempo_name,
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "matched_by": "created",
                }

            return tempo_name

        logger.info(f"Migrating {len(companies_to_migrate)} companies to OpenProject")
        process_with_progress(
            items=companies_to_migrate,
            process_func=process_company,
            description="Migrating companies",
            log_title="Companies Being Migrated",
            item_name_func=lambda company: company.get("name", "Unknown")
        )

        self._save_to_json(self.company_mapping, "company_mapping.json")

        if config.migration_config.get('dry_run'):
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

        logger.info(f"Company mapping analysis complete")
        logger.info(f"Total companies: {analysis['total_companies']}")
        logger.info(
            f"Matched companies: {analysis['matched_companies']} ({analysis['match_percentage']:.1f}%)"
        )
        logger.info(f"- Matched by name: {analysis['matched_by_name']}")
        logger.info(f"- Created in OpenProject: {analysis['actually_created']}")
        logger.info(f"- Already existing in OpenProject: {analysis['matched_by_existing']}")
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
