"""
Link type migration module for Jira to OpenProject migration.
Handles the migration of issue link types from Jira to OpenProject.
"""

import json
import os
from typing import Any
import time

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import ProgressTracker, console
from src.models import ComponentResult

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
        self.link_type_id_mapping: dict[str, str] = {}  # Jira ID -> OpenProject ID

        self.data_dir = config.get_path("data")

        self.console = console

    def run(
        self, dry_run: bool = False, force: bool = False
    ) -> ComponentResult:
        """
        Run the link type migration process.

        Args:
            dry_run: If True, no changes will be made to OpenProject
            force: If True, force extraction of data even if it already exists

        Returns:
            ComponentResult dictionary with migration status and counts
        """
        logger.info("Starting link type migration...")
        start_time = time.time()
        result = ComponentResult(
            success=True,
            message="Link type migration started.",
            details={},
        )

        try:
            # 1. Extract data
            self.extract_jira_link_types(force=force)
            self.extract_openproject_relation_types(force=force)

            if not self.jira_link_types:
                result.success = False
                result.message = "Failed to extract Jira link types."
                result.details["time"] = time.time() - start_time
                return result

            # 2. Create mapping
            self.create_link_type_mapping(force=force)
            if not self.link_type_mapping:
                result.success = False
                result.message = "Failed to create link type mapping."
                result.details["time"] = time.time() - start_time
                return result

            # Analyze mapping to see what needs creation
            link_types_to_create = [
                (jira_id, mapping)
                for jira_id, mapping in self.link_type_mapping.items()
                if mapping["matched_by"] == "none"
            ]

            total_link_types = len(self.link_type_mapping)
            result.details["total_count"] = total_link_types
            result.details["success_count"] = 0
            result.details["failed_count"] = 0
            result.details["status"] = "pending"

            # 3. Migrate (create if necessary)
            if not link_types_to_create:
                logger.info("No link types need to be created, all are already matched")
                result.details["success_count"] = total_link_types
                result.message = f"All {total_link_types} link types were already matched."
                result.details["status"] = "success"
            else:
                migration_sub_result = self._perform_migration(
                    link_types_to_create, dry_run
                )
                result.success = migration_sub_result.success
                result.message = migration_sub_result.message
                if migration_sub_result.details:
                    result.details.update(migration_sub_result.details)
                if migration_sub_result.errors:
                    result.errors = (result.errors or []) + migration_sub_result.errors

            # Update overall status string based on success flag
            result.details["status"] = "success" if result.success else "failed"

        except Exception as e:
            logger.error(f"Error during link type migration: {e}", exc_info=True)
            result.success = False
            result.message = f"An unexpected error occurred: {e}"
            result.errors = (result.errors or []) + [str(e)]
            result.details["status"] = "failed"

        result.details["time"] = time.time() - start_time
        logger.info(
            f"Link type migration finished: Status={result.details.get('status')}, "
            f"Total={result.details.get('total_count', 0)}, Success={result.details.get('success_count', 0)}, "
            f"Failed={result.details.get('failed_count', 0)}, Time={result.details.get('time', 0):.2f}s"
        )
        return result

    def _perform_migration(
        self, link_types_to_create: list[tuple[str, dict]], dry_run: bool
    ) -> ComponentResult:
        """
        Performs the actual creation of link types in OpenProject.

        Args:
            link_types_to_create: List of (jira_id, mapping_dict) for types to create.
            dry_run: If True, simulate creation.

        Returns:
            ComponentResult dictionary with migration status and counts for the creation step.
        """
        migrated_count = 0
        failed_count = 0
        total_to_create = len(link_types_to_create)
        sub_result = ComponentResult(
            success=True,
            message=f"Attempting to create {total_to_create} link types.",
            details={
                "total_count": total_to_create,
                "success_count": 0,
                "failed_count": 0,
                "status": "pending"
            }
        )

        logger.info(f"Attempting to create {total_to_create} link types in OpenProject...")

        with ProgressTracker(
            description="Creating link types",
            total=total_to_create,
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
                    failed_count += 1
                    tracker.increment()
                    continue

                name = jira_link_type.get("name", "")
                tracker.update_description(f"Creating link type: {name[:20]}")

                op_relation_type = None
                error_msg = None
                if not dry_run:
                    try:
                        op_relation_type = self.create_relation_type_in_openproject(
                            jira_link_type
                        )
                    except Exception as e:
                        error_msg = f"Error creating '{name}': {e}"
                        logger.error(error_msg, exc_info=True)
                else:
                    # Simulate success in dry run for mapping purposes
                    logger.info(
                        f"DRY RUN: Skipping creation of relation type '{name}'"
                    )
                    op_relation_type = {
                        "id": f"dry_run_{jira_id}",
                        "name": f"[DRY] {jira_link_type.get('name')}",
                        "reverseName": f"[DRY] {jira_link_type.get('name')}",
                        "inward": jira_link_type.get("inward"),
                        "outward": jira_link_type.get("outward"),
                        "_links": {"self": {"href": "/api/v3/relations/types/dry_run"}},
                    }

                link_type_info = (
                    f"{name} (Inward: {jira_link_type.get('inward')}, "
                    f"Outward: {jira_link_type.get('outward')})"
                )
                tracker.add_log_item(link_type_info)

                if op_relation_type:
                    mapping["openproject_id"] = op_relation_type.get("id")
                    mapping["openproject_name"] = op_relation_type.get("name")
                    mapping["openproject_reverse_name"] = op_relation_type.get(
                        "reverseName"
                    )
                    mapping["matched_by"] = "created"
                    mapping["status"] = "success"
                    migrated_count += 1
                else:
                    mapping["status"] = "failed"
                    mapping["error"] = error_msg or "Failed to create in OpenProject"
                    failed_count += 1
                    if error_msg:
                        sub_result.errors = (sub_result.errors or []) + [error_msg]

                tracker.increment()

        self._save_to_json(self.link_type_mapping, "link_type_mapping.json")
        self.analyze_link_type_mapping()

        already_matched_count = len(self.link_type_mapping) - total_to_create

        final_success_count = migrated_count + already_matched_count
        final_total_count = len(self.link_type_mapping)

        sub_result.details["success_count"] = final_success_count
        sub_result.details["failed_count"] = failed_count
        sub_result.details["total_count"] = final_total_count

        if failed_count > 0:
            sub_result.success = False
            sub_result.details["status"] = "failed"
            sub_result.message = (
                f"Finished creating link types: {migrated_count} created, "
                f"{failed_count} failed. {already_matched_count} were already matched."
            )
        else:
            sub_result.success = True
            sub_result.details["status"] = "success"
            sub_result.message = (
                f"Finished creating link types: {migrated_count} created successfully. "
                f"{already_matched_count} were already matched."
            )

        if dry_run:
            logger.info(
                "DRY RUN: No relation types were actually created in OpenProject"
            )
            sub_result.message += " (DRY RUN)"

        return sub_result

    def extract_jira_link_types(
        self, force: bool = False
    ) -> list[dict[str, Any]] | None:
        """
        Extract link types from Jira.

        Args:
            force: If True, re-extract data even if it exists.

        Returns:
            List of Jira link type dictionaries or None if extraction fails.
        """
        filepath = os.path.join(self.data_dir, "jira_link_types.json")
        if not force and os.path.exists(filepath):
            logger.info("Loading existing Jira link types from file.")
            try:
                with open(filepath) as f:
                    self.jira_link_types = json.load(f)
                logger.info(
                    f"Loaded {len(self.jira_link_types)} Jira link types from cache"
                )
                return self.jira_link_types
            except Exception as e:
                logger.warning(f"Could not load cached Jira link types: {e}")

        logger.info("Extracting link types from Jira...")
        try:
            self.jira_link_types = self.jira_client.get_issue_link_types()
            if self.jira_link_types is not None:
                logger.info(f"Extracted {len(self.jira_link_types)} link types from Jira")
                self._save_to_json(self.jira_link_types, "jira_link_types.json")
                return self.jira_link_types
            else:
                logger.error("Failed to extract link types from Jira (API returned None)")
                self.jira_link_types = []
                return None
        except Exception as e:
            logger.error(f"Error extracting link types from Jira: {e}", exc_info=True)
            self.jira_link_types = []
            return None

    def extract_openproject_relation_types(
        self, force: bool = False
    ) -> list[dict[str, Any]] | None:
        """
        Extract relation types from OpenProject.

        Args:
            force: If True, re-extract data even if it exists.

        Returns:
            List of OpenProject relation type dictionaries or None if extraction fails.
        """
        filepath = os.path.join(self.data_dir, "openproject_relation_types.json")
        if not force and os.path.exists(filepath):
            logger.info("Loading existing OpenProject relation types from file.")
            try:
                with open(filepath) as f:
                    self.op_link_types = json.load(f)
                logger.info(
                    f"Loaded {len(self.op_link_types)} OpenProject relation types from cache"
                )
                return self.op_link_types
            except Exception as e:
                logger.warning(f"Could not load cached OpenProject relation types: {e}")

        logger.info("Extracting relation types from OpenProject...")
        try:
            self.op_link_types = self.op_client.get_relation_types()
            if self.op_link_types is not None:
                logger.info(
                    f"Extracted {len(self.op_link_types)} relation types from OpenProject"
                )
                self._save_to_json(
                    self.op_link_types, "openproject_relation_types.json"
                )
                return self.op_link_types
            else:
                logger.error(
                    "Failed to extract relation types from OpenProject (API returned None)"
                )
                self.op_link_types = []
                return None
        except Exception as e:
            logger.error(
                f"Error extracting relation types from OpenProject: {e}", exc_info=True
            )
            self.op_link_types = []
            return None

    def create_link_type_mapping(self, force: bool = False) -> dict[str, Any] | None:
        """
        Create a mapping between Jira link types and OpenProject relation types.

        Args:
            force: If True, recreate the mapping even if it exists.

        Returns:
            Dictionary representing the mapping or None if creation fails.
        """
        filepath = os.path.join(self.data_dir, "link_type_mapping.json")
        analysis_filepath = os.path.join(
            self.data_dir, "link_type_mapping_analysis.json"
        )

        if (
            not force
            and os.path.exists(filepath)
            and os.path.exists(analysis_filepath)
        ):
            logger.info("Loading existing link type mapping from file.")
            try:
                with open(filepath) as f:
                    self.link_type_mapping = json.load(f)
                logger.info(f"Loaded {len(self.link_type_mapping)} link type mappings")
                # Optionally load and log analysis too
                with open(analysis_filepath) as f:
                    analysis = json.load(f)
                logger.info(f"Loaded mapping analysis: {analysis.get('message')}")
                self._build_id_mapping()
                return self.link_type_mapping
            except Exception as e:
                logger.warning(f"Could not load cached link type mapping: {e}")

        if self.jira_link_types is None:
            self.extract_jira_link_types()
        if self.op_link_types is None:
            self.extract_openproject_relation_types()

        if self.jira_link_types is None or self.op_link_types is None:
            logger.error(
                "Cannot create mapping: Jira link types or OpenProject relation types are missing."
            )
            return None

        logger.info("Creating link type mapping...")
        mapping = {}
        op_types_by_name = {
            op_type["name"].lower(): op_type for op_type in self.op_link_types
        }
        op_types_by_reverse_name = {
            op_type.get("reverseName", "").lower(): op_type for op_type in self.op_link_types
        }

        for jira_type in self.jira_link_types:
            jira_id = str(jira_type["id"])
            jira_name = jira_type["name"]
            jira_outward = jira_type["outward"]
            jira_inward = jira_type["inward"]

            match_info = {
                "jira_id": jira_id,
                "jira_name": jira_name,
                "jira_outward": jira_outward,
                "jira_inward": jira_inward,
                "openproject_id": None,
                "openproject_name": None,
                "openproject_reverse_name": None,
                "matched_by": "none",
                "status": "pending",  # pending, success, failed (during creation)
                "error": None,
            }

            # 1. Exact name match
            if jira_name.lower() in op_types_by_name:
                op_type = op_types_by_name[jira_name.lower()]
                match_info.update(
                    {
                        "openproject_id": str(op_type["id"]),
                        "openproject_name": op_type["name"],
                        "openproject_reverse_name": op_type.get("reverseName"),
                        "matched_by": "name",
                        "status": "matched",
                    }
                )
                mapping[jira_id] = match_info
                continue

            # 2. Exact outward description match (OP Name)
            if jira_outward.lower() in op_types_by_name:
                op_type = op_types_by_name[jira_outward.lower()]
                match_info.update(
                    {
                        "openproject_id": str(op_type["id"]),
                        "openproject_name": op_type["name"],
                        "openproject_reverse_name": op_type.get("reverseName"),
                        "matched_by": "outward",
                        "status": "matched",
                    }
                )
                mapping[jira_id] = match_info
                continue

            # 3. Exact inward description match (OP Reverse Name)
            if jira_inward.lower() in op_types_by_reverse_name:
                op_type = op_types_by_reverse_name[jira_inward.lower()]
                match_info.update(
                    {
                        "openproject_id": str(op_type["id"]),
                        "openproject_name": op_type["name"],
                        "openproject_reverse_name": op_type.get("reverseName"),
                        "matched_by": "inward",
                        "status": "matched",
                    }
                )
                mapping[jira_id] = match_info
                continue

            # 4. Exact inward description match (OP Name) - Less likely but possible
            if jira_inward.lower() in op_types_by_name:
                op_type = op_types_by_name[jira_inward.lower()]
                match_info.update(
                    {
                        "openproject_id": str(op_type["id"]),
                        "openproject_name": op_type["name"],
                        "openproject_reverse_name": op_type.get("reverseName"),
                        # Indicate less common match
                        "matched_by": "inward_match_name",
                        "status": "matched",
                    }
                )
                mapping[jira_id] = match_info
                continue

            # 5. Exact outward description match (OP Reverse Name) - Less likely
            if jira_outward.lower() in op_types_by_reverse_name:
                op_type = op_types_by_reverse_name[jira_outward.lower()]
                match_info.update(
                    {
                        "openproject_id": str(op_type["id"]),
                        "openproject_name": op_type["name"],
                        "openproject_reverse_name": op_type.get("reverseName"),
                        # Indicate less common match
                        "matched_by": "outward_match_reverse",
                        "status": "matched",
                    }
                )
                mapping[jira_id] = match_info
                continue

            # If no match found
            mapping[jira_id] = match_info

        self.link_type_mapping = mapping
        self._save_to_json(self.link_type_mapping, "link_type_mapping.json")
        self.analyze_link_type_mapping()  # Analyze and save analysis
        self._build_id_mapping()
        return self.link_type_mapping

    def _build_id_mapping(self):
        """Build the jira_id -> openproject_id mapping from the full mapping."""
        self.link_type_id_mapping = {
            jira_id: data["openproject_id"]
            for jira_id, data in self.link_type_mapping.items()
            if data.get("openproject_id")
        }
        logger.debug(f"Built link type ID mapping with {len(self.link_type_id_mapping)} entries.")

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
                f"{analysis['matched_types']} of {total} link types mapped "
                f"({analysis['match_percentage']:.1f}%), {analysis['unmatched_types']} remaining"
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
        os.makedirs(os.path.dirname(filepath), exist_ok=True)  # Ensure dir exists
        try:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved data to {filepath}")
        except Exception as e:
            logger.error(f"Failed to save data to {filepath}: {e}")
