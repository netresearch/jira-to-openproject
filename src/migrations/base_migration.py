import os
import json
from typing import Dict, List, Any, Optional

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src import config


class BaseMigration:
    """
    Base class for all migration classes.
    Provides common functionality and initialization for all migration types.
    """

    def __init__(
        self,
        jira_client: Optional[JiraClient] = None,
        op_client: Optional[OpenProjectClient] = None,
        data_dir: str | None = None,
        dry_run: bool = True,
        force: bool = False,
    ) -> None:
        """
        Initialize the base migration with common attributes.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            data_dir: Directory for storing migration data
            dry_run: If True, simulate migration without making changes
            force: If True, force re-extraction of data
        """
        # Initialize clients
        self.jira_client = jira_client or JiraClient()
        self.op_client = op_client or OpenProjectClient()

        # Flag options
        self.dry_run = dry_run
        self.force = force

        # Directories
        self.data_dir = data_dir or config.get_path("data")
        self.output_dir = config.get_path("output")

        # Logger
        self.logger = config.logger

    def _load_from_json(self, filename: str, default: Any = None) -> Any:
        """
        Load data from a JSON file in the data directory.

        Args:
            filename: Name of the JSON file
            default: Default value to return if file doesn't exist

        Returns:
            Loaded JSON data or default value
        """
        filepath = os.path.join(self.data_dir, filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    return json.load(f)
            except Exception as e:
                self.logger.warning(f"Failed to load {filepath}: {e}")
                return default
        return default

    def _save_to_json(self, data: Any, filename: str) -> str:
        """
        Save data to a JSON file in the data directory.

        Args:
            data: Data to save
            filename: Name of the JSON file

        Returns:
            Path to the saved file
        """
        filepath = os.path.join(self.data_dir, filename)

        # Create parent directories if they don't exist
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        self.logger.debug(f"Saved data to {filepath}")
        return filepath
