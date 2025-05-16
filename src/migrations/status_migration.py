"""Status migration module for Jira to OpenProject migration.
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
from typing import Any, TypeVar

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import ProgressTracker
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult

# Get logger from config
logger = config.logger

# Define a type variable for ProgressTracker
T = TypeVar("T")


class StatusMigration(BaseMigration):
    """Handles the migration of statuses from Jira to OpenProject.

    This class is responsible for:
    1. Extracting statuses from Jira
    2. Creating corresponding statuses in OpenProject
    3. Creating and maintaining the status mapping
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
        mappings: Mappings | None = None,
        tracker: ProgressTracker[T] | None = None,
    ) -> None:
        """Initialize the status migration tools.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            mappings: Initialized Mappings instance
            tracker: Optional progress tracker instance

        """
        super().__init__(jira_client, op_client)

        self.mappings = mappings
        self.tracker = tracker

        # Initialize empty lists
        self.jira_statuses: list[dict[str, Any]] = []
        self.jira_status_categories: list[dict[str, Any]] = []
        self.op_statuses: list[dict[str, Any]] = []

        # Load existing status mappings
        self.status_mapping: dict[str, Any] = {}
        if self.mappings is not None and hasattr(self.mappings, "status_mapping"):
            self.status_mapping = self.mappings.status_mapping.copy()

        # Load existing data if available
        self._load_data()

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.jira_statuses = self._load_from_json("jira_statuses.json", [])
        self.jira_status_categories = self._load_from_json("jira_status_categories.json", [])
        self.op_statuses = self._load_from_json("op_statuses.json", [])

        logger.info(f"Loaded {len(self.jira_statuses)} Jira statuses")
        logger.info(f"Loaded {len(self.jira_status_categories)} Jira status categories")
        logger.info(f"Loaded {len(self.op_statuses)} OpenProject statuses")

    def extract_jira_statuses(self) -> list[dict[str, Any]]:
        """Extract all statuses from Jira using the Jira statuses API endpoint.

        Returns:
            List of Jira status dictionaries

        """
        statuses_file = os.path.join(self.data_dir, "jira_statuses.json")

        if os.path.exists(statuses_file) and not config.migration_config.get("force", False):
            logger.info("Jira statuses data already exists, skipping extraction (use --force to override)")
            with open(statuses_file) as f:
                self.jira_statuses = json.load(f)
            return self.jira_statuses

        logger.info("Extracting statuses from Jira...")

        try:
            # Check if jira_client and jira attribute exist
            if self.jira_client is None or not hasattr(self.jira_client, "jira") or self.jira_client.jira is None:
                logger.error("Jira client is not properly initialized")
                return []

            # Use the REST API endpoint for status retrieval
            response = self.jira_client.jira._get_json("status")
            statuses = response if response else []

            if not statuses:
                logger.warning("No statuses found in Jira")
                return []

            logger.info(f"Extracted {len(statuses)} statuses from Jira")

            self.jira_statuses = statuses
            self._save_to_json(statuses, "jira_statuses.json")

            return statuses
        except Exception as e:
            logger.exception(f"Failed to extract statuses from Jira: {e!s}")
            return []

    def extract_status_categories(self) -> list[dict[str, Any]]:
        """Extract status categories from Jira.

        Returns:
            List of Jira status category dictionaries

        """
        categories_file = os.path.join(self.data_dir, "jira_status_categories.json")

        if os.path.exists(categories_file) and not config.migration_config.get("force", False):
            logger.info("Jira status categories data already exists, skipping extraction (use --force to override)")
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
            logger.exception(f"Failed to extract status categories from Jira: {e!s}")
            return []

    def get_openproject_statuses(self) -> list[dict[str, Any]]:
        """Get all statuses from OpenProject.

        Returns:
            List of OpenProject status dictionaries

        """
        statuses_file = os.path.join(self.data_dir, "op_statuses.json")

        if os.path.exists(statuses_file) and not config.migration_config.get("force", False):
            logger.info("OpenProject statuses data already exists, skipping extraction (use --force to override)")
            with open(statuses_file) as f:
                self.op_statuses = json.load(f)
            return self.op_statuses

        logger.info("Getting statuses from OpenProject...")

        try:
            if self.op_client is None:
                logger.error("OpenProject client is not initialized")
                return []

            # Check if get_statuses method exists on the client
            if hasattr(self.op_client, "get_statuses"):
                statuses = self.op_client.get_statuses()
            else:
                # Use a direct query to get statuses if method is not available
                result = self.op_client.execute_query("Status.all.as_json")
                if result["status"] != "success":
                    logger.error("Failed to get statuses from OpenProject")
                    return []
                statuses = result["output"]

                # Convert output to proper format if needed
                if isinstance(statuses, str):
                    try:
                        # Handle Ruby output format
                        statuses = json.loads(statuses.replace("=>", ":").replace("nil", "null"))
                    except json.JSONDecodeError:
                        logger.exception("Failed to parse statuses from OpenProject")
                        return []

            # Ensure statuses is a list of dictionaries
            if not isinstance(statuses, list):
                statuses = [statuses]

            # Check if we have any statuses
            if not statuses:
                logger.warning("No statuses found in OpenProject")
                return []

            logger.info(f"Found {len(statuses)} statuses in OpenProject")

            # Explicitly convert to the expected return type
            result_statuses: list[dict[str, Any]] = []
            for status in statuses:
                if isinstance(status, dict):
                    result_statuses.append(status)
                else:
                    # Try to convert to a dictionary if possible
                    try:
                        result_statuses.append(dict(status))
                    except (TypeError, ValueError):
                        logger.warning(f"Skipping invalid status format: {status}")

            self.op_statuses = result_statuses
            self._save_to_json(result_statuses, "op_statuses.json")

            return result_statuses
        except Exception as e:
            logger.exception(f"Failed to get statuses from OpenProject: {e!s}")
            return []

    def create_statuses_bulk_via_rails(self, statuses_to_create: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Create multiple statuses in OpenProject using a single Rails console command.

        Args:
            statuses_to_create: List of status dictionaries with keys:
                - jira_id: Jira status ID
                - name: Status name
                - is_closed: Whether the status is considered 'closed'
                - is_default: Whether the status is the default status
                - color: Optional hex color code

        Returns:
            Dictionary mapping Jira status IDs to created OpenProject status info

        """
        if not statuses_to_create:
            logger.warning("No statuses provided for bulk creation")
            return {}

        logger.info(f"Creating {len(statuses_to_create)} statuses in bulk via Rails...")

        # Create Ruby script that processes the data passed in
        ruby_script = """
        begin
          puts "Starting bulk status creation..."

          # Results will be stored here
          results = {}

          # Process each status definition
          input_data.each do |status_def|
            jira_id = status_def["jira_id"]
            name = status_def["name"]
            is_closed = status_def["is_closed"] || false
            is_default = status_def["is_default"] || false
            color = status_def["color"]

            puts "Processing status '#{name}' (Jira ID: #{jira_id})..."

            # Check if status already exists
            existing_status = Status.find_by(name: name)

            if existing_status
              puts "Status '#{name}' already exists with ID: #{existing_status.id}"
              results[jira_id] = {
                "id" => existing_status.id,
                "name" => existing_status.name,
                "is_closed" => existing_status.is_closed,
                "already_existed" => true
              }
            else
              # Create new status
              new_status = Status.new(
                name: name,
                is_closed: is_closed,
                is_default: is_default
              )

              # Set position as the highest existing position + 1
              new_status.position = Status.maximum(:position).to_i + 1

              # Set color if provided
              new_status.color = color if color

              if new_status.save
                puts "SUCCESS: Created status '#{name}' with ID: #{new_status.id}"
                results[jira_id] = {
                  "id" => new_status.id,
                  "name" => new_status.name,
                  "is_closed" => new_status.is_closed,
                  "already_existed" => false
                }
              else
                puts "ERROR: Failed to create status '#{name}'. Validation errors:"
                new_status.errors.full_messages.each do |msg|
                  puts "  - #{msg}"
                end
                results[jira_id] = {
                  "error" => new_status.errors.full_messages.join("; "),
                  "already_existed" => false
                }
              end
            end
          end

          # Output the results for validation
          puts "Bulk status creation completed."
          puts "Created or found #{results.size} statuses"

          # Return results in JSON format
          puts "JSON_OUTPUT_START"
          puts results.to_json
          puts "JSON_OUTPUT_END"

          results
        rescue => e
          puts "EXCEPTION: #{e.class.name}: #{e.message}"
          puts "Backtrace: #{e.backtrace.join('\\n')}"
          puts "JSON_OUTPUT_START"
          puts {error: e.message}.to_json
          puts "JSON_OUTPUT_END"
        end
        """

        try:
            # Execute the script with the statuses data
            logger.debug("Executing bulk status creation via Rails with data")
            result = self.op_client.execute_script_with_data(script_content=ruby_script, data=statuses_to_create)

            if result.get("status") == "success" and "data" in result:
                created_statuses = result["data"]
                logger.info(f"Successfully processed {len(created_statuses)} statuses via Rails")
                # Ensure the return value has the expected type
                typed_statuses: dict[str, dict[str, Any]] = {}
                for jira_id, status_info in created_statuses.items():
                    # Ensure jira_id is a string key
                    str_jira_id = str(jira_id)
                    # Ensure status_info is a dictionary
                    if isinstance(status_info, dict):
                        typed_statuses[str_jira_id] = status_info
                    else:
                        try:
                            typed_statuses[str_jira_id] = dict(status_info)
                        except (TypeError, ValueError):
                            typed_statuses[str_jira_id] = {"error": "Invalid status format"}

                return typed_statuses
            error_msg = result.get("message", "Unknown error")
            logger.error(f"Failed to create statuses: {error_msg}")
            if "output" in result:
                logger.debug(f"Rails output: {result['output'][:500]}...")
            return {}

        except Exception as e:
            logger.exception(f"Exception during Rails bulk execution: {e!s}")
            return {}

    def create_status_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira statuses and OpenProject statuses.

        Returns:
            Dictionary mapping Jira status names to OpenProject status IDs

        """
        mapping_file = os.path.join(self.data_dir, "status_mapping.json")

        if os.path.exists(mapping_file) and not config.migration_config.get("force", False):
            logger.info("Status mapping already exists, loading from file (use --force to recreate)")
            with open(mapping_file) as f:
                self.status_mapping = json.load(f)
            return self.status_mapping

        logger.info("Creating status mapping...")

        if not self.jira_statuses:
            self.extract_jira_statuses()

        if not self.op_statuses:
            self.get_openproject_statuses()

        # Create mapping based on name
        mapping: dict[str, dict[str, Any]] = {}
        op_statuses_by_name = {s.get("name", "").lower(): s for s in self.op_statuses}

        for jira_status in self.jira_statuses:
            jira_id = jira_status.get("id")
            jira_name = jira_status.get("name", "")

            # Skip if jira_id is None
            if jira_id is None:
                logger.warning(f"Skipping Jira status with no ID: {jira_name}")
                continue

            # Ensure jira_id is a string
            jira_id = str(jira_id)

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
        """Migrate statuses from Jira to OpenProject.

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

        # If in dry run mode, simulate success for all statuses
        if config.migration_config.get("dry_run", False):
            logger.info("[DRY RUN] Simulating status migration success")

            for jira_status in self.jira_statuses:
                jira_id = jira_status.get("id")
                name = jira_status.get("name", "")

                # Check if it already exists in OpenProject
                for op_name, op_status in op_statuses_by_name.items():
                    if op_name.lower() == name.lower():
                        status_mapping[jira_id] = {
                            "openproject_id": op_status.get("id"),
                            "openproject_name": op_status.get("name"),
                        }
                        already_exists_count += 1
                        break
                else:
                    # Not found, simulate creation
                    status_mapping[jira_id] = {
                        "openproject_id": f"dry_run_{jira_id}",
                        "openproject_name": name,
                    }
                    created_count += 1

            # Ensure jira_id is a string to avoid type errors
            for jira_id in list(status_mapping.keys()):
                if not isinstance(jira_id, str):
                    status_mapping[str(jira_id)] = status_mapping.pop(jira_id)

            # Save status mapping
            self._save_to_json(status_mapping, "status_mapping.json")
            logger.info(
                f"[DRY RUN] Found {already_exists_count} existing statuses, would create {created_count} new statuses",
            )

            # Update the mappings instance
            if self.mappings is not None:
                # Convert the mapping to the expected type
                migration_status_mapping: dict[str, Any] = {}
                for k, v in status_mapping.items():
                    migration_status_mapping[str(k)] = v
                self.mappings.status_mapping = migration_status_mapping

            return {
                "status": "success",
                "total_processed": len(self.jira_statuses),
                "already_exists_count": already_exists_count,
                "created_count": created_count,
                "error_count": 0,
                "mapping": status_mapping,
            }

        # Prepare statuses to create in bulk
        statuses_to_create = []
        for jira_status in self.jira_statuses:
            jira_id = jira_status.get("id")
            name = jira_status.get("name")

            if not jira_id or not name:
                logger.warning(f"Skipping status with missing ID or name: {jira_status}")
                continue

            # Check if a similar status already exists in OpenProject
            exists = False
            for op_name, op_status in op_statuses_by_name.items():
                if op_name.lower() == name.lower():
                    # Use existing status
                    op_status_id = op_status.get("id")
                    logger.debug(
                        f"Using existing OpenProject status '{op_status.get('name')}' "
                        f"(ID: {op_status_id}) for Jira status '{name}'",
                    )
                    status_mapping[jira_id] = {
                        "openproject_id": op_status_id,
                        "openproject_name": op_status.get("name"),
                    }
                    already_exists_count += 1
                    exists = True
                    break

            if not exists:
                # Determine if status should be considered 'closed'
                is_closed = False
                if "statusCategory" in jira_status:
                    category_key = jira_status.get("statusCategory", {}).get("key", "")
                    is_closed = category_key.upper() == "DONE"

                # Determine a suitable color (optional)
                color = None
                if "statusCategory" in jira_status:
                    category_color = jira_status.get("statusCategory", {}).get("colorName", "")
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

                # Add to list for bulk creation
                statuses_to_create.append(
                    {
                        "jira_id": jira_id,
                        "name": name,
                        "is_closed": is_closed,
                        "is_default": False,  # Default to False for imported statuses
                        "color": color,
                    },
                )

        # Create statuses in bulk if there are any to create
        if statuses_to_create:
            logger.info(f"Creating {len(statuses_to_create)} statuses in bulk")

            creation_results = self.create_statuses_bulk_via_rails(statuses_to_create)

            # Process results
            for jira_id, result in creation_results.items():
                if "id" in result:
                    status_mapping[jira_id] = {
                        "openproject_id": result["id"],
                        "openproject_name": result["name"],
                    }

                    if result.get("already_existed", False):
                        # Count as already existing if not counted above
                        # (shouldn't happen in normal flow but included for safety)
                        pass
                    else:
                        created_count += 1
                else:
                    logger.error(
                        f"Failed to create status for Jira ID {jira_id}: {result.get('error', 'Unknown error')}",
                    )
                    error_count += 1

        # Save status mapping
        if status_mapping:
            mapping_file_path = os.path.join(self.data_dir, "status_mapping.json")
            self._save_to_json(status_mapping, "status_mapping.json")
            logger.info(f"Saved status mapping to {mapping_file_path}")

            # Update the mappings instance
            if self.mappings is not None:
                # Convert the mapping to the expected type
                migration_status_mapping: dict[str, Any] = {}
                for k, v in status_mapping.items():
                    migration_status_mapping[str(k)] = v
                self.mappings.status_mapping = migration_status_mapping

        logger.info("Status migration completed")
        logger.info(f"Total Jira statuses processed: {len(self.jira_statuses)}")
        logger.info(
            f"OpenProject statuses: {already_exists_count} existing, {created_count} created, {error_count} errors",
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
        """Analyze the status mapping to identify potential issues.

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
        mapped_statuses = sum(1 for s in self.status_mapping.values() if s.get("openproject_id") is not None)
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

    def run(self) -> ComponentResult:
        """Run the status migration process.

        Returns:
            ComponentResult with migration results

        """
        logger.info("Running status migration...")

        # Update instance variables
        self.mappings = config.mappings
        self.status_mapping = {}

        # Check if mappings exists and has status_mapping attribute
        if hasattr(config, "mappings") and config.mappings is not None and hasattr(config.mappings, "status_mapping"):
            self.status_mapping = config.mappings.status_mapping.copy()

        try:
            # Step 1: Extract Jira statuses
            self.jira_statuses = self.extract_jira_statuses()
            if not self.jira_statuses:
                return ComponentResult(
                    success=False,
                    errors=["Failed to extract Jira statuses"],
                    success_count=0,
                    failed_count=0,
                    total_count=0,
                )

            # Step 2: Extract Jira status categories
            self.jira_status_categories = self.extract_status_categories()

            # Step 3: Get OpenProject statuses
            self.op_statuses = self.get_openproject_statuses()

            # Step 4: Create status mapping if needed
            if not self.status_mapping:
                self.status_mapping = self.create_status_mapping()

            # Step 5: Migrate statuses - if in dry_run mode, simulate success for all
            if config.migration_config.get("dry_run", False):
                logger.info("[DRY RUN] Simulating status migration success")

                # Create simulated mapping for all statuses
                op_statuses_by_name = {s.get("name", "").lower(): s for s in self.op_statuses}
                for jira_status in self.jira_statuses:
                    jira_id: str = jira_status.get("id", "")
                    name: str = jira_status.get("name", "")

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
            # Real migration
            migration_result = self.migrate_statuses()

            return ComponentResult(
                success=migration_result.get("status", "success") == "success",
                message=migration_result.get("message", "Status migration completed"),
                success_count=migration_result.get("created_count", 0)
                + migration_result.get("already_exists_count", 0),
                failed_count=migration_result.get("error_count", 0),
                total_count=migration_result.get("total_processed", 0),
                details=migration_result,
            )

        except Exception as e:
            logger.exception(f"Error during status migration: {e!s}")
            return ComponentResult(
                success=False,
                errors=[f"Error during status migration: {e!s}"],
                success_count=0,
                failed_count=0,
                total_count=0,
            )
