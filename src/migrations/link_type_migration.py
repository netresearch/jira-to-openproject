"""Link type migration module for Jira to OpenProject migration.
Handles the migration of issue link types from Jira to OpenProject.
"""

import json
import time
from typing import Any
from pathlib import Path

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import console
from src.migrations.base_migration import BaseMigration
from src.migrations.custom_field_migration import CustomFieldMigration
from src.models import ComponentResult

# Get logger from config
logger = config.logger

# Default OpenProject relation types
# These are built-in and cannot be modified or extended via API
DEFAULT_RELATION_TYPES = [
    {
        "id": "relates",
        "name": "relates to",
        "reverseName": "relates to",
        "_type": "RelationType",
    },
    {
        "id": "duplicates",
        "name": "duplicates",
        "reverseName": "duplicated by",
        "_type": "RelationType",
    },
    {
        "id": "blocks",
        "name": "blocks",
        "reverseName": "blocked by",
        "_type": "RelationType",
    },
    {
        "id": "precedes",
        "name": "precedes",
        "reverseName": "follows",
        "_type": "RelationType",
    },
    {
        "id": "includes",
        "name": "includes",
        "reverseName": "part of",
        "_type": "RelationType",
    },
]


class LinkTypeMigration(BaseMigration):
    """Handles the migration of issue link types from Jira to OpenProject.

    This class is responsible for:
    1. Extracting link type definitions from Jira
    2. Mapping Jira link types to OpenProject's built-in relation types
    3. Creating custom fields for unmapped link types
    """

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        """Initialize the link type migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.

        """
        super().__init__(jira_client, op_client)
        self.jira_link_types: list[dict[str, Any]] = []
        self.op_link_types = DEFAULT_RELATION_TYPES
        self.link_type_mapping: dict[str, Any] = {}
        self.link_type_id_mapping: dict[str, str] = {}  # Jira ID -> OpenProject ID

        self.console = console

    def run(self) -> ComponentResult:
        """Run the link type migration process.

        Returns:
            ComponentResult dictionary with migration status and counts

        """
        self.logger.info("Starting link type migration...")
        start_time = time.time()
        result = ComponentResult(
            success=True,
            message="Link type migration started.",
            details={},
        )

        try:
            # 1. Extract data
            self.extract_jira_link_types()
            # Note: We no longer try to extract from OpenProject API since it's not supported
            self._save_to_json(self.op_link_types, Path("openproject_relation_types.json"))

            if not self.jira_link_types:
                result.success = False
                result.message = "Failed to extract Jira link types."
                result.details["time"] = time.time() - start_time
                return result

            # 2. Create mapping
            self.create_link_type_mapping()
            if not self.link_type_mapping:
                result.success = False
                result.message = "Failed to create link type mapping."
                result.details["time"] = time.time() - start_time
                return result

            # Analyze mapping to identify which link types need custom fields
            unmapped_link_types = [
                (jira_id, mapping)
                for jira_id, mapping in self.link_type_mapping.items()
                if mapping["matched_by"] == "none"
            ]

            total_link_types = len(self.link_type_mapping)
            result.details["total_count"] = total_link_types
            result.details["success_count"] = 0
            result.details["failed_count"] = 0
            result.details["status"] = "pending"

            # 3. Create custom fields for unmapped link types if needed
            if not unmapped_link_types:
                self.logger.info("All link types are mapped to default OpenProject relation types")
                result.details["success_count"] = total_link_types
                result.message = f"All {total_link_types} link types were successfully mapped."
                result.details["status"] = "success"
            else:
                self.logger.info(f"Found {len(unmapped_link_types)} link types that need custom fields")
                if config.migration_config.get("dry_run", False):
                    self.logger.info("DRY RUN: Would create custom fields for unmapped link types")
                    result.details["success_count"] = total_link_types
                    result.message = (
                        f"DRY RUN: {len(unmapped_link_types)} link types would be created as custom fields."
                    )
                    result.details["status"] = "success"
                else:
                    # Create custom fields for unmapped link types
                    cf_result = self.create_custom_fields_for_link_types(unmapped_link_types)

                    # Update the result based on the custom field creation outcome
                    if cf_result["success"]:
                        created_count = cf_result["created_count"]
                        error_count = cf_result["error_count"]

                        result.details["success_count"] = total_link_types - error_count
                        result.details["custom_field_count"] = created_count
                        result.details["failed_count"] = error_count

                        if error_count == 0:
                            result.message = (
                                f"Successfully migrated all {total_link_types} link types: "
                                f"{total_link_types - created_count} mapped to standard relations, "
                                f"{created_count} created as custom fields."
                            )
                            result.details["status"] = "success"
                        else:
                            result.message = (
                                f"Partially migrated {total_link_types} link types: "
                                f"{total_link_types - created_count - error_count} mapped to standard relations, "
                                f"{created_count} created as custom fields, "
                                f"{error_count} failed."
                            )
                            result.details["status"] = "partial_success"
                    else:
                        result.details["success_count"] = total_link_types - len(unmapped_link_types)
                        result.details["failed_count"] = len(unmapped_link_types)
                        result.message = (
                            f"Mapped {result.details['success_count']} link types to default OpenProject relations. "
                            f"Failed to create custom fields for {len(unmapped_link_types)} link types."
                        )
                        result.details["status"] = "partial_success"

            # Update overall status string based on success flag
            if result.details["status"] != "partial_success":
                result.details["status"] = "success" if result.success else "failed"

        except Exception as e:
            self.logger.error(f"Error during link type migration: {e}", exc_info=True)
            result.success = False
            result.message = f"An unexpected error occurred: {e}"
            result.errors = (result.errors or []) + [str(e)]
            result.details["status"] = "failed"

        result.details["time"] = time.time() - start_time
        self.logger.info(
            f"Link type migration finished: Status={result.details.get('status')}, "
            f"Total={result.details.get('total_count', 0)}, Success={result.details.get('success_count', 0)}, "
            f"Failed={result.details.get('failed_count', 0)}, Time={result.details.get('time', 0):.2f}s",
        )
        return result

    def extract_jira_link_types(self) -> list[dict[str, Any]] | None:
        """Extract link types from Jira.

        Returns:
            List of Jira link type dictionaries or None if extraction fails.

        """
        filepath = self.data_dir / "jira_link_types.json"
        if not config.migration_config.get("force", False) and filepath.exists():
            self.logger.info("Loading existing Jira link types from file.")
            try:
                with filepath.open() as f:
                    self.jira_link_types = json.load(f)
                self.logger.info(f"Loaded {len(self.jira_link_types)} Jira link types from cache")
                return self.jira_link_types
            except Exception as e:
                self.logger.warning(f"Could not load cached Jira link types: {e}")

        self.logger.info("Extracting link types from Jira...")
        try:
            self.jira_link_types = self.jira_client.get_issue_link_types()
            if self.jira_link_types is not None:
                self.logger.info(f"Extracted {len(self.jira_link_types)} link types from Jira")
                self._save_to_json(self.jira_link_types, Path("jira_link_types.json"))
                return self.jira_link_types
            self.logger.error("Failed to extract link types from Jira (API returned None)")
            self.jira_link_types = []
            return None
        except Exception as e:
            self.logger.error(f"Error extracting link types from Jira: {e}", exc_info=True)
            self.jira_link_types = []
            return None

    def create_link_type_mapping(self) -> dict[str, Any] | None:
        """Create a mapping between Jira link types and OpenProject relation types.

        Returns:
            Dictionary representing the mapping or None if creation fails.

        """
        filepath = self.data_dir / "link_type_mapping.json"
        analysis_filepath = self.data_dir / "link_type_mapping_analysis.json"

        if (
            not config.migration_config.get("force", False)
            and filepath.exists()
            and analysis_filepath.exists()
        ):
            self.logger.info("Loading existing link type mapping from file.")
            try:
                with filepath.open() as f:
                    self.link_type_mapping = json.load(f)
                self.logger.info(f"Loaded {len(self.link_type_mapping)} link type mappings")
                # Optionally load and log analysis too
                with analysis_filepath.open() as f:
                    analysis = json.load(f)
                self.logger.info(f"Loaded mapping analysis: {analysis.get('message')}")
                self._build_id_mapping()
                return self.link_type_mapping
            except Exception as e:
                self.logger.warning(f"Could not load cached link type mapping: {e}")

        # Check if we have the necessary data
        if not self.jira_link_types:
            self.extract_jira_link_types()

        if not self.jira_link_types:
            self.logger.error("Cannot create mapping: Jira link types are missing.")
            return None

        self.logger.info("Creating link type mapping...")
        mapping = {}

        # Create lookup dictionaries for OpenProject relation types
        op_types_by_name = {op_type["name"].lower(): op_type for op_type in self.op_link_types}
        op_types_by_reverse_name = {
            op_type.get("reverseName", "").lower(): op_type
            for op_type in self.op_link_types
            if op_type.get("reverseName")
        }

        # Check for user-defined mappings first
        user_mapping_path = self.data_dir / "link_type_user_mapping.json"
        user_mappings = {}
        if user_mapping_path.exists():
            try:
                with user_mapping_path.open() as f:
                    user_mappings = json.load(f)
                self.logger.info(f"Loaded {len(user_mappings)} user-defined link type mappings")
            except Exception as e:
                self.logger.warning(f"Could not load user-defined link type mappings: {e}")

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
                "status": "pending",  # pending, matched, unmapped
                "create_custom_field": False,
            }

            # Check if there's a user-defined mapping for this Jira link type
            if jira_id in user_mappings:
                user_mapping = user_mappings[jira_id]
                op_id = user_mapping.get("openproject_id")

                # If user mapped to a valid OpenProject relation type
                if op_id and any(op_type["id"] == op_id for op_type in self.op_link_types):
                    op_type = next(op_type for op_type in self.op_link_types if op_type["id"] == op_id)
                    match_info.update(
                        {
                            "openproject_id": op_id,
                            "openproject_name": op_type["name"],
                            "openproject_reverse_name": op_type.get("reverseName"),
                            "matched_by": "user_defined",
                            "status": "matched",
                        },
                    )
                    mapping[jira_id] = match_info
                    continue
                # If user specified it should be a custom field
                if user_mapping.get("create_custom_field", False):
                    match_info.update(
                        {
                            "matched_by": "user_defined_custom_field",
                            "status": "unmapped",
                            "create_custom_field": True,
                        },
                    )
                    mapping[jira_id] = match_info
                    continue

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
                    },
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
                    },
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
                    },
                )
                mapping[jira_id] = match_info
                continue

            # 4. Similar matches using contains for jira_outward
            for op_type in self.op_link_types:
                op_name = op_type["name"].lower()
                if (jira_outward.lower() in op_name) or (op_name in jira_outward.lower()):
                    match_info.update(
                        {
                            "openproject_id": str(op_type["id"]),
                            "openproject_name": op_type["name"],
                            "openproject_reverse_name": op_type.get("reverseName"),
                            "matched_by": "similar_outward",
                            "status": "matched",
                        },
                    )
                    mapping[jira_id] = match_info
                    break

            # If still not matched, this will need a custom field
            if match_info["matched_by"] == "none":
                match_info.update(
                    {
                        "status": "unmapped",
                        "create_custom_field": True,
                    },
                )
                mapping[jira_id] = match_info

        self.link_type_mapping = mapping
        self._save_to_json(mapping, Path("link_type_mapping.json"))
        self._build_id_mapping()
        self.analyze_link_type_mapping()
        return mapping

    def _build_id_mapping(self) -> None:
        """Create a simple ID-to-ID mapping for quick lookups."""
        self.link_type_id_mapping = {}
        for jira_id, mapping_data in self.link_type_mapping.items():
            if mapping_data.get("openproject_id"):
                self.link_type_id_mapping[jira_id] = mapping_data["openproject_id"]

    def analyze_link_type_mapping(self) -> dict[str, Any]:
        """Analyze the link type mapping to identify potential issues.

        Returns:
            Dictionary with analysis results

        """
        if not self.link_type_mapping:
            mapping_path = self.data_dir / "link_type_mapping.json"
            if mapping_path.exists():
                with mapping_path.open() as f:
                    self.link_type_mapping = json.load(f)
            else:
                self.logger.error("No link type mapping found. Run create_link_type_mapping() first.")
                return {}

        analysis = {
            "total_types": len(self.link_type_mapping),
            "matched_types": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] != "none" and not type_data.get("create_custom_field", False)
            ),
            "matched_by_name": sum(
                1 for type_data in self.link_type_mapping.values() if type_data["matched_by"] == "name"
            ),
            "matched_by_outward": sum(
                1 for type_data in self.link_type_mapping.values() if type_data["matched_by"] == "outward"
            ),
            "matched_by_inward": sum(
                1 for type_data in self.link_type_mapping.values() if type_data["matched_by"] == "inward"
            ),
            "matched_by_similar": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data["matched_by"] in ["similar_outward", "similar_inward"]
            ),
            "matched_by_user": sum(
                1 for type_data in self.link_type_mapping.values() if type_data["matched_by"] == "user_defined"
            ),
            "unmapped_types": sum(
                1
                for type_data in self.link_type_mapping.values()
                if type_data.get("matched_by") == "none" or type_data.get("create_custom_field", False)
            ),
            "custom_field_count": sum(
                1 for type_data in self.link_type_mapping.values() if type_data.get("create_custom_field", False)
            ),
        }

        unmapped_types = []
        for jira_id, type_data in self.link_type_mapping.items():
            if type_data.get("matched_by") == "none" or type_data.get("create_custom_field", False):
                unmapped_types.append(
                    {
                        "jira_id": jira_id,
                        "jira_name": type_data["jira_name"],
                        "jira_outward": type_data["jira_outward"],
                        "jira_inward": type_data["jira_inward"],
                    },
                )

        analysis["unmapped_type_details"] = unmapped_types

        if analysis["unmapped_types"] > 0:
            analysis["message"] = (
                f"Analysis: Found {analysis['total_types']} link types. "
                f"{analysis['matched_types']} are matched to OpenProject relation types "
                f"({analysis['matched_by_name']} by name, {analysis['matched_by_outward']} by outward, "
                f"{analysis['matched_by_inward']} by inward, {analysis['matched_by_similar']} by similarity). "
                f"{analysis['unmapped_types']} link types need custom fields."
            )
        else:
            analysis["message"] = (
                f"Analysis: All {analysis['total_types']} link types are matched to OpenProject relation types "
                f"({analysis['matched_by_name']} by name, {analysis['matched_by_outward']} by outward, "
                f"{analysis['matched_by_inward']} by inward, {analysis['matched_by_similar']} by similarity)."
            )

        # Save analysis
        self._save_to_json(analysis, Path("link_type_mapping_analysis.json"))
        self.logger.info(f"Link type mapping analysis: {analysis['message']}")
        return analysis

    def _suggest_relation_types(self, jira_name: str, jira_outward: str, jira_inward: str) -> list[dict[str, Any]]:
        """Suggest appropriate OpenProject relation types for a given Jira link type.

        Args:
            jira_name: Name of the Jira link type
            jira_outward: Outward description
            jira_inward: Inward description

        Returns:
            List of suggested OpenProject relation types

        """
        # This is a stub to satisfy type checking. We don't need the implementation.
        return []

    def generate_user_mapping_template(self, output_path: str | None = None) -> str | None:
        """Generate a template for user-defined mappings.

        Args:
            output_path: Path to save the template file.

        Returns:
            Path to the generated template file or None if generation fails.
        """
        # This is a stub to satisfy type checking. We don't need the implementation.
        return None

    def create_custom_fields_for_link_types(
        self,
        unmapped_link_types: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        """Create custom fields in OpenProject for unmapped link types.

        Args:
            unmapped_link_types: List of tuples (jira_id, mapping_data) for link types that need custom fields

        Returns:
            Dictionary with results of the operation

        """
        self.logger.info(f"Creating custom fields for {len(unmapped_link_types)} unmapped link types")

        # Initialize a CustomFieldMigration instance
        custom_field_migration = CustomFieldMigration(jira_client=self.jira_client, op_client=self.op_client)

        # Prepare the fields to create
        fields_to_create = []

        for jira_id, mapping in unmapped_link_types:
            # Create a field definition for each unmapped link type
            field_definition = {
                "jira_id": jira_id,  # Use the Jira link type ID as a reference
                # Prefix with 'Link:' to distinguish link type fields
                "jira_name": f"Link: {mapping['jira_name']}",
                "openproject_type": "text",  # Use text field for link types
                "openproject_field_type": "WorkPackageCustomField",
                "is_required": False,
                "is_for_all": True,  # Make available for all work package types
                "description": (
                    f"Custom field for Jira link type: {mapping['jira_name']} "
                    f"(Outward: {mapping['jira_outward']}, Inward: {mapping['jira_inward']})"
                ),
            }

            fields_to_create.append(field_definition)

        if not fields_to_create:
            self.logger.info("No custom fields to create")
            return {"success": True, "created_count": 0, "error_count": 0, "message": "No custom fields to create"}

        # Use the batch method for efficiency
        success = custom_field_migration.migrate_custom_fields_via_json(fields_to_create)

        if not success:
            self.logger.error("Failed to create custom fields for link types")
            return {
                "success": False,
                "created_count": 0,
                "error_count": len(fields_to_create),
                "message": "Failed to create custom fields",
            }

        # Update our mapping with the newly created custom field IDs
        created_count = 0
        error_count = 0

        # Refresh the list of OpenProject custom fields
        op_custom_fields = custom_field_migration.extract_openproject_custom_fields()

        # Create a dictionary of OpenProject custom fields by name for easy lookup
        op_fields_by_name = {}
        for field in op_custom_fields:
            field_name = field.get("name", "").lower()
            if field_name:
                op_fields_by_name[field_name] = field

        # Update our mapping
        for jira_id, mapping in unmapped_link_types:
            field_name = f"Link: {mapping['jira_name']}".lower()

            if field_name in op_fields_by_name:
                # Found the custom field - update the mapping
                op_field = op_fields_by_name[field_name]
                self.link_type_mapping[jira_id].update(
                    {
                        "openproject_id": str(op_field.get("id")),
                        "openproject_name": op_field.get("name"),
                        "openproject_type": op_field.get("field_format", "text"),
                        "matched_by": "custom_field",
                        "status": "mapped",
                        "custom_field_id": op_field.get("id"),
                    },
                )
                created_count += 1
            else:
                error_count += 1
                self.logger.warning(f"Could not find created custom field: {field_name}")

        # Save the updated mapping
        self._save_to_json(self.link_type_mapping, Path("link_type_mapping.json"))

        return {
            "success": created_count > 0,
            "created_count": created_count,
            "error_count": error_count,
            "message": f"Created {created_count} custom fields for link types ({error_count} errors)",
        }
