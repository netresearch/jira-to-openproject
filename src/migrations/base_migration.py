import json
import os
from typing import Any, Optional

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.models import ComponentResult


class BaseMigration:
    """
    Base class for all migration classes.
    Provides common functionality and initialization for all migration types.
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
        op_rails_client: Optional["OpenProjectRailsClient"] = None,
    ) -> None:
        """
        Initialize the base migration with common attributes.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            op_rails_client: Optional Initialized OpenProject Rails client
        """
        self.jira_client = jira_client or JiraClient()
        self.op_client = op_client or OpenProjectClient()
        self.op_rails_client = op_rails_client

        self.data_dir = config.get_path("data")
        self.output_dir = config.get_path("output")

        self.logger = config.logger

        # Initialize config.mappings if not already set
        if config.mappings is None:
            from src.mappings.mappings import Mappings
            config.mappings = Mappings(data_dir=self.data_dir)

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
                with open(filepath) as f:
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
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        self.logger.debug(f"Saved data to {filepath}")
        return filepath

    def run(self) -> ComponentResult:
        """
        Default implementation of the run method that all migration classes should implement.

        Returns:
            Dictionary with migration results
        """
        self.logger.warning(
            f"The run method has not been implemented for {self.__class__.__name__}"
        )
        return ComponentResult(
            status="failed",
            error=f"The run method has not been implemented for {self.__class__.__name__}",
            success_count=0,
            failed_count=0,
            total_count=0,
        )
