"""Mappings module for loading and accessing persisted migration mappings."""

from pathlib import Path
from typing import Any

from src.config import logger, get_path
from src.utils import data_handler

# We might need other clients or migration classes later


class Mappings:
    """Handles loading and accessing various mapping files generated during migration.

    Also provides methods that utilize these mappings, like preparing work packages.

    """

    # Define constants as class attributes
    USER_MAPPING_FILE = Path("user_mapping.json")
    PROJECT_MAPPING_FILE = Path("project_mapping.json")
    ACCOUNT_MAPPING_FILE = Path("account_mapping.json")  # For parent project ID info
    COMPANY_MAPPING_FILE = Path("company_mapping.json")
    ISSUE_TYPE_MAPPING_FILE = Path("issue_type_mapping.json")
    ISSUE_TYPE_ID_MAPPING_FILE = Path("issue_type_id_mapping.json")
    STATUS_MAPPING_FILE = Path("status_mapping.json")
    LINK_TYPE_MAPPING_FILE = Path("link_type_mapping.json")
    CUSTOM_FIELD_MAPPING_FILE = Path("custom_field_mapping.json")
    SPRINT_MAPPING_FILE = Path("sprint_mapping.json")
    WORK_PACKAGE_MAPPING_FILE_PATTERN = Path(
        "work_package_mapping_{}.json",
    )  # Per project

    TEMPO_ACCOUNTS_FILE = Path("tempo_accounts.json")
    OP_PROJECTS_FILE = Path("openproject_projects.json")
    TEMPO_COMPANIES_FILE = Path("tempo_companies.json")

    def __init__(  # noqa: D107
        self,
        data_dir: Path | None = None,
    ) -> None:
        if data_dir is None:
            try:
                data_dir = get_path('data')
            except Exception:
                data_dir = Path('data')
        self.data_dir: Path = data_dir

        # Load all mappings using class attributes for filenames
        self.user_mapping = self._load_mapping(self.USER_MAPPING_FILE)
        self.project_mapping = self._load_mapping(self.PROJECT_MAPPING_FILE)
        self.account_mapping = self._load_mapping(self.ACCOUNT_MAPPING_FILE)
        self.company_mapping = self._load_mapping(self.COMPANY_MAPPING_FILE)
        self.issue_type_mapping = self._load_mapping(self.ISSUE_TYPE_MAPPING_FILE)
        self.status_mapping = self._load_mapping(self.STATUS_MAPPING_FILE)
        self.link_type_mapping = self._load_mapping(self.LINK_TYPE_MAPPING_FILE)
        self.custom_field_mapping = self._load_mapping(self.CUSTOM_FIELD_MAPPING_FILE)
        self.sprint_mapping = self._load_mapping(self.SPRINT_MAPPING_FILE)

        # Additional mappings that might be added during runtime
        self.issue_type_id_mapping = self._load_mapping(self.ISSUE_TYPE_ID_MAPPING_FILE)

        # Check essential mappings
        if not self.project_mapping:
            logger.notice(
                "Project mapping (%s) is missing or empty!",
                self.PROJECT_MAPPING_FILE,
            )
        if not self.issue_type_mapping:
            logger.notice(
                "Issue type mapping (%s) is missing or empty!",
                self.ISSUE_TYPE_MAPPING_FILE,
            )
        # Add checks for other critical mappings as needed

    def __setitem__(self, key: str, value: Any) -> None:  # noqa: ANN401
        """Support dictionary-style item assignment for compatibility with migration modules.

        Args:
            key: The mapping key to set (e.g., 'issue_type_id_mapping')
            value: The value to set for this mapping

        """
        if hasattr(self, key):
            setattr(self, key, value)
        else:
            logger.warning("Setting unknown mapping attribute: %s", key)
            setattr(self, key, value)

    def __getitem__(self, key: str) -> Any:  # noqa: ANN401
        """Support dictionary-style item access for compatibility with migration modules.

        Args:
            key: The mapping key to get (e.g., 'issue_type_id_mapping')

        Returns:
            The mapping value or raises KeyError if not found

        """
        if hasattr(self, key):
            return getattr(self, key)
        msg = f"Mapping '{key}' not found"
        raise KeyError(msg)

    def _load_mapping(self, filename: Path) -> dict[str, Any]:
        """Load a specific mapping file from the data directory."""
        file_path = self.data_dir / filename
        mapping = data_handler.load_dict(file_path)
        if mapping is None:
            logger.notice("Mapping file not found or invalid: %s", filename)
            return {}
        logger.notice("Loaded mapping '%s' with %d entries.", filename, len(mapping))
        return mapping

    def get_op_project_id(self, jira_project_key: str) -> int | None:
        """Get the mapped OpenProject project ID for a Jira project key."""
        entry = self.project_mapping.get(jira_project_key)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            "No OpenProject ID found in mapping for Jira project key: %s",
            jira_project_key,
        )
        return None

    def get_op_user_id(self, jira_user_id: str) -> int | None:
        """Get the mapped OpenProject user ID for a Jira user ID."""
        # User mapping keys might be jira_user_id or jira_account_id
        entry = self.user_mapping.get(jira_user_id)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        # Add fallback logic if key format varies
        logger.debug(
            "No OpenProject ID found in mapping for Jira user ID: %s",
            jira_user_id,
        )
        return None

    def get_op_type_id(self, jira_issue_type_name: str) -> int | None:
        """Get the mapped OpenProject type ID for a Jira issue type name."""
        entry = self.issue_type_mapping.get(jira_issue_type_name)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            "No OpenProject ID found in mapping for Jira issue type name: %s",
            jira_issue_type_name,
        )
        return None

    def get_op_status_id(self, jira_status_name: str) -> int | None:
        """Get the mapped OpenProject status ID for a Jira status name."""
        entry = self.status_mapping.get(jira_status_name)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            "No OpenProject ID found in mapping for Jira status name: %s",
            jira_status_name,
        )
        return None

    def has_mapping(self, mapping_name: str) -> bool:
        """Check if a mapping exists and has entries.

        Args:
            mapping_name: Name of the mapping (e.g., 'projects', 'users', etc.)

        Returns:
            True if the mapping exists and has entries, False otherwise

        """
        mapping_attr = f"{mapping_name}_mapping"
        if hasattr(self, mapping_attr):
            mapping = getattr(self, mapping_attr)
            return bool(mapping)
        return False

    def get_mapping(self, mapping_name: str) -> dict[str, Any]:
        """Get a specific mapping.

        Args:
            mapping_name: Name of the mapping (e.g., 'projects', 'users', etc.)

        Returns:
            The mapping dictionary or an empty dict if not found

        """
        mapping_attr = f"{mapping_name}_mapping"
        if hasattr(self, mapping_attr):
            return getattr(self, mapping_attr)
        logger.notice("Mapping '%s' not found", mapping_name)
        return {}

    def get_all_mappings(self) -> dict[str, Any]:
        """Get all mappings as a dictionary.

        Returns:
            Dictionary containing all mappings with their names as keys

        """
        mappings = {}
        mapping_attrs = [
            "user_mapping",
            "project_mapping",
            "account_mapping",
            "company_mapping",
            "issue_type_mapping",
            "status_mapping",
            "link_type_mapping",
            "custom_field_mapping",
            "sprint_mapping",
            "issue_type_id_mapping",
        ]

        for attr in mapping_attrs:
            if hasattr(self, attr):
                mappings[attr] = getattr(self, attr)

        return mappings

    def set_mapping(self, mapping_name: str, mapping_data: dict[str, Any]) -> None:
        """Set or update a specific mapping and save it to file.

        Args:
            mapping_name: Name of the mapping (e.g., 'projects', 'users', etc.)
            mapping_data: The mapping dictionary to save

        """
        # First update the instance variable
        mapping_attr = f"{mapping_name}_mapping"
        filename = Path(f"{mapping_name}_mapping.json")

        # Use the constant filename if available
        if hasattr(self, f"{mapping_name.upper()}_MAPPING_FILE"):
            filename = getattr(self, f"{mapping_name.upper()}_MAPPING_FILE")

        # Update the attribute
        setattr(self, mapping_attr, mapping_data)

        # Save to file
        file_path = self.data_dir / filename
        try:
            data_handler.save_dict(mapping_data, file_path)
            logger.info(
                "Saved mapping '%s' with %d entries",
                mapping_name,
                len(mapping_data),
            )
        except Exception:
            logger.exception("Error saving mapping '%s'", mapping_name)
            raise
