"""Configuration package for Jira to OpenProject migration.

This package provides a modern, type-safe configuration system using Pydantic v2
and pydantic-settings with comprehensive validation and CLI tools.
"""

from .loader import ConfigLoader, get_config_loader, load_settings
from .schemas.settings import Settings

__all__ = ["ConfigLoader", "get_config_loader", "load_settings", "Settings"]
