"""
Link type migration module for Jira to OpenProject migration.
Handles the migration of issue link types from Jira to OpenProject.
"""

import json
import os
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import ProgressTracker, console

# Get logger from config
logger = config.logger


class LinkTypeMigration:
    """
    Handles the migration of issue link types from Jira to OpenProject.

    This class is responsible for:
    1. Extracting link type definitions from Jira
    2. Creating corresponding link types in OpenProject
    3. Mapping link types between the systems
    """

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient):
        """
        Initialize the link type migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.
        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.jira_link_types = []
        self.op_link_types = []
        self.link_type_mapping = {}

        self.data_dir = config.get_path("data")

        self.console = console

    def run(
        self, dry_run: bool = False, force: bool = False, mappings=None
    ) -> dict[str, Any]:
        """
        Run the link type migration process.

        Args:
            dry_run: If True, no changes will be made to OpenProject
            force: If True, force extraction of data even if it already exists
            mappings: Optional mappings object that can be used for mapping IDs between systems

        Returns:
            Dictionary with migration results
        """
        logger.info("Starting link type migration...")

        try:
            result = self.migrate_link_types()

            # Format result to match the expected structure
            # If all types were matched and none needed creation, consider it a success
            if "already_matched" in result and result["already_matched"]:
                return {
                    "status": "success",
                    "success_count": result.get("total_count", 0),
                    "failed_count": 0,
                    "total_count": result.get("total_count", 0),
                    "message": "All link types were already matched, no creation needed",
                    "details": result,
                }
            else:
                return {
                    "status": "success" if result.get("success", False) else "failed",
                    "success_count": result.get("migrated_count", 0),
                    "failed_count": result.get("failed_count", 0),
                    "total_count": result.get("total_count", 0),
                    "message": result.get("message", ""),
                    "details": result,
                }
        except Exception as e:
            logger.error(f"Error in link type migration: {str(e)}")
            return {
                "status": "failed",
                "error": str(e),
                "message": f"Error in link type migration: {str(e)}",
                "success_count": 0,
                "failed_count": 0,
                "total_count": 0,
            }

    def extract_jira_link_types(self) -> list[dict[str, Any]]:
        """
        Extract link type definitions from Jira.

        Returns:
            List of Jira link type definitions
        """
        logger.info("Extracting link types from Jira...")

        try:
            url = f"{self.jira_client.base_url}/rest/api/2/issueLinkType"
            response = self.jira_client.jira._session.get(url)
            response.raise_for_status()
            link_types = response.json().get("issueLinkTypes", [])

            logger.info(f"Extracted {len(link_types)} link types from Jira")

            self.jira_link_types = link_types
            self._save_to_json(link_types, "jira_link_types.json")

            return link_types
        except Exception as e:
            logger.error(f"Failed to get link types from Jira: {str(e)}")
            return []

    def extract_openproject_relation_types(self) -> list[dict[str, Any]]:
        """
        Extract relation types from OpenProject.

        Returns:
            List of OpenProject relation type definitions
        """
        logger.info("Extracting relation types from OpenProject...")

        try:
            self.op_link_types = self.op_client.get_relation_types()
        except Exception as e:
            logger.warning(f"Failed to get relation types from OpenProject: {str(e)}")
            logger.warning("Using an empty list of relation types for OpenProject")
            self.op_link_types = []

        logger.info(
            f"Extracted {len(self.op_link_types)} relation types from OpenProject"
        )

        self._save_to_json(self.op_link_types, "openproject_relation_types.json")

        return self.op_link_types

    def create_link_type_mapping(self) -> dict[str, Any]:
        """
        Create a mapping between Jira link types and OpenProject relation types.

        Returns:
            Dictionary mapping Jira link type IDs to OpenProject relation type IDs
        """
        logger.info("Creating link type mapping...")

        if not self.jira_link_types:
            self.extract_jira_link_types()

        if not self.op_link_types:
            self.extract_openproject_relation_types()

        op_relations_by_outward = {
            relation.get("outward", "").lower(): relation
            for relation in self.op_link_types
        }
        op_relations_by_inward = {
            relation.get("inward", "").lower(): relation
            for relation in self.op_link_types
        }
        op_relations_by_name = {
            relation.get("name", "").lower(): relation
            for relation in self.op_link_types
        }

        mapping = {}
        for jira_link_type in self.jira_link_types:
            jira_id = jira_link_type.get("id")
            jira_name = jira_link_type.get("name", "")
            jira_inward = jira_link_type.get("inward", "")
            jira_outward = jira_link_type.get("outward", "")

            jira_name_lower = jira_name.lower()
            jira_inward_lower = jira_inward.lower()
            jira_outward_lower = jira_outward.lower()

            op_relation = op_relations_by_name.get(jira_name_lower, None)
            if op_relation:
                mapping[jira_id] = {
                    "jira_id": jira_id,
                    "jira_name": jira_name,
                    "jira_inward": jira_inward,
                    "jira_outward": jira_outward,
                    "openproject_id": op_relation.get("id"),
                    "openproject_name": op_relation.get("name"),
                    "openproject_inward": op_relation.get("inward"),
                    "openproject_outward": op_relation.get("outward"),
                    "matched_by": "name",
                }
                continue

            op_relation = op_relations_by_outward.get(jira_outward_lower, None)
            if op_relation:
                mapping[jira_id] = {
                    "jira_id": jira_id,
                    "jira_name": jira_name,
                    "jira_inward": jira_inward,
                    "jira_outward": jira_outward,
                    "openproject_id": op_relation.get("id"),
                    "openproject_name": op_relation.get("name"),
                    "openproject_inward": op_relation.get("inward"),
                    "openproject_outward": op_relation.get("outward"),
                    "matched_by": "outward",
                }
                continue

            op_relation = op_relations_by_inward.get(jira_inward_lower, None)
            if op_relation:
                mapping[jira_id] = {
                    "jira_id": jira_id,
                    "jira_name": jira_name,
                    "jira_inward": jira_inward,
                    "jira_outward": jira_outward,
                    "openproject_id": op_relation.get("id"),
                    "openproject_name": op_relation.get("name"),
                    "openproject_inward": op_relation.get("inward"),
                    "openproject_outward": op_relation.get("outward"),
                    "matched_by": "inward",
                }
                continue

            for op_relation in self.op_link_types:
                op_outward = op_relation.get("outward", "").lower()
                op_inward = op_relation.get("inward", "").lower()

                if jira_outward_lower in op_outward or op_outward in jira_outward_lower:
                    mapping[jira_id] = {
                        "jira_id": jira_id,
                        "jira_name": jira_name,
                        "jira_inward": jira_inward,
                        "jira_outward": jira_outward,
                        "openproject_id": op_relation.get("id"),
                        "openproject_name": op_relation.get("name"),
                        "openproject_inward": op_relation.get("inward"),
                        "openproject_outward": op_relation.get("outward"),
                        "matched_by": "similar_outward",
                    }
                    break

                if jira_inward_lower in op_inward or op_inward in jira_inward_lower:
                    mapping[jira_id] = {
                        "jira_id": jira_id,
                        "jira_name": jira_name,
                        "jira_inward": jira_inward,
                        "jira_outward": jira_outward,
                        "openproject_id": op_relation.get("id"),
                        "openproject_name": op_relation.get("name"),
                        "openproject_inward": op_relation.get("inward"),
                        "openproject_outward": op_relation.get("outward"),
                        "matched_by": "similar_inward",
                    }
                    break
            else:
                mapping[jira_id] = {
                    "jira_id": jira_id,
                    "jira_name": jira_name,
                    "jira_inward": jira_inward,
                    "jira_outward": jira_outward,
                    "openproject_id": None,
                    "openproject_name": None,
                    "openproject_inward": None,
                    "openproject_outward": None,
                    "matched_by": "none",
                }

        self.link_type_mapping = mapping
        self._save_to_json(mapping, "link_type_mapping.json")

        total_types = len(mapping)
        matched_types = sum(
            1 for type_data in mapping.values() if type_data["matched_by"] != "none"
        )
        match_percentage = (matched_types / total_types) * 100 if total_types > 0 else 0

        logger.info(f"Link type mapping created for {total_types} types")
        logger.info(
            f"Successfully matched {matched_types} types ({match_percentage:.1f}%)"
        )

        return mapping

    def create_relation_type_in_openproject(
        self, jira_link_type: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        Create a relation type in OpenProject based on a Jira link type.

        Args:
            jira_link_type: The Jira link type definition

        Returns:
            The created OpenProject relation type or None if creation failed
        """
        name = jira_link_type.get("name")
        inward = jira_link_type.get("inward")
        outward = jira_link_type.get("outward")

        if config.migration_config.get("dry_run"):
            logger.info(f"DRY RUN: Would create relation type: {name}")
            return {"id": None, "name": name, "inward": inward, "outward": outward}

        try:
            result = self.op_client.create_relation_type(
                name=name, inward=inward, outward=outward
            )

            if result.get("success", False):
                return result.get("data")
            else:
                logger.error(
                    f"Failed to create relation type: {name} - {result.get('message', 'Unknown error')}"
                )
                return None
        except Exception as e:
            logger.error(f"Error creating relation type {name}: {str(e)}")
            return None

    def migrate_link_types(self) -> dict[str, Any]:
        """
        Migrate link types from Jira to OpenProject.

        Returns:
            Dictionary with migration results including mappings and status
        """
        logger.info("Starting link type migration...")

        if not self.jira_link_types:
            self.extract_jira_link_types()

        if not self.op_link_types:
            self.extract_openproject_relation_types()

        if not self.link_type_mapping:
            self.create_link_type_mapping()

        link_types_to_create = [
            (jira_id, mapping)
            for jira_id, mapping in self.link_type_mapping.items()
            if mapping["matched_by"] == "none"
        ]

        if not link_types_to_create:
            logger.info("No link types need to be created, all are already matched")
            # Return the mapping directly
            return self.link_type_mapping

        migrated_count = 0
        failed_count = 0

        with ProgressTracker(
            description="Migrating link types",
            total=len(link_types_to_create),
            log_title="Link Types Being Created",
        ) as tracker:
            for jira_id, mapping in link_types_to_create:
                jira_link_type = next(
                    (lt for lt in self.jira_link_types if lt.get("id") == jira_id), None
                )

                if not jira_link_type:
                    logger.warning(
                        f"Could not find Jira link type definition for ID: {jira_id}"
                    )
                    continue

                name = jira_link_type.get("name", "")
                tracker.update_description(f"Migrating link type: {name[:20]}")

                op_relation_type = self.create_relation_type_in_openproject(
                    jira_link_type
                )

                link_type_info = f"{name} (Inward: {jira_link_type.get('inward')}, Outward: {jira_link_type.get('outward')})"
                tracker.add_log_item(link_type_info)

                if op_relation_type:
                    mapping["openproject_id"] = op_relation_type.get("id")
                    mapping["openproject_name"] = op_relation_type.get("name")
                    mapping["openproject_inward"] = op_relation_type.get("inward")
                    mapping["openproject_outward"] = op_relation_type.get("outward")
                    mapping["matched_by"] = "created"

                tracker.increment()

                if op_relation_type:
                    migrated_count += 1
                else:
                    failed_count += 1

        self._save_to_json(self.link_type_mapping, "link_type_mapping.json")

        self.analyze_link_type_mapping()

        if config.migration_config.get("dry_run"):
            logger.info(
                "DRY RUN: No relation types were actually created in OpenProject"
            )

        # Return the mapping directly
        return self.link_type_mapping

    def analyze_link_type_mapping(self) -> dict[str, Any]:
        """
        Analyze the link type mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.link_type_mapping:
            mapping_path = os.path.join(self.data_dir, "link_type_mapping.json")
            if os.path.exists(mapping_path):
                with open(mapping_path) as f:
                    self.link_type_mapping = json.load(f)
            else:
                logger.error(
                    "No link type mapping found. Run create_link_type_mapping() first."
                )
                return {}

        analysis = {
            "total_types": len(self.link_type_mapping),
            "matched_types": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] != "none"
            ),
            "matched_by_name": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] == "name"
            ),
            "matched_by_outward": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] == "outward"
            ),
            "matched_by_inward": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] == "inward"
            ),
            "matched_by_similar": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] in ["similar_outward", "similar_inward"]
            ),
            "matched_by_creation": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] == "created"
            ),
            "unmatched_types": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] == "none"
            ),
            "unmatched_details": [
                {
                    "jira_id": type_data["jira_id"],
                    "jira_name": type_data["jira_name"],
                    "jira_inward": type_data["jira_inward"],
                    "jira_outward": type_data["jira_outward"],
                }
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] == "none"
            ],
        }

        total = analysis["total_types"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_types"] / total) * 100
        else:
            analysis["match_percentage"] = 0

        # Add a summary message
        if analysis["unmatched_types"] == 0:
            analysis["message"] = f"All {total} link types are mapped successfully"
        else:
            analysis["message"] = (
                f"{analysis['matched_types']} of {total} link types mapped ({analysis['match_percentage']:.1f}%), {analysis['unmatched_types']} remaining"
            )

        self._save_to_json(analysis, "link_type_mapping_analysis.json")

        logger.info("Link type mapping analysis complete")
        logger.info(f"Total link types: {analysis['total_types']}")
        logger.info(
            f"Matched types: {analysis['matched_types']} ({analysis['match_percentage']:.1f}%)"
        )
        logger.info(f"- Matched by name: {analysis['matched_by_name']}")
        logger.info(
            f"- Matched by outward description: {analysis['matched_by_outward']}"
        )
        logger.info(f"- Matched by inward description: {analysis['matched_by_inward']}")
        logger.info(
            f"- Matched by similar description: {analysis['matched_by_similar']}"
        )
        logger.info(f"- Created in OpenProject: {analysis['matched_by_creation']}")
        logger.info(f"Unmatched types: {analysis['unmatched_types']}")

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
