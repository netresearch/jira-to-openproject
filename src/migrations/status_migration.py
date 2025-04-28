"""
Status migration module for Jira to OpenProject migration.
Handles the migration of Jira statuses to OpenProject statuses.

Implementation is now complete, including:
- Status extraction from Jira and OpenProject
- Status mapping creation
- Status creation in OpenProject via Rails
- Automated and manual processes documented in docs/status_migration.md
- Test validation in tests/test_status_migration.py
"""

import json
import os
import re
from typing import Any

from src.models import ComponentResult
from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.display import ProgressTracker
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration

# Get logger from config
logger = config.logger


class StatusMigration(BaseMigration):
    """
    Handles the migration of statuses from Jira to OpenProject.

    This class is responsible for:
    1. Extracting statuses from Jira
    2. Creating corresponding statuses in OpenProject
    3. Creating and maintaining the status mapping
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
        op_rails_client: OpenProjectRailsClient | None = None,
        mappings: Mappings | None = None,
        data_dir: str | None = None,
        tracker: ProgressTracker | None = None,
        dry_run: bool = False,
    ):
        """
        Initialize the status migration tools.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            op_rails_client: Optional Rails client
            mappings: Initialized Mappings instance
            data_dir: Path to the data directory
            tracker: Optional progress tracker instance
            dry_run: If True, no changes will be made to OpenProject
        """
        super().__init__(jira_client, op_client, op_rails_client)

        self.mappings = mappings
        if data_dir:
            self.data_dir = data_dir
        self.tracker = tracker
        self.dry_run = dry_run

        # Initialize empty lists
        self.jira_statuses = []
        self.jira_status_categories = []
        self.op_statuses = []

        # Load existing status mappings
        self.status_mapping = (
            self.mappings.status_mapping.copy()
            if hasattr(self.mappings, "status_mapping")
            else {}
        )

        # Load existing data if available
        self._load_data()

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.jira_statuses = self._load_from_json("jira_statuses.json", [])
        self.jira_status_categories = self._load_from_json(
            "jira_status_categories.json", []
        )
        self.op_statuses = self._load_from_json("op_statuses.json", [])

        logger.info(f"Loaded {len(self.jira_statuses)} Jira statuses")
        logger.info(f"Loaded {len(self.jira_status_categories)} Jira status categories")
        logger.info(f"Loaded {len(self.op_statuses)} OpenProject statuses")

    def extract_jira_statuses(self, force=False) -> list[dict[str, Any]]:
        """
        Extract all statuses from Jira.

        Args:
            force: If True, extract again even if data already exists

        Returns:
            List of Jira status dictionaries
        """
        statuses_file = os.path.join(self.data_dir, "jira_statuses.json")

        if os.path.exists(statuses_file) and not force:
            logger.info(
                "Jira statuses data already exists, skipping extraction (use --force to override)"
            )
            with open(statuses_file) as f:
                self.jira_statuses = json.load(f)
            return self.jira_statuses

        logger.info("Extracting statuses from Jira...")

        try:
            statuses = self.jira_client.get_all_statuses()
            if not statuses:
                logger.warning("No statuses found in Jira")
                return []

            logger.info(f"Extracted {len(statuses)} statuses from Jira")

            self.jira_statuses = statuses
            self._save_to_json(statuses, "jira_statuses.json")

            return statuses
        except Exception as e:
            logger.error(f"Failed to extract statuses from Jira: {str(e)}")
            return []

    def extract_status_categories(self, force=False) -> list[dict[str, Any]]:
        """
        Extract status categories from Jira.

        Args:
            force: If True, extract again even if data already exists

        Returns:
            List of Jira status category dictionaries
        """
        categories_file = os.path.join(self.data_dir, "jira_status_categories.json")

        if os.path.exists(categories_file) and not force:
            logger.info(
                "Jira status categories data already exists, skipping extraction (use --force to override)"
            )
            with open(categories_file) as f:
                self.jira_status_categories = json.load(f)
            return self.jira_status_categories

        logger.info("Extracting status categories from Jira...")

        try:
            categories = self.jira_client.get_status_categories()
            if not categories:
                logger.warning("No status categories found in Jira")
                return []

            logger.info(f"Extracted {len(categories)} status categories from Jira")

            self.jira_status_categories = categories
            self._save_to_json(categories, "jira_status_categories.json")

            return categories
        except Exception as e:
            logger.error(f"Failed to extract status categories from Jira: {str(e)}")
            return []

    def get_openproject_statuses(self, force=False) -> list[dict[str, Any]]:
        """
        Get all statuses from OpenProject.

        Args:
            force: If True, get again even if data already exists

        Returns:
            List of OpenProject status dictionaries
        """
        statuses_file = os.path.join(self.data_dir, "op_statuses.json")

        if os.path.exists(statuses_file) and not force:
            logger.info(
                "OpenProject statuses data already exists, skipping extraction (use --force to override)"
            )
            with open(statuses_file) as f:
                self.op_statuses = json.load(f)
            return self.op_statuses

        logger.info("Getting statuses from OpenProject...")

        try:
            statuses = self.op_client.get_statuses()
            if not statuses:
                logger.warning("No statuses found in OpenProject")
                return []

            logger.info(f"Found {len(statuses)} statuses in OpenProject")

            self.op_statuses = statuses
            self._save_to_json(statuses, "op_statuses.json")

            return statuses
        except Exception as e:
            logger.error(f"Failed to get statuses from OpenProject: {str(e)}")
            return []

    def create_status_via_rails(
        self,
        name: str,
        is_closed: bool = False,
        is_default: bool = False,
        color: str = None,
    ) -> dict[str, Any] | None:
        """
        Create a status in OpenProject using the Rails console.

        Args:
            name: Name of the status
            is_closed: Whether the status is considered 'closed'
            is_default: Whether the status is the default status
            color: Hex color code (optional)

        Returns:
            Dictionary with created status information or None if creation failed
        """
        if not self.op_rails_client:
            logger.error("Rails client not available for status creation")
            return None

        logger.debug(f"Attempting to create status '{name}' via Rails...")

        # Escape single quotes in name
        safe_name = name.replace("'", "\\'")

        # Header section with Python f-string variables
        header_script = f"""
        # Status configuration variables
        status_name = '{safe_name}'
        is_closed_flag = {str(is_closed).lower()}
        is_default_flag = {str(is_default).lower()}
        """

        # Add color to header if provided
        if color:
            header_script += f"status_color = '{color}'\n"

        # Main Ruby section without f-strings
        main_script = """
        begin
          puts "Starting command execution..."

          # Check if status already exists
          existing_status = Status.find_by(name: status_name)

          if existing_status
            puts "Status already exists with ID: #{existing_status.id}"
            existing_status
          else
            # Create new status
            new_status = Status.new(
              name: status_name,
              is_closed: is_closed_flag,
              is_default: is_default_flag
            )

            # Set position as the highest existing position + 1
            new_status.position = Status.maximum(:position).to_i + 1
        """

        # Add color handling to main script if color was provided
        if color:
            main_script += """
            # Set color
            new_status.color = status_color
            """

        main_script += """
            if new_status.save
              puts "SUCCESS: Status created with ID: #{new_status.id}"
              new_status
            else
              puts "ERROR: Failed to save status. Validation errors:"
              new_status.errors.full_messages.each do |msg|
                puts "  - #{msg}"
              end
              nil
            end
          end
        rescue => e
          puts "EXCEPTION: #{e.class.name}: #{e.message}"
          nil
        end
        """

        # Combine the scripts
        command = header_script + main_script

        try:
            result = self.op_rails_client.execute(command, timeout=60)

            if result and "output" in result:
                output_str = result.get("output", "")

                # Check for success message with ID
                id_match = re.search(
                    r"Status already exists with ID: (\d+)|SUCCESS: Status created with ID: (\d+)",
                    output_str,
                )
                if id_match:
                    status_id = int(id_match.group(1) or id_match.group(2))
                    logger.debug(
                        f"Rails successfully found/created status '{name}' with ID: {status_id}"
                    )

                    return {
                        "id": status_id,
                        "name": name,
                        "is_closed": is_closed,
                        "is_default": is_default,
                    }

                # Log detailed error messages
                if "ERROR:" in output_str:
                    error_lines = re.findall(r"ERROR:.*|  - .*", output_str)
                    logger.error(f"Failed to create status '{name}'. Errors:")
                    for error in error_lines:
                        logger.error(f"  {error.strip()}")

                # Check for exception
                if "EXCEPTION:" in output_str:
                    exception_match = re.search(r"EXCEPTION: (.*)", output_str)
                    if exception_match:
                        logger.error(f"Exception in Rails: {exception_match.group(1)}")

                return None

            else:
                logger.error(
                    f"Rails command execution failed for status '{name}'. "
                    f"Status: {result.get('status')}, Error: {result.get('error')}"
                )
                return None

        except Exception as e:
            logger.error(
                f"Exception during Rails execution for status '{name}': {str(e)}"
            )
            return None

    def create_status_mapping(self, force=False) -> dict[str, Any]:
        """
        Create a mapping between Jira statuses and OpenProject statuses.

        Args:
            force: If True, create the mapping again even if it already exists

        Returns:
            Dictionary mapping Jira status names to OpenProject status IDs
        """
        mapping_file = os.path.join(self.data_dir, "status_mapping.json")

        if os.path.exists(mapping_file) and not force:
            logger.info(
                "Status mapping already exists, loading from file (use --force to recreate)"
            )
            with open(mapping_file) as f:
                self.status_mapping = json.load(f)
            return self.status_mapping

        logger.info("Creating status mapping...")

        if not self.jira_statuses:
            self.extract_jira_statuses()

        if not self.op_statuses:
            self.get_openproject_statuses()

        # Create mapping based on name
        mapping = {}
        op_statuses_by_name = {s.get("name", "").lower(): s for s in self.op_statuses}

        for jira_status in self.jira_statuses:
            jira_id = jira_status.get("id")
            jira_name = jira_status.get("name", "")

            # Check if a similar status exists in OpenProject
            op_status = None
            jira_name_lower = jira_name.lower()

            # Try exact match first
            if jira_name_lower in op_statuses_by_name:
                op_status = op_statuses_by_name[jira_name_lower]

            if op_status:
                mapping[jira_id] = {
                    "openproject_id": op_status.get("id"),
                    "openproject_name": op_status.get("name"),
                }
            else:
                # No match found
                mapping[jira_id] = {
                    "openproject_id": None,
                    "openproject_name": jira_name,
                }

        self.status_mapping = mapping
        self._save_to_json(mapping, "status_mapping.json")

        return mapping

    def migrate_statuses(self) -> dict[str, Any]:
        """
        Migrate statuses from Jira to OpenProject.

        Returns:
            Dictionary with migration summary and status mapping
        """
        logger.info("Starting status migration...")

        # Extract Jira statuses if not already done
        if not self.jira_statuses:
            self.jira_statuses = self.extract_jira_statuses()

        if not self.jira_statuses:
            return {"status": "error", "message": "No Jira statuses to migrate"}

        # Get existing OpenProject statuses if not already done
        if not self.op_statuses:
            self.op_statuses = self.get_openproject_statuses()

        # Create a name-based lookup for OpenProject statuses
        op_statuses_by_name = {s.get("name", "").lower(): s for s in self.op_statuses}

        # Create status mapping if not already done
        if not self.status_mapping:
            self.status_mapping = self.create_status_mapping()

        # Initialize counters
        created_count = 0
        already_exists_count = 0
        error_count = 0

        # Initialize status mapping
        status_mapping = {}

        # Process each Jira status
        with ProgressTracker(
            "Migrating statuses", len(self.jira_statuses), "Recent Statuses"
        ) as progress:
            for jira_status in self.jira_statuses:
                jira_id = jira_status.get("id")
                name = jira_status.get("name")

                if not jira_id or not name:
                    logger.warning(
                        f"Skipping status with missing ID or name: {jira_status}"
                    )
                    progress.increment()
                    continue

                progress.update_description(
                    f"Processing status: {name} (ID: {jira_id})"
                )

                # Determine if status should be considered 'closed'
                is_closed = False
                if "statusCategory" in jira_status:
                    category_key = jira_status.get("statusCategory", {}).get("key", "")
                    is_closed = category_key.upper() == "DONE"

                # Check if a similar status already exists in OpenProject
                existing_op_status = None
                for op_name, op_status in op_statuses_by_name.items():
                    if op_name.lower() == name.lower():
                        existing_op_status = op_status
                        break

                op_status_id = None

                if existing_op_status:
                    # Use existing status
                    op_status_id = existing_op_status.get("id")
                    logger.debug(
                        f"Using existing OpenProject status '{existing_op_status.get('name')}' "
                        f"(ID: {op_status_id}) for Jira status '{name}'"
                    )
                    already_exists_count += 1
                elif self.dry_run:
                    logger.info(
                        f"[DRY RUN] Would create status '{name}' (is_closed: {is_closed})"
                    )
                    # Simulate ID for dry run
                    op_status_id = f"dry_run_{jira_id}"
                    created_count += 1
                else:
                    # Create new status in OpenProject
                    # Use Rails console since API doesn't support status creation
                    if self.op_rails_client:
                        # Determine a suitable color (optional)
                        color = None
                        if "statusCategory" in jira_status:
                            category_color = jira_status.get("statusCategory", {}).get(
                                "colorName", ""
                            )
                            if category_color:
                                # Map Jira category colors to hex codes
                                color_mapping = {
                                    "blue-gray": "#4a6785",
                                    "yellow": "#f6c342",
                                    "green": "#14892c",
                                    "red": "#d04437",
                                    "medium-gray": "#8993a4",
                                }
                                color = color_mapping.get(category_color.lower())

                        # Create status via Rails
                        created_status = self.create_status_via_rails(
                            name=name,
                            is_closed=is_closed,
                            is_default=False,  # Default to False for imported statuses
                            color=color,
                        )

                        if created_status:
                            op_status_id = created_status.get("id")
                            logger.info(
                                f"Created new OpenProject status '{name}' (ID: {op_status_id}, is_closed: {is_closed})"
                            )
                            created_count += 1

                            # Add to local cache of OP statuses
                            op_statuses_by_name[name.lower()] = created_status
                        else:
                            logger.error(
                                f"Failed to create status '{name}' in OpenProject"
                            )
                            error_count += 1
                    else:
                        logger.error(
                            "Rails client not available, cannot create statuses"
                        )
                        error_count += 1

                # Update mapping if we got an OpenProject status ID
                if op_status_id:
                    status_mapping[jira_id] = {
                        "openproject_id": op_status_id,
                        "openproject_name": name,
                    }

                progress.increment()

        # Save status mapping
        if status_mapping:
            mapping_file_path = os.path.join(self.data_dir, "status_mapping.json")
            self._save_to_json(status_mapping, "status_mapping.json")
            logger.info(f"Saved status mapping to {mapping_file_path}")

            # Update the mappings instance
            if self.mappings:
                self.mappings.status_mapping = status_mapping

        logger.success("Status migration completed")
        logger.info(f"Total Jira statuses processed: {len(self.jira_statuses)}")
        logger.info(
            f"OpenProject statuses: {already_exists_count} existing, {created_count} created, {error_count} errors"
        )

        return {
            "status": "success",
            "total_processed": len(self.jira_statuses),
            "already_exists_count": already_exists_count,
            "created_count": created_count,
            "error_count": error_count,
            "mapping": status_mapping,
        }

    def analyze_status_mapping(self) -> dict[str, Any]:
        """
        Analyze the status mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        logger.info("Analyzing status mapping...")

        if not self.status_mapping:
            self.status_mapping = self._load_from_json("status_mapping.json", {})

        if not self.status_mapping:
            return {
                "status": "warning",
                "message": "No status mapping found",
                "statuses_count": 0,
            }

        # Count statuses
        total_statuses = len(self.status_mapping)
        mapped_statuses = sum(
            1
            for s in self.status_mapping.values()
            if s.get("openproject_id") is not None
        )
        unmapped_statuses = total_statuses - mapped_statuses

        # List unmapped statuses
        unmapped = [
            {"jira_id": jira_id, "name": status.get("openproject_name")}
            for jira_id, status in self.status_mapping.items()
            if status.get("openproject_id") is None
        ]

        return {
            "status": "success",
            "statuses_count": total_statuses,
            "mapped_count": mapped_statuses,
            "unmapped_count": unmapped_statuses,
            "unmapped_statuses": unmapped[:10],  # Limit to first 10
            "message": f"Found {mapped_statuses}/{total_statuses} mapped statuses",
        }

    def run(
        self, dry_run: bool = False, force: bool = False
    ) -> ComponentResult:
        """
        Run the status migration process.

        Args:
            dry_run: If True, no changes will be made to OpenProject
            force: If True, force extraction of data even if it already exists

        Returns:
            ComponentResult with migration results
        """
        logger.info("Running status migration...")

        # Update instance variables
        self.dry_run = dry_run
        self.mappings = config.mappings
        self.status_mapping = (
            config.mappings.status_mapping if hasattr(config.mappings, "status_mapping") else {}
        )

        try:
            # Step 1: Extract Jira statuses
            self.jira_statuses = self.extract_jira_statuses(force=force)
            if not self.jira_statuses:
                return {
                    "status": "failed",
                    "error": "Failed to extract Jira statuses",
                    "success_count": 0,
                    "failed_count": 0,
                    "total_count": 0,
                }

            # Step 2: Extract Jira status categories
            self.jira_status_categories = self.extract_status_categories(force=force)

            # Step 3: Get OpenProject statuses
            self.op_statuses = self.get_openproject_statuses(force=force)

            # Step 4: Create status mapping if needed
            if not self.status_mapping or force:
                self.status_mapping = self.create_status_mapping(force=force)

            # Step 5: Migrate statuses - if in dry_run mode, simulate success for all
            if dry_run:
                logger.info("[DRY RUN] Simulating status migration success")

                # Create simulated mapping for all statuses
                op_statuses_by_name = {
                    s.get("name", "").lower(): s for s in self.op_statuses
                }
                for jira_status in self.jira_statuses:
                    jira_id = jira_status.get("id")
                    name = jira_status.get("name", "")

                    # Check if it already exists in OpenProject
                    existing = False
                    for op_name, op_status in op_statuses_by_name.items():
                        if op_name.lower() == name.lower():
                            existing = True
                            # Use the actual existing status ID
                            self.status_mapping[jira_id] = {
                                "openproject_id": op_status.get("id"),
                                "openproject_name": op_status.get("name"),
                            }
                            break

                    # If not existing, create a simulated ID
                    if not existing:
                        self.status_mapping[jira_id] = {
                            "openproject_id": f"dry_run_{jira_id}",
                            "openproject_name": name,
                        }

                # Save the mapping
                self._save_to_json(self.status_mapping, "status_mapping.json")

                # If we have a mappings object, update it
                if self.mappings:
                    self.mappings.status_mapping = self.status_mapping

                return ComponentResult(
                    success=True,
                    message="[DRY RUN] Simulated status migration",
                    success_count=len(self.jira_statuses),
                    failed_count=0,
                    total_count=len(self.jira_statuses),
                    details={
                        "status": "success",
                        "total_processed": len(self.jira_statuses),
                        "already_exists_count": 0,
                        "created_count": len(self.jira_statuses),
                        "error_count": 0,
                    },
                )
            else:
                # Real migration
                migration_result = self.migrate_statuses()

                return ComponentResult(
                    success=True if "success" == migration_result.get("status", "success") else False,
                    message=migration_result.get(
                        "message", "Status migration completed"
                    ),
                    success_count=migration_result.get("created_count", 0)
                    + migration_result.get("already_exists_count", 0),
                    failed_count=migration_result.get("error_count", 0),
                    total_count=migration_result.get("total_processed", 0),
                    details=migration_result,
                )

        except Exception as e:
            logger.error(f"Error during status migration: {str(e)}")
            return ComponentResult(
                success=False,
                error=f"Error during status migration: {str(e)}",
                success_count=0,
                failed_count=0,
                total_count=0,
            )
