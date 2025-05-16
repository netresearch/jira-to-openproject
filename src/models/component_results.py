"""Component result models for tracking migration operations."""

from typing import Any

from pydantic import BaseModel, Field


class ComponentResult(BaseModel):
    """Represents the result of a migration component."""

    success: bool = False
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] | list[dict[str, Any]] | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = False
    total_types: int = 0
    matched_types: int = 0
    normalized_types: int = 0
    created_types: int = 0
    failed_types: int = 0
    existing_types: int = 0
    total_issues: int = 0
    matched_issues: int = 0
    normalized_issues: int = 0
    created_issues: int = 0
    failed_issues: int = 0
    existing_issues: int = 0
    success_count: int = 0
    failed_count: int = 0
    total_count: int = 0
    updated: int = 0
    failed: int = 0
    analysis: dict[str, Any] = Field(default_factory=dict)
    jira_fields_count: int = 0
    op_fields_count: int = 0
    mapped_fields_count: int = 0
    error: str | None = None

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
