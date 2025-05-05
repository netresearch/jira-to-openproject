"""
Migration result models for tracking overall migration operations.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.models.component_results import ComponentResult


class MigrationResult(BaseModel):
    """Represents the overall result of a migration operation."""

    components: dict[str, ComponentResult] = Field(default_factory=dict)
    overall: dict[str, Any] = Field(default_factory=dict)

    # Default values for overall dictionary
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)

        # Set default values for overall dict if they don't exist
        self.overall.setdefault("status", "success")
        self.overall.setdefault("start_time", datetime.now().isoformat())
        self.overall.setdefault("timestamp", datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))

    def __setitem__(self, key: str, value: Any) -> None:
        """Support dictionary-style item assignment for top-level attributes."""
        if key == "components":
            self.components = value
        elif key == "overall":
            self.overall = value
        else:
            # Fallback to overall dictionary for other keys
            self.overall[key] = value

    def __getitem__(self, key: str) -> Any:
        """Support dictionary-style item access for top-level attributes."""
        if key == "components":
            return self.components
        elif key == "overall":
            return self.overall
        else:
            # Fallback to overall dictionary for other keys
            if key not in self.overall:
                raise KeyError(key)
            return self.overall[key]

    def __contains__(self, key: str) -> bool:
        """Support 'in' operator for top-level attributes."""
        return key in ["components", "overall"] or key in self.overall
