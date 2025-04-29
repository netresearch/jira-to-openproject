"""
Component result models for tracking migration operations.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ComponentResult(BaseModel):
    """Represents the result of a migration component."""

    success: bool = False
    message: str = ""
    details: Dict[str, Any] = Field(default_factory=dict)
    data: Optional[Dict[str, Any] | List[Dict[str, Any]]] = None
    errors: Optional[List[str]] = None
    warnings: Optional[List[str]] = None
    dry_run: bool = False
    total_types: int = 0
    matched_types: int = 0
    normalized_types: int = 0
    created_types: int = 0
    failed_types: int = 0
    existing_types: int = 0

    # Helper methods to make the class more usable
    def add_error(self, error: str) -> None:
        """Add an error message to the errors list."""
        if self.errors is None:
            self.errors = []
        self.errors.append(error)

    def add_warning(self, warning: str) -> None:
        """Add a warning message to the warnings list."""
        if self.warnings is None:
            self.warnings = []
        self.warnings.append(warning)

    def __setitem__(self, key: str, value: Any) -> None:
        """Support dictionary-style item assignment."""
        # Store the value in the details dictionary
        if self.details is None:
            self.details = {}
        self.details[key] = value

    def __getitem__(self, key: str) -> Any:
        """Support dictionary-style item access."""
        if self.details is None:
            raise KeyError(key)
        return self.details[key]

    def __contains__(self, key: str) -> bool:
        """Support 'in' operator."""
        return self.details is not None and key in self.details
