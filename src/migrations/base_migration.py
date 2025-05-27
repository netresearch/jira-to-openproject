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

    def _load_from_json(self, filename: Path | str, default: Any = None) -> Any:
        """Load data from a JSON file in the data directory.

        Args:
            filename: Name of the JSON file
            default: Default value to return if file doesn't exist

        Returns:
            Loaded JSON data or default value

        """
        filepath = self.data_dir / filename
        try:
            # Optimistic execution: attempt to load directly
            with filepath.open("r") as f:
                return json.load(f)
        except FileNotFoundError:
            # File doesn't exist - this is expected, return default
            self.logger.debug("File does not exist: %s", filepath)
            return default
        except json.JSONDecodeError as e:
            # Only perform diagnostics after JSON parsing fails
            if filepath.stat().st_size == 0:
                self.logger.debug("File is empty: %s", filepath)
            else:
                self.logger.exception("JSON decode error in %s: %s", filepath, e)
            return default
        except Exception as e:
            # Unexpected error - log it
            self.logger.exception("Unexpected error loading %s: %s", filepath, e)
            return default

    def _save_to_json(self, data: Any, filename: Path | str) -> Path:
        """Save data to a JSON file in the data directory.

        Args:
            data: Data to save
            filename: Name of the JSON file

        Returns:
            Path to the saved file

        """
        filepath = self.data_dir / Path(filename)
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
            errors=[
                f"The run method has not been implemented for {self.__class__.__name__}",
            ],
            success_count=0,
            failed_count=0,
            total_count=0,
        )
