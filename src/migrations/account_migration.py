"""Account migration module for Jira to OpenProject migration.

Handles the migration of Tempo timesheet accounts as custom fields in OpenProject.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import config
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult, MigrationError

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

# Constants for filenames
ACCOUNT_MAPPING_FILE = "account_mapping.json"
TEMPO_ACCOUNTS_FILE = "tempo_accounts.json"


@register_entity_types("accounts", "tempo_accounts")
class AccountMigration(BaseMigration):
    """Handles the migration of accounts from Tempo timesheet to OpenProject.

    This class is responsible for:
    1. Extracting account information from Tempo timesheet in Jira
    2. Creating custom fields in OpenProject to store account information
    3. Mapping these accounts to be used later when creating Jira projects and work packages

    The approach is:
    - Tempo Company → OpenProject top-level project (created by company_migration.py)
    - Tempo Account → Custom field in OpenProject projects and work packages
    - Jira Project → OpenProject project with account information stored in custom fields
    """

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
    ) -> None:
        """Initialize the account migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.

        """
        super().__init__(jira_client, op_client)
        self.tempo_accounts: list = []
        self.op_projects: list = []
        self.account_mapping: dict = {}
        self.company_mapping: dict = {}
        self._created_accounts: int = 0

        self.account_custom_field_id: int | None = None

        # Load existing data if available
        self.tempo_accounts = self._load_from_json(Mappings.TEMPO_ACCOUNTS_FILE) or []
        self.op_projects = self._load_from_json(Mappings.OP_PROJECTS_FILE) or []
        self.account_mapping = config.mappings.get_mapping(
            Mappings.ACCOUNT_MAPPING_FILE,
        )

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.tempo_accounts = self._load_from_json(Mappings.TEMPO_ACCOUNTS_FILE) or []
        self.op_projects = self._load_from_json(Mappings.OP_PROJECTS_FILE) or []
        self.company_mapping = config.mappings.get_mapping(
            Mappings.COMPANY_MAPPING_FILE,
        )
        self.account_mapping = config.mappings.get_mapping(
            Mappings.ACCOUNT_MAPPING_FILE,
        )

        analysis_data = self._load_from_json("account_mapping_analysis.json", {})
        custom_field_id = analysis_data.get("custom_field_id")

        # Validate the custom field ID
        if custom_field_id == "nil" or custom_field_id is None:
            self.account_custom_field_id = None
        else:
            try:
                self.account_custom_field_id = int(custom_field_id)
            except (ValueError, TypeError):
                self.logger.warning(
                    "Invalid custom field ID in analysis file: %s, will create a new one",
                    custom_field_id,
                )
                self.account_custom_field_id = None

        self.logger.info("Loaded %d Tempo accounts", len(self.tempo_accounts))
        self.logger.info("Loaded %d OpenProject projects", len(self.op_projects))
        self.logger.info("Loaded %d company mappings", len(self.company_mapping))
        self.logger.info("Loaded %d account mappings", len(self.account_mapping))
        if self.account_custom_field_id:
            self.logger.info(
                "Loaded existing custom field ID: %d",
                self.account_custom_field_id,
            )
        else:
            self.logger.info("No valid custom field ID found, will create a new one")

    def extract_tempo_accounts(self, force: bool = False) -> list:
        """Extract Tempo accounts using the JiraClient.

        Args:
            force: If True, forces re-extraction even if cached data exists.

        Returns:
            List of Tempo account dictionaries.

        Raises:
            MigrationError: If accounts cannot be extracted from Tempo

        """
        if (
            self.tempo_accounts
            and not config.migration_config.get("force", False)
            and not force
        ):
            self.logger.info(
                "Using cached Tempo accounts from %s",
                Mappings.TEMPO_ACCOUNTS_FILE,
            )
            return self.tempo_accounts

        self.logger.info("Extracting Tempo accounts...")

        try:
            # Get accounts from Tempo API
            accounts = self.jira_client.get_tempo_accounts(expand=True)

            if accounts is not None:
                self.logger.info("Retrieved %d Tempo accounts", len(accounts))
                self.tempo_accounts = accounts
                self._save_to_json(accounts, Mappings.TEMPO_ACCOUNTS_FILE)
                return accounts
            error_msg = "Failed to retrieve Tempo accounts using JiraClient"
            self.logger.error(error_msg)
            raise MigrationError(error_msg)

        except Exception as e:
            error_msg = f"Failed to extract Tempo accounts: {e!s}"
            self.logger.exception(error_msg)
            raise MigrationError(error_msg) from e

    def extract_openproject_projects(self) -> list:
        """Get a list of all projects in OpenProject.

        Returns:
            List of OpenProject projects

        Raises:
            MigrationError: If unable to get projects from OpenProject

        """
        self.logger.info("Extracting projects from OpenProject...")

        # Get projects from OpenProject - no fallbacks or mocks
        self.op_projects = self.op_client.get_projects()

        if not self.op_projects:
            self.logger.warning(
                "Failed to get projects from OpenProject - no projects found. "
                "This may be due to JSON parsing issues. Continuing with empty project list."
            )
            self.op_projects = []

        # Save projects for future reference
        self._save_to_json(self.op_projects, "openproject_projects.json")

        self.logger.info(
            "Extracted %d projects from OpenProject",
            len(self.op_projects),
        )
        return self.op_projects

    def create_account_mapping(self) -> dict:
        """Create a mapping between Tempo accounts and OpenProject sub-projects.

        Returns:
            Dictionary mapping Tempo account IDs to OpenProject project IDs

        Raises:
            MigrationError: If required data is missing

        """
        self.logger.info("Creating account mapping...")

        if not self.tempo_accounts:
            self.extract_tempo_accounts()

        if not self.op_projects:
            self.extract_openproject_projects()

        op_projects_by_name = {
            project.get("name", "").lower(): project for project in self.op_projects
        }

        jira_project_mapping = self._load_from_json("jira_project_mapping.json", {})

        mapping = {}
        for tempo_account in self.tempo_accounts:
            tempo_id = tempo_account.get("id")
            tempo_key = tempo_account.get("key")
            tempo_name = tempo_account.get("name", "")
            tempo_name_lower = tempo_name.lower()
            company_id = tempo_account.get("customer", {}).get("id")
            default_project = tempo_account.get("default_project", {})
            default_project_key = default_project.get("key")

            op_project = op_projects_by_name.get(tempo_name_lower)
            match_method = "name" if op_project else "none"

            if (
                not op_project
                and default_project_key
                and default_project_key in jira_project_mapping
            ):
                op_project_id = jira_project_mapping[default_project_key].get(
                    "openproject_id",
                )
                if op_project_id:
                    op_project = next(
                        (
                            p
                            for p in self.op_projects
                            if str(p.get("id")) == str(op_project_id)
                        ),
                        None,
                    )
                    match_method = "default_project"

            if op_project:
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "tempo_lead": tempo_account.get("lead", {}).get("username"),
                    "tempo_contact": tempo_account.get("contact", {}).get("username"),
                    "tempo_status": tempo_account.get("status"),
                    "tempo_customer_id": tempo_account.get("customer", {}).get("id"),
                    "tempo_category": tempo_account.get("category", {}).get("name"),
                    "tempo_category_type": tempo_account.get("category", {})
                    .get("categorytype", {})
                    .get("name"),
                    "company_id": company_id,
                    "default_project_key": default_project_key,
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "parent_id": (
                        op_project.get("_links", {})
                        .get("parent", {})
                        .get("href", "")
                        .split("/")[-1]
                        if op_project.get("_links", {}).get("parent", {}).get("href")
                        else None
                    ),
                    "matched_by": match_method,
                }
            else:
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "tempo_lead": tempo_account.get("lead", {}).get("username"),
                    "tempo_contact": tempo_account.get("contact", {}).get("username"),
                    "tempo_status": tempo_account.get("status"),
                    "tempo_customer_id": tempo_account.get("customer", {}).get("id"),
                    "tempo_category": tempo_account.get("category", {}).get("name"),
                    "tempo_category_type": tempo_account.get("category", {})
                    .get("categorytype", {})
                    .get("name"),
                    "company_id": company_id,
                    "default_project_key": default_project_key,
                    "openproject_id": None,
                    "openproject_identifier": None,
                    "openproject_name": None,
                    "parent_id": None,
                    "matched_by": "none",
                }

        self.account_mapping = mapping
        self._save_to_json(mapping, Mappings.ACCOUNT_MAPPING_FILE)

        total_accounts = len(mapping)
        matched_accounts = sum(
            1 for account in mapping.values() if account["matched_by"] != "none"
        )
        name_matched = sum(
            1 for account in mapping.values() if account["matched_by"] == "name"
        )
        default_project_matched = sum(
            1
            for account in mapping.values()
            if account["matched_by"] == "default_project"
        )
        match_percentage = (
            (matched_accounts / total_accounts) * 100 if total_accounts > 0 else 0
        )

        self.logger.info(
            "Account mapping created for %d accounts",
            total_accounts,
        )
        self.logger.info(
            "Successfully matched %d accounts (%f%%)",
            matched_accounts,
            match_percentage,
        )
        if name_matched > 0:
            self.logger.info(
                "  - %d accounts matched by name",
                name_matched,
            )
        if default_project_matched > 0:
            self.logger.info(
                "  - %d accounts matched by default project",
                default_project_matched,
            )

        self.logger.info(
            "Unmatched accounts: %d - these will be available in the custom field but not linked to projects",
            total_accounts - matched_accounts,
        )

        return mapping

    def create_account_custom_field(self) -> int:
        """Create a custom field in OpenProject for Tempo accounts.

        Returns:
            ID of the custom field

        Raises:
            MigrationError: If custom field creation fails

        """
        self.logger.info(
            "Ensuring 'Tempo Account' custom field exists in OpenProject...",
        )

        # First, check if field already exists
        try:
            existing_id = self.get_existing_custom_field_id()
            self.logger.info(
                "Found existing Tempo Account custom field with ID %d",
                existing_id,
            )
            self.account_custom_field_id = existing_id
            return existing_id
        except MigrationError:
            # Field doesn't exist, proceed to create it
            self.logger.info("Custom field doesn't exist, will create it")

        # Create the custom field using direct API
        self.logger.info("Creating Tempo Account custom field...")

        field_options = {
            "name": "Tempo Account",
            "field_format": "string",
            "is_required": False,
            "searchable": True,
            "is_filter": True,
            "type": "WorkPackageCustomField",
        }

        # Create the field
        result = self.op_client.create_record("CustomField", field_options)
        self.logger.debug(
            f"Result from create_record: type={type(result)}, value={result}"
        )

        # Check if field was created successfully or already exists
        if isinstance(result, dict) and "id" in result:
            # Check if this is an existing field
            if result.get("status") == "existing":
                self.account_custom_field_id = result["id"]
                self.logger.info(
                    "Found existing Tempo Account custom field with ID %d",
                    self.account_custom_field_id,
                )
            else:
                # New field was created
                self.account_custom_field_id = result["id"]
                self.logger.info(
                    "Created Tempo Account custom field with ID %d",
                    self.account_custom_field_id,
                )
        # Check if field already exists (expected error)
        elif isinstance(result, dict) and "error" in result:
            error_messages = result.get("error", [])
            if any("already been taken" in str(msg) for msg in error_messages):
                self.logger.info(
                    "Tempo Account custom field already exists, attempting to find existing field..."
                )
                # Try to get the existing field ID
                try:
                    field_id = self.get_account_custom_field_id()
                    if field_id:
                        self.account_custom_field_id = field_id
                        self.logger.info(
                            "Found existing Tempo Account custom field with ID %d",
                            field_id,
                        )
                    else:
                        self.logger.warning(
                            "Custom field already exists but couldn't retrieve its ID"
                        )
                except Exception as e:
                    self.logger.warning(
                        "Failed to retrieve existing custom field ID: %s", e
                    )
            else:
                msg = f"Failed to create custom field: OpenProject error: {error_messages}"
                raise MigrationError(msg)
        else:
            msg = (
                f"Failed to create custom field: Invalid response from OpenProject "
                f"(type={type(result)}, value={result})"
            )
            raise MigrationError(msg)

        # Associate with all work package types
        self.associate_field_with_work_package_types(self.account_custom_field_id)

        return self.account_custom_field_id

    def run(self) -> ComponentResult:
        """Run the account migration process.

        Returns:
            ComponentResult with migration results

        """
        self.logger.info("Starting account migration")

        try:
            self._load_data()

            # Extract data
            self.extract_tempo_accounts()
            self.extract_openproject_projects()

            # Create mapping
            self.create_account_mapping()

            # Ensure the custom field exists
            self.create_account_custom_field()

            # Analyze results
            analysis = self.analyze_account_mapping()

            # Update mappings in global configuration
            config.mappings.set_mapping("accounts", self.account_mapping)

            return ComponentResult(
                success=True,
                data=analysis,
                success_count=analysis["matched_accounts"],
                failed_count=analysis["unmatched_accounts"],
                total_count=analysis["total_accounts"],
            )
        except Exception as e:
            self.logger.exception(
                "Error during account migration",
            )
            return ComponentResult(
                success=False,
                errors=[f"Error during account migration: {e!s}"],
                success_count=0,
                failed_count=len(self.tempo_accounts) if self.tempo_accounts else 0,
                total_count=len(self.tempo_accounts) if self.tempo_accounts else 0,
            )

    def analyze_account_mapping(self) -> dict[str, Any]:
        """Analyze the account mapping to identify potential issues.

        Returns:
            Dictionary with analysis results

        """
        if not self.account_mapping:
            mapping_path = Path(self.data_dir) / Mappings.ACCOUNT_MAPPING_FILE
            if mapping_path.exists():
                with mapping_path.open() as f:
                    self.account_mapping = json.load(f)
            else:
                self.logger.error(
                    "No account mapping found. Run create_account_mapping() first.",
                )
                return {}

        analysis = {
            "total_accounts": len(self.account_mapping),
            "matched_accounts": sum(
                1
                for account in self.account_mapping.values()
                if account.get("custom_field_id") is not None
            ),
            "custom_field_id": self.account_custom_field_id,
            "unmatched_accounts": sum(
                1
                for account in self.account_mapping.values()
                if account.get("custom_field_id") is None
            ),
            "unmatched_details": [
                {
                    "tempo_id": account["tempo_id"],
                    "tempo_key": account["tempo_key"],
                    "tempo_name": account["tempo_name"],
                    "company_id": account.get("company_id"),
                    "default_project_key": account.get("default_project_key"),
                }
                for account in self.account_mapping.values()
                if account.get("custom_field_id") is None
            ],
        }

        analysis["match_methods"] = {
            "name": sum(
                1
                for account in self.account_mapping.values()
                if account.get("matched_by") == "name"
            ),
            "default_project": sum(
                1
                for account in self.account_mapping.values()
                if account.get("matched_by") == "default_project"
            ),
            "none": sum(
                1
                for account in self.account_mapping.values()
                if account.get("matched_by") == "none"
            ),
        }

        total = analysis["total_accounts"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_accounts"] / total) * 100
        else:
            analysis["match_percentage"] = 0

        analysis["summary"] = {
            "total_tempo_accounts": len(self.tempo_accounts),
            "total_openproject_projects": len(self.op_projects),
            "accounts_matched_to_projects": sum(
                1
                for account in self.account_mapping.values()
                if account.get("openproject_id") is not None
            ),
            "accounts_without_project_match": sum(
                1
                for account in self.account_mapping.values()
                if account.get("openproject_id") is None
            ),
            "custom_field_created": self.account_custom_field_id is not None,
            "custom_field_id": self.account_custom_field_id,
        }

        self._save_to_json(analysis, "account_mapping_analysis.json")

        self.logger.info("=== Account Migration Summary ===")
        self.logger.info(
            "Total Tempo accounts processed: %d",
            analysis["summary"]["total_tempo_accounts"],
        )
        self.logger.info(
            "OpenProject projects available: %d",
            analysis["summary"]["total_openproject_projects"],
        )
        self.logger.info(
            "Accounts matched to projects: %d (%f%%)",
            analysis["summary"]["accounts_matched_to_projects"],
            analysis["match_percentage"],
        )
        if analysis["match_methods"]["name"] > 0:
            self.logger.info(
                "  - Matched by name: %d",
                analysis["match_methods"]["name"],
            )
        if analysis["match_methods"]["default_project"] > 0:
            self.logger.info(
                "  - Matched by default project: %d",
                analysis["match_methods"]["default_project"],
            )
        self.logger.info(
            "Accounts added to custom field but not matched to projects: %d",
            analysis["summary"]["accounts_without_project_match"],
        )
        self.logger.info(
            "Custom field ID in OpenProject: %d",
            analysis["summary"]["custom_field_id"],
        )
        self.logger.info("=====================")

        return analysis

    def _save_custom_field_id(self, cf_id: int) -> None:
        """Save the custom field ID to the analysis file.

        Args:
            cf_id: The custom field ID to save

        """
        # Validate the custom field ID
        if cf_id is None or cf_id == "nil":
            self.logger.warning("Not saving None or 'nil' as custom field ID")
            return

        try:
            # Ensure it's a valid integer
            cf_id = int(cf_id)
        except (ValueError, TypeError):
            self.logger.warning(
                "Not saving invalid custom field ID: %s",
                cf_id,
            )
            return

        analysis = self._load_from_json("account_mapping_analysis.json", {})
        analysis["custom_field_id"] = cf_id
        self._save_to_json(analysis, "account_mapping_analysis.json")
        self.account_custom_field_id = cf_id

    def migrate_accounts(self) -> dict:
        """Migrate accounts from Tempo to OpenProject.

        Returns:
            Dictionary of mapped accounts

        """
        self.logger.info("Starting account migration")

        # Extract accounts if needed
        if not self.tempo_accounts:
            self.extract_tempo_accounts()

        # Create/get custom field for accounts
        if self.account_custom_field_id is None:
            self.create_account_custom_field()

        # Create mapping
        if not self.account_mapping:
            self.create_account_mapping()

        # Add custom field ID to all accounts in the mapping
        for account_id in self.account_mapping:
            self.account_mapping[account_id][
                "custom_field_id"
            ] = self.account_custom_field_id

        # Save the updated mapping
        self._save_to_json(self.account_mapping, ACCOUNT_MAPPING_FILE)

        # Return the mapping
        return self.account_mapping

    def get_existing_custom_field_id(self) -> int:
        """Check if 'Tempo Account' custom field already exists.

        Returns:
            ID of the existing custom field

        Raises:
            MigrationError: If custom field does not exist or cannot be retrieved

        """
        try:
            self.logger.info("Checking if 'Tempo Account' custom field exists...")
            existing_id = self.op_client.get_custom_field_id_by_name("Tempo Account")

            if existing_id is None:
                self.logger.info("No existing 'Tempo Account' custom field found")
                raise MigrationError("Tempo Account custom field does not exist")

            self.logger.info(
                "Found existing 'Tempo Account' custom field with ID: %d",
                existing_id,
            )
            return existing_id
        except MigrationError:
            # Re-raise migration errors
            raise
        except Exception as e:
            error_msg = f"Error checking for existing custom field: {e}"
            self.logger.warning(error_msg)
            raise MigrationError(error_msg) from e

    def create_custom_field_via_rails(self) -> int:
        """Create a custom field via Rails console commands.

        Returns:
            ID of the created custom field

        Raises:
            MigrationError: If custom field creation fails

        """
        try:
            if not self.tempo_accounts:
                self.extract_tempo_accounts()

            # Get possible values from Tempo accounts
            possible_values = [
                acc.get("name") for acc in self.tempo_accounts if acc.get("name")
            ]

            # Safely construct Ruby array literal to prevent injection attacks
            # Each account name is properly escaped for Ruby string literals
            escaped_values = []
            for value in possible_values:
                if value is None:
                    continue
                # Escape single quotes and backslashes for Ruby %q{} syntax
                # This prevents arbitrary Ruby code execution via malicious account names
                escaped_value = str(value).replace("\\", "\\\\").replace("}", "\\}")
                escaped_values.append(f"%q{{{escaped_value}}}")

            # Construct Ruby array using safe literal syntax
            ruby_array_literal = "[" + ", ".join(escaped_values) + "]"

            # Create custom field command with safe parameterization
            # Uses Ruby %q{} syntax to prevent injection attacks
            create_command = f"""
            possible_values_array = {ruby_array_literal}
            cf = CustomField.new(
              name: %q{{Tempo Account}},
              field_format: %q{{list}},
              is_required: false,
              searchable: true,
              editable: true,
              type: %q{{WorkPackageCustomField}},
              possible_values: possible_values_array
            )
            cf.save!
            cf.id
            """

            result = self.op_client.execute_query(create_command, timeout=45)

            if result and "output" in result and result["output"] is not None:
                new_id = int(result["output"])
                self.logger.info(
                    "Successfully created custom field with ID: %d",
                    new_id,
                )

                # Make custom field available for all work package types
                self.associate_field_with_work_package_types(new_id)

                return new_id

            error_msg = f"Failed to create custom field via Rails. Result: {result}"
            self.logger.warning(error_msg)
            raise MigrationError(error_msg)

        except MigrationError:
            # Re-raise migration errors
            raise
        except Exception as e:
            error_msg = f"Error creating custom field via Rails: {e}"
            self.logger.warning(error_msg)
            raise MigrationError(error_msg) from e

    def associate_field_with_work_package_types(self, field_id: int) -> None:
        """Make custom field available for all work package types.

        Args:
            field_id: ID of the custom field to associate

        Raises:
            MigrationError: If association fails

        """
        try:
            self.logger.info(
                "Making custom field available for all work package types...",
            )

            # Validate field_id is a valid integer to prevent injection attacks
            try:
                validated_field_id = int(field_id)
                if validated_field_id <= 0:
                    raise ValueError("Field ID must be positive")
            except (ValueError, TypeError) as e:
                error_msg = f"Invalid field_id provided: {field_id!r} (must be positive integer)"
                raise MigrationError(error_msg) from e

            # Command to associate with all types using validated integer ID
            # No string interpolation of user input to prevent injection attacks
            activate_command = f"""
            cf = CustomField.find({validated_field_id})
            cf.is_for_all = true
            cf.save!
            Type.all.each do |type|
              type.custom_fields << cf unless type.custom_fields.include?(cf)
              type.save!
            end
            puts %q{{SUCCESS}}
            """

            result = self.op_client.execute_query(activate_command, timeout=45)

            # Handle both string and dict results
            success = False
            if isinstance(result, dict):
                if result.get("status") == "success":
                    success = True
            elif isinstance(result, str):
                # Check if the command executed successfully (no error output)
                if "SUCCESS" in result or "=> nil" in result:
                    success = True
                # If there's no error indication, assume success
                elif not any(
                    error_word in result.lower()
                    for error_word in ["error", "exception", "failed"]
                ):
                    success = True

            if success:
                self.logger.info("Custom field activated for all work package types")
            else:
                error_msg = (
                    f"Failed to activate custom field for all types. Result: {result}"
                )
                self.logger.warning(error_msg)
                raise MigrationError(error_msg)

        except MigrationError:
            # Re-raise migration errors
            raise
        except Exception as e:
            error_msg = f"Error associating custom field with types: {e}"
            self.logger.warning(error_msg)
            raise MigrationError(error_msg) from e
