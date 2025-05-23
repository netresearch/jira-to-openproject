import json
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.models import ComponentResult


class BaseMigration:
    """Base class for all migration classes.

    Provides common functionality and initialization for all migration types.

    Follows the layered client architecture:
    1. OpenProjectClient - Manages all lower-level clients and operations
    2. BaseMigration - Uses OpenProjectClient for migrations
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
    ) -> None:
        """Initialize the base migration with common attributes.

        Follows dependency injection pattern for the high-level clients only.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client

        """
        # Initialize clients using dependency injection
        self.jira_client = jira_client or JiraClient()
        self.op_client = op_client or OpenProjectClient()

        self.data_dir: Path = config.get_path("data")
        self.output_dir: Path = config.get_path("output")

        self.logger = config.logger

        # Initialize config.mappings if not already set
        if config.mappings is None:
            from src.mappings.mappings import Mappings

            config.mappings = Mappings(data_dir=self.data_dir)

    def _load_from_json(self, filename: Path, default: Any = None) -> Any:
        """Load data from a JSON file in the data directory.

        Args:
            filename: Name of the JSON file
            default: Default value to return if file doesn't exist

        Returns:
            Loaded JSON data or default value

        """
        filepath = self.data_dir / filename
        if filepath.exists():
            try:
                with filepath.open("r") as f:
                    return json.load(f)
            except Exception:
                self.logger.exception("Failed to load %s", filepath)
                return default
        return default

    def _save_to_json(self, data: Any, filename: Path) -> Path:
        """Save data to a JSON file in the data directory.

        Args:
            data: Data to save
            filename: Name of the JSON file

        Returns:
            Path to the saved file

        """
        filepath = self.data_dir / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)

        with filepath.open("w") as f:
            json.dump(data, f, indent=2)

        self.logger.debug("Saved data to %s", filepath)
        return filepath

    def run(self) -> ComponentResult:
        """Implement the default run method that all migration classes should implement.

        Returns:
            Dictionary with migration results

        """
        self.logger.warning(
            "The run method has not been implemented for %s",
            self.__class__.__name__,
        )
        return ComponentResult(
            success=False,
            errors=[f"The run method has not been implemented for {self.__class__.__name__}"],
            success_count=0,
            failed_count=0,
            total_count=0,
        )
