import os
from typing import Dict, List, Any, Optional

from src.config import logger
from src.utils import load_json_file
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
# We might need other clients or migration classes later

class Mappings:
    """
    Handles loading and accessing various mapping files generated during migration.
    Also provides methods that utilize these mappings, like preparing work packages.
    """
    # Define constants as class attributes
    USER_MAPPING_FILE = "user_mapping.json"
    PROJECT_MAPPING_FILE = "project_mapping.json"
    ACCOUNT_MAPPING_FILE = "account_mapping.json" # For parent project ID info
    COMPANY_MAPPING_FILE = "company_mapping.json"
    ISSUE_TYPE_MAPPING_FILE = "issue_type_mapping.json"
    STATUS_MAPPING_FILE = "status_mapping.json"
    LINK_TYPE_MAPPING_FILE = "link_type_mapping.json"
    CUSTOM_FIELD_MAPPING_FILE = "custom_field_mapping.json"
    WORK_PACKAGE_MAPPING_FILE_PATTERN = "work_package_mapping_{}.json" # Per project

    def __init__(
        self,
        data_dir: str,
        jira_client: JiraClient,
        op_client: OpenProjectClient
    ):
        self.data_dir = data_dir
        self.jira_client = jira_client
        self.op_client = op_client

        # Load all mappings using class attributes for filenames
        self.user_mapping = self._load_mapping(self.USER_MAPPING_FILE)
        self.project_mapping = self._load_mapping(self.PROJECT_MAPPING_FILE)
        self.account_mapping = self._load_mapping(self.ACCOUNT_MAPPING_FILE)
        self.company_mapping = self._load_mapping(self.COMPANY_MAPPING_FILE)
        self.issue_type_mapping = self._load_mapping(self.ISSUE_TYPE_MAPPING_FILE)
        self.status_mapping = self._load_mapping(self.STATUS_MAPPING_FILE)
        self.link_type_mapping = self._load_mapping(self.LINK_TYPE_MAPPING_FILE)
        self.custom_field_mapping = self._load_mapping(self.CUSTOM_FIELD_MAPPING_FILE)

        # Check essential mappings
        if not self.project_mapping:
            logger.warning(f"Project mapping ({self.PROJECT_MAPPING_FILE}) is missing or empty!")
        if not self.issue_type_mapping:
            logger.warning(f"Issue type mapping ({self.ISSUE_TYPE_MAPPING_FILE}) is missing or empty!")
        # Add checks for other critical mappings as needed

    def _load_mapping(self, filename: str) -> Dict[str, Any]:
        """Loads a specific mapping file from the data directory."""
        file_path = os.path.join(self.data_dir, filename)
        mapping = load_json_file(file_path)
        if mapping is None:
            logger.warning(f"Mapping file not found or invalid: {filename}")
            return {}
        logger.notice(f"Loaded mapping '{filename}' with {len(mapping)} entries.")
        return mapping

    def get_op_project_id(self, jira_project_key: str) -> Optional[int]:
        """Get the mapped OpenProject project ID for a Jira project key."""
        entry = self.project_mapping.get(jira_project_key)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(f"No OpenProject ID found in mapping for Jira project key: {jira_project_key}")
        return None

    def get_op_user_id(self, jira_user_id: str) -> Optional[int]:
        """Get the mapped OpenProject user ID for a Jira user ID."""
        # User mapping keys might be jira_user_id or jira_account_id
        entry = self.user_mapping.get(jira_user_id)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        # Add fallback logic if key format varies
        logger.debug(f"No OpenProject ID found in mapping for Jira user ID: {jira_user_id}")
        return None

    def get_op_type_id(self, jira_issue_type_name: str) -> Optional[int]:
        """Get the mapped OpenProject type ID for a Jira issue type name."""
        entry = self.issue_type_mapping.get(jira_issue_type_name)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(f"No OpenProject ID found in mapping for Jira issue type name: {jira_issue_type_name}")
        return None

    def get_op_status_id(self, jira_status_name: str) -> Optional[int]:
        """Get the mapped OpenProject status ID for a Jira status name."""
        entry = self.status_mapping.get(jira_status_name)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(f"No OpenProject ID found in mapping for Jira status name: {jira_status_name}")
        return None

    # --- Methods needed by export_work_packages ---

    def extract_jira_issues(self, project_key: str, project_tracker=None) -> List[Dict[str, Any]]:
        """Extracts all issues for a given Jira project key."""
        logger.notice(f"Extracting Jira issues for project: {project_key}")
        try:
            # Use the updated Jira client method that handles pagination
            issues = self.jira_client.get_all_issues_for_project(project_key, expand_changelog=True)

            if issues is not None:
                 logger.notice(f"Retrieved {len(issues)} issues for project {project_key}")
                 # The client returns jira.Issue objects, convert them to dicts if needed by prepare_work_package
                 # For now, assume prepare_work_package can handle jira.Issue objects or we adapt it later.
                 # If dicts are strictly needed:
                 # return [issue.raw for issue in issues]
                 return issues # Return the list of jira.Issue objects
            else:
                 logger.error(f"Failed to retrieve issues for project {project_key} from Jira client.")
                 return []
        except AttributeError:
             logger.error(f"JiraClient does not have the expected 'get_all_issues_for_project' method.")
             return []
        except Exception as e:
            logger.error(f"Error extracting issues for project {project_key}: {e}", exc_info=True)
            return []

    def prepare_work_package(self, issue: Any, op_project_id: int) -> Optional[Dict[str, Any]]:
        """
        Transforms a Jira issue object (from python-jira library) into an OpenProject
        work package payload suitable for the Rails bulk import script.

        Args:
            issue: The jira.Issue object.
            op_project_id: The target OpenProject project ID.

        Returns:
            A dictionary for the work package payload or None if essential mapping fails.
        """
        # Adapt to use jira.Issue object attributes instead of dict.get()
        try:
            fields = issue.fields
            jira_key = issue.key
            jira_id = issue.id
        except AttributeError as e:
             logger.warning(f"Skipping issue due to missing essential attributes (not a jira.Issue object?): {e}")
             return None

        if not jira_key or not fields:
            logger.warning(f"Skipping issue due to missing key or fields: {jira_id}")
            return None

        # --- Essential Mappings ---
        # Type
        jira_type_name = fields.issuetype.name if hasattr(fields, 'issuetype') and fields.issuetype else None
        op_type_id = self.get_op_type_id(jira_type_name) if jira_type_name else None
        if op_type_id is None:
             logger.warning(f"Skipping issue {jira_key}: No mapped OpenProject type ID found for Jira type '{jira_type_name}'.")
             return None # Strict: skip if type not mapped

        # Status
        jira_status_name = fields.status.name if hasattr(fields, 'status') and fields.status else None
        op_status_id = self.get_op_status_id(jira_status_name) if jira_status_name else None

        # Assignee
        op_assignee_id = None
        if hasattr(fields, 'assignee') and fields.assignee:
             jira_assignee_id = getattr(fields.assignee, 'accountId', getattr(fields.assignee, 'name', None))
             if jira_assignee_id:
                 op_assignee_id = self.get_op_user_id(jira_assignee_id)

        # Reporter (Author in OpenProject)
        # op_author_id = None
        # if hasattr(fields, 'reporter') and fields.reporter:
        #     jira_reporter_id = getattr(fields.reporter, 'accountId', getattr(fields.reporter, 'name', None))
        #     if jira_reporter_id:
        #         op_author_id = self.get_op_user_id(jira_reporter_id)

        # --- Basic Fields ---
        subject = getattr(fields, 'summary', "No Subject")
        description = getattr(fields, 'description', None) or "" # Ensure it's a string

        wp_payload = {
            "jira_id": jira_id,
            "jira_key": jira_key,
            "project_id": op_project_id,
            "type_id": op_type_id,
            "subject": subject,
            "description": description,
            "status_id": op_status_id, # May be None
            "assigned_to_id": op_assignee_id, # May be None
            # "author_id": op_author_id, # If needed
            # Add other fields accessed via fields.xxx or fields.customfield_xxxxx
        }

        # TODO: Add remaining mapping logic using issue object attributes (fields.priority, fields.parent, fields.customfield_xxxxx, etc.)
        # TODO: Handle changelog/history from issue.changelog if needed

        return wp_payload

    # --- Helper methods for getting default/fallback IDs (implement if needed) ---
    # def _get_default_type_id(self, project_id: int) -> Optional[int]: ...
    # def get_default_op_user_id(self) -> Optional[int]: ...
    # def get_op_priority_id(self, jira_priority_name: str) -> Optional[int]: ...
    # def get_op_parent_wp_id(self, jira_parent_key: str) -> Optional[int]: ...


    # Add methods for specific mapping lookups if needed, e.g.:
    # def get_work_package_map_for_project(self, jira_project_key: str) -> Dict[str, Any]:
    #     filename = WORK_PACKAGE_MAPPING_FILE_PATTERN.format(jira_project_key)
    #     return self._load_mapping(filename)
