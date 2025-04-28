import os
from typing import Any

from src.config import logger
from src.utils import data_handler

# We might need other clients or migration classes later


class Mappings:
    """
    Handles loading and accessing various mapping files generated during migration.
    Also provides methods that utilize these mappings, like preparing work packages.
    """

    # Define constants as class attributes
    USER_MAPPING_FILE = "user_mapping.json"
    PROJECT_MAPPING_FILE = "project_mapping.json"
    ACCOUNT_MAPPING_FILE = "account_mapping.json"  # For parent project ID info
    COMPANY_MAPPING_FILE = "company_mapping.json"
    ISSUE_TYPE_MAPPING_FILE = "issue_type_mapping.json"
    STATUS_MAPPING_FILE = "status_mapping.json"
    LINK_TYPE_MAPPING_FILE = "link_type_mapping.json"
    CUSTOM_FIELD_MAPPING_FILE = "custom_field_mapping.json"
    WORK_PACKAGE_MAPPING_FILE_PATTERN = "work_package_mapping_{}.json"  # Per project

    TEMPO_ACCOUNTS_FILE = "tempo_accounts.json"
    OP_PROJECTS_FILE = "openproject_projects.json"
    TEMPO_COMPANIES_FILE = "tempo_companies.json"

    def __init__(
        self,
        data_dir: str,
    ):
        self.data_dir = data_dir

        # Load all mappings using class attributes for filenames
        self.user_mapping = self._load_mapping(self.USER_MAPPING_FILE)
        self.project_mapping = self._load_mapping(self.PROJECT_MAPPING_FILE)
        self.account_mapping = self._load_mapping(self.ACCOUNT_MAPPING_FILE)
        self.company_mapping = self._load_mapping(self.COMPANY_MAPPING_FILE)
        self.issue_type_mapping = self._load_mapping(self.ISSUE_TYPE_MAPPING_FILE)
        self.status_mapping = self._load_mapping(self.STATUS_MAPPING_FILE)
        self.link_type_mapping = self._load_mapping(self.LINK_TYPE_MAPPING_FILE)
        self.custom_field_mapping = self._load_mapping(self.CUSTOM_FIELD_MAPPING_FILE)

        # Additional mappings that might be added during runtime
        self.issue_type_id_mapping = {}

        # Check essential mappings
        if not self.project_mapping:
            logger.warning(
                f"Project mapping ({self.PROJECT_MAPPING_FILE}) is missing or empty!"
            )
        if not self.issue_type_mapping:
            logger.warning(
                f"Issue type mapping ({self.ISSUE_TYPE_MAPPING_FILE}) is missing or empty!"
            )
        # Add checks for other critical mappings as needed

    def __setitem__(self, key: str, value: Any) -> None:
        """
        Support dictionary-style item assignment for compatibility with migration modules.

        Args:
            key: The mapping key to set (e.g., 'issue_type_id_mapping')
            value: The value to set for this mapping
        """
        if hasattr(self, key):
            setattr(self, key, value)
        else:
            logger.warning(f"Setting unknown mapping attribute: {key}")
            setattr(self, key, value)

    def __getitem__(self, key: str) -> Any:
        """
        Support dictionary-style item access for compatibility with migration modules.

        Args:
            key: The mapping key to get (e.g., 'issue_type_id_mapping')

        Returns:
            The mapping value or raises KeyError if not found
        """
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(f"Mapping '{key}' not found")

    def _load_mapping(self, filename: str) -> dict[str, Any]:
        """Loads a specific mapping file from the data directory."""
        file_path = os.path.join(self.data_dir, filename)
        mapping = data_handler.load_dict(file_path)
        if mapping is None:
            logger.warning(f"Mapping file not found or invalid: {filename}")
            return {}
        logger.notice(
            f"Loaded mapping '{filename}' with {len(mapping)} entries."
        )
        return mapping

    def get_op_project_id(self, jira_project_key: str) -> int | None:
        """Get the mapped OpenProject project ID for a Jira project key."""
        entry = self.project_mapping.get(jira_project_key)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            f"No OpenProject ID found in mapping for Jira project key: {jira_project_key}"
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
            f"No OpenProject ID found in mapping for Jira user ID: {jira_user_id}"
        )
        return None

    def get_op_type_id(self, jira_issue_type_name: str) -> int | None:
        """Get the mapped OpenProject type ID for a Jira issue type name."""
        entry = self.issue_type_mapping.get(jira_issue_type_name)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            f"No OpenProject ID found in mapping for Jira issue type name: {jira_issue_type_name}"
        )
        return None

    def get_op_status_id(self, jira_status_name: str) -> int | None:
        """Get the mapped OpenProject status ID for a Jira status name."""
        entry = self.status_mapping.get(jira_status_name)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            f"No OpenProject ID found in mapping for Jira status name: {jira_status_name}"
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
        logger.warning(f"Mapping '{mapping_name}' not found")
        return {}