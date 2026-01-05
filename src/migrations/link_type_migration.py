"""Link type migration module for Jira to OpenProject migration.
Handles the migration of link types from Jira to OpenProject relation types.
"""

import json
import time
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraApiError, JiraAuthenticationError, JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging, console
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.migrations.custom_field_migration import CustomFieldMigration
from src.models import ComponentResult, MigrationError

try:
    from src.config import logger  # type: ignore
except Exception:
    logger = configure_logging("INFO", None)

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


@register_entity_types("link_types", "relation_types")
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

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method enables idempotent workflow caching by providing a standard
        interface for entity retrieval. Called by run_with_change_detection() to fetch data
        with automatic thread-safe caching.

        Args:
            entity_type: The type of entities to retrieve (e.g., "link_types", "relation_types")

        Returns:
            List of entity dictionaries from Jira API

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        # Check if this is the entity type we handle
        if entity_type in ("link_types", "relation_types"):
            return self.jira_client.get_issue_link_types()

        # Raise error for unsupported types
        msg = (
            f"LinkTypeMigration does not support entity type: {entity_type}. "
            f"Supported types: ['link_types', 'relation_types']"
        )
        raise ValueError(msg)

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
            self._save_to_json(
                self.op_link_types,
                Path("openproject_relation_types.json"),
            )

            # 2. Create mapping
            self.create_link_type_mapping()

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

            # 3. Create custom fields for unmapped link types if needed (idempotent)
            if not unmapped_link_types:
                self.logger.info(
                    "All link types are mapped to default OpenProject relation types",
                )
                result.details["success_count"] = total_link_types
                result.message = f"All {total_link_types} link types were successfully mapped."
                result.details["status"] = "success"
                result.details["created_now"] = 0
                result.details["preexisting_count"] = total_link_types
            else:
                self.logger.info(
                    "Found %s link types that need custom fields",
                    len(unmapped_link_types),
                )
                if config.migration_config.get("dry_run", False):
                    self.logger.info(
                        "DRY RUN: Would create custom fields for unmapped link types",
                    )
                    result.details["success_count"] = total_link_types
                    result.message = (
                        f"DRY RUN: {len(unmapped_link_types)} link types would be created as custom fields."
                    )
                    result.details["status"] = "success"
                else:
                    # Create custom fields for unmapped link types
                    # The implementation pre-checks existing OP fields to avoid re-creation
                    cf_result = self.create_custom_fields_for_link_types(
                        unmapped_link_types,
                    )

                    # Update the result based on the custom field creation outcome
                    success_count = cf_result["success_count"]
                    failure_count = cf_result["failure_count"]
                    total_operations = success_count + failure_count
                    mapped_count = total_link_types - len(unmapped_link_types)

                    # Determine overall status based on results
                    if failure_count == 0:
                        # Full success: all operations succeeded
                        status = "success"
                        result.success = True
                        result.message = (
                            f"Successfully migrated all {total_link_types} link types: "
                            f"{mapped_count} mapped to standard relations, "
                            f"{success_count} created as custom fields."
                        )
                        self.logger.info(result.message)
                    elif 0 < failure_count < total_operations:
                        # Partial success: some operations failed, but some succeeded
                        status = "partial_success"
                        result.success = True  # Still considered successful since some operations completed
                        result.message = (
                            f"Partially migrated {total_link_types} link types: "
                            f"{mapped_count} mapped to standard relations, "
                            f"{success_count} created as custom fields, "
                            f"{failure_count} failed."
                        )
                        self.logger.warning(result.message)

                        # Add error details to result
                        if cf_result.get("errors"):
                            result.errors = (result.errors or []) + [
                                f"Failed to create custom field for '{error['jira_name']}': {error['error']}"
                                for error in cf_result["errors"]
                            ]
                    else:
                        # Complete failure: all operations failed
                        status = "failure"
                        result.success = False
                        result.message = (
                            f"Migration failed for {total_link_types} link types: "
                            f"{mapped_count} mapped to standard relations, "
                            f"but all {failure_count} custom field creations failed."
                        )
                        self.logger.error(result.message)

                        # Add error details to result
                        if cf_result.get("errors"):
                            result.errors = (result.errors or []) + [
                                f"Failed to create custom field for '{error['jira_name']}': {error['error']}"
                                for error in cf_result["errors"]
                            ]

                    # Update result details with accurate counts
                    result.details["success_count"] = mapped_count + success_count
                    result.details["custom_field_count"] = success_count
                    result.details["failed_count"] = failure_count
                    result.details["created_now"] = cf_result.get("created_now", 0)
                    result.details["preexisting_count"] = cf_result.get("preexisting_count", 0)
                    result.details["status"] = status

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

    def extract_jira_link_types(self) -> list[dict[str, Any]]:
        """Extract link types from Jira.

        Returns:
            List of Jira link type dictionaries

        Raises:
            MigrationError: If extraction fails

        """
        filepath = self.data_dir / "jira_link_types.json"
        if not config.migration_config.get("force", False) and filepath.exists():
            self.logger.info("Loading existing Jira link types from file.")
            try:
                with filepath.open() as f:
                    self.jira_link_types = json.load(f)
                self.logger.info(
                    "Loaded %s Jira link types from cache",
                    len(self.jira_link_types),
                )
                return self.jira_link_types
            except Exception as e:
                self.logger.warning("Could not load cached Jira link types: %s", e)

        self.logger.info("Extracting link types from Jira...")
        try:
            self.jira_link_types = self.jira_client.get_issue_link_types()
            if self.jira_link_types is not None:
                self.logger.info(
                    "Extracted %s link types from Jira",
                    len(self.jira_link_types),
                )
                self._save_to_json(self.jira_link_types, Path("jira_link_types.json"))
                return self.jira_link_types
            # None or empty is an error condition for link types
            msg = "Jira returned no link types"
            self.logger.error(msg)
            raise MigrationError(msg)
        except (JiraAuthenticationError, JiraApiError) as e:
            # Make auth/401 failures fatal: link types influence relation mapping
            msg = (
                "Failed to retrieve Jira link types due to authentication/API error (e.g., 401). "
                "Blocking migration to prevent incomplete relation mappings: "
                f"{e}"
            )
            self.logger.error(msg)
            raise MigrationError(msg) from e
        except Exception as e:
            msg = f"Error extracting link types from Jira: {e}"
            self.logger.error(msg, exc_info=True)
            raise MigrationError(msg) from e

    def create_link_type_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira link types and OpenProject relation types.

        Returns:
            Dictionary representing the mapping

        Raises:
            MigrationError: If mapping creation fails

        """
        filepath = self.data_dir / "link_type_mapping.json"
        analysis_filepath = self.data_dir / "link_type_mapping_analysis.json"

        if not config.migration_config.get("force", False) and filepath.exists() and analysis_filepath.exists():
            self.logger.info("Loading existing link type mapping from file.")
            try:
                with filepath.open() as f:
                    self.link_type_mapping = json.load(f)
                self.logger.info(
                    "Loaded %s link type mappings",
                    len(self.link_type_mapping),
                )
                # Optionally load and log analysis too
                with analysis_filepath.open() as f:
                    analysis = json.load(f)
                self.logger.info("Loaded mapping analysis: %s", analysis.get("message"))
                self._build_id_mapping()
                return self.link_type_mapping
            except Exception as e:
                self.logger.warning("Could not load cached link type mapping: %s", e)

        # Check if we have the necessary data
        if not self.jira_link_types:
            self.extract_jira_link_types()

        if not self.jira_link_types:
            # No link types available; return empty mapping and treat as success
            self.logger.info("No Jira link types available; creating empty mapping")
            self.link_type_mapping = {}
            from src import config as _cfg

            _cfg.mappings.set_mapping("link_type", self.link_type_mapping)
            self._build_id_mapping()
            self.analyze_link_type_mapping()
            return self.link_type_mapping

        self.logger.info("Creating link type mapping...")
        mapping = {}

        # Create lookup dictionaries for OpenProject relation types
        op_types_by_name = {(op_type.get("name") or "").lower(): op_type for op_type in self.op_link_types}
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
                self.logger.info(
                    "Loaded %s user-defined link type mappings",
                    len(user_mappings),
                )
            except Exception as e:
                self.logger.warning(
                    "Could not load user-defined link type mappings: %s",
                    e,
                )

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
            if (jira_name or "").lower() in op_types_by_name:
                op_type = op_types_by_name[(jira_name or "").lower()]
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
            if (jira_outward or "").lower() in op_types_by_name:
                op_type = op_types_by_name[(jira_outward or "").lower()]
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
            if (jira_inward or "").lower() in op_types_by_reverse_name:
                op_type = op_types_by_reverse_name[(jira_inward or "").lower()]
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
                op_name = (op_type.get("name") or "").lower()
                jira_outward_lower = (jira_outward or "").lower()
                if (jira_outward_lower in op_name) or (op_name in jira_outward_lower):
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
        from src import config as _cfg

        _cfg.mappings.set_mapping("link_type", mapping)
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
            from src import config as _cfg

            self.link_type_mapping = _cfg.mappings.get_mapping("link_type") or {}
            if not self.link_type_mapping:
                self.logger.error(
                    "No link type mapping found. Run create_link_type_mapping() first.",
                )
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
            if type_data.get("matched_by") == "none" or type_data.get(
                "create_custom_field",
                False,
            ):
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
        self.logger.info("Link type mapping analysis: %s", analysis["message"])
        return analysis

    def _suggest_relation_types(
        self,
        jira_name: str,
        jira_outward: str,
        jira_inward: str,
    ) -> list[dict[str, Any]]:
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

    def generate_user_mapping_template(
        self,
        output_path: str | None = None,
    ) -> str:
        """Generate a template for user-defined mappings.

        Args:
            output_path: Path to save the template file.

        Returns:
            Path to the generated template file

        Raises:
            MigrationError: If template generation fails

        """
        # This is a stub to satisfy type checking. We don't need the implementation.
        msg = "User mapping template generation not implemented"
        raise MigrationError(msg)

    def create_custom_fields_for_link_types(
        self,
        unmapped_link_types: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        """Create custom fields in OpenProject for unmapped link types.

        Args:
            unmapped_link_types: List of tuples (jira_id, mapping_data) for link types that need custom fields

        Returns:
            Dictionary with detailed results of the operation including:
            - success_count: Number of custom fields successfully created
            - failure_count: Number of custom fields that failed to create
            - errors: List of error details for failed creations
            - success: True if at least one field was created successfully

        """
        self.logger.info(
            "Creating custom fields for %s unmapped link types",
            len(unmapped_link_types),
        )

        # Initialize tracking variables
        success_count = 0
        failure_count = 0
        error_details: list[dict[str, Any]] = []

        # Initialize dependencies once
        custom_field_migration = CustomFieldMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
        )

        if not unmapped_link_types:
            self.logger.info("No custom fields to create")
            return {
                "success": True,
                "success_count": 0,
                "failure_count": 0,
                "errors": [],
                "message": "No custom fields to create",
            }

        # Build desired names and pre-resolve existing ones with a single refresh
        try:
            op_custom_fields = custom_field_migration.extract_openproject_custom_fields()
        except Exception as e:
            self.logger.warning("Failed to pre-load OpenProject custom fields: %s", e)
            op_custom_fields = []

        op_fields_by_name = {
            (field.get("name") or "").lower(): field for field in op_custom_fields if field.get("name")
        }

        # Pre-mark existing to avoid redundant creation attempts
        to_create: list[dict[str, Any]] = []
        for jira_id, mapping in unmapped_link_types:
            desired_name = f"Link: {mapping['jira_name']}".strip()
            lookup = desired_name.lower()
            if lookup in op_fields_by_name:
                op_field = op_fields_by_name[lookup]
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
                success_count += 1
            else:
                to_create.append(
                    {
                        "jira_id": jira_id,
                        "jira_name": desired_name,
                        "openproject_type": "text",
                        "openproject_field_type": "WorkPackageCustomField",
                        "is_required": False,
                        "is_for_all": True,
                        "description": (
                            f"Custom field for Jira link type: {mapping['jira_name']} "
                            f"(Outward: {mapping['jira_outward']}, Inward: {mapping['jira_inward']})"
                        ),
                    },
                )

        # If nothing to create, persist mapping and return
        if not to_create:
            from src import config as _cfg

            _cfg.mappings.set_mapping("link_type", self.link_type_mapping)
            return {
                "success": True,
                "success_count": success_count,
                "failure_count": 0,
                "created_now": 0,
                "preexisting_count": success_count,
                "errors": error_details,
                "message": f"Mapped {success_count} link types to existing custom fields",
            }

        # Create remaining custom fields in one batch
        batch_ok = custom_field_migration.migrate_custom_fields_via_json(to_create)

        # Refresh once and update mapping for all created
        try:
            op_custom_fields = custom_field_migration.extract_openproject_custom_fields()
        except Exception as e:
            self.logger.warning("Failed to refresh OpenProject custom fields after creation: %s", e)
            op_custom_fields = []

        op_fields_by_name = {
            (field.get("name") or "").lower(): field for field in op_custom_fields if field.get("name")
        }

        created_now = 0
        for fd in to_create:
            lookup = fd["jira_name"].lower()
            if lookup in op_fields_by_name:
                op_field = op_fields_by_name[lookup]
                jira_id = fd["jira_id"]
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
                created_now += 1
            else:
                failure_count += 1
                error_details.append(
                    {
                        "jira_id": fd["jira_id"],
                        "jira_name": fd["jira_name"],
                        "error": "Custom field not found after batch creation",
                    },
                )

        # success_count currently counts pre-existing; add newly created
        preexisting_count = success_count
        success_count += created_now

        # Save the updated mapping if any fields are now mapped
        if success_count > 0 or error_details:
            from src import config as _cfg

            _cfg.mappings.set_mapping("link_type", self.link_type_mapping)

            # Record provenance for link types with custom fields
            self._record_link_type_provenance()

        return {
            "success": failure_count == 0,
            "success_count": success_count,
            "failure_count": failure_count,
            "created_now": created_now,
            "preexisting_count": preexisting_count,
            "errors": error_details,
            "message": f"Resolved {success_count} link types via custom fields ({failure_count} errors)",
        }

    def _record_link_type_provenance(self) -> None:
        """Record provenance for link types with custom fields in J2O Migration project."""
        provenance_mappings = []
        for jira_id, entry in self.link_type_mapping.items():
            # Only record link types that have custom fields (unmapped ones)
            cf_id = entry.get("custom_field_id")
            if cf_id:
                provenance_mappings.append({
                    "jira_key": jira_id,
                    "jira_name": entry.get("jira_name"),
                    "op_entity_id": cf_id,
                })

        if provenance_mappings:
            try:
                result = self.op_client.bulk_record_entity_provenance(
                    "link_type", provenance_mappings
                )
                self.logger.info(
                    "Recorded link type provenance: %d success, %d failed",
                    result.get("success", 0),
                    result.get("failed", 0),
                )
            except Exception as prov_err:
                self.logger.warning("Failed to record link type provenance: %s", prov_err)

    def restore_mapping_from_openproject(self) -> dict[str, Any]:
        """Restore link type mapping from OpenProject provenance data alone.

        This method rebuilds the link type mapping by querying the J2O Migration
        provenance project for link type mapping work packages. It does NOT require
        Jira data, making it suitable for recovery scenarios where local mapping
        files are missing but OP contains provenance data from previous migrations.

        Returns:
            Dictionary keyed by Jira link type ID with OpenProject mapping data

        """
        self.logger.info("Restoring link type mapping from OpenProject provenance data...")

        # Query provenance registry for link type mappings
        provenance_mappings = self.op_client.restore_entity_mappings_from_provenance("link_type")

        if not provenance_mappings:
            self.logger.info("No link type provenance data found in OpenProject")
            return {}

        # Convert provenance format to standard mapping format
        mapping: dict[str, Any] = {}
        for jira_id, prov_data in provenance_mappings.items():
            mapping[jira_id] = {
                "jira_id": jira_id,
                "jira_name": prov_data.get("jira_name"),
                "custom_field_id": prov_data.get("openproject_id"),
                "openproject_id": str(prov_data.get("openproject_id")),
                "matched_by": "j2o_provenance",
                "status": "mapped",
                "restored_from_op": True,
                "provenance_wp_id": prov_data.get("provenance_wp_id"),
            }

        # Persist mapping via controller
        from src import config as _cfg
        self.link_type_mapping = mapping
        _cfg.mappings.set_mapping("link_type", mapping)

        self.logger.info("Restored %d link type mappings from OpenProject provenance", len(mapping))
        return mapping
