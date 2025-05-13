"""
Data handler module for serialization and deserialization of data.

This module provides a consistent interface for loading and saving data,
with special handling for Pydantic models.
"""

import json
import os
from typing import Any, TypeVar

from src import config

T = TypeVar("T")


def save_results(
    data: Any, filename: str, directory: str | None = None, indent: int = 2, ensure_ascii: bool = False
) -> bool:
    """
    Save data to a JSON file, automatically handling Pydantic models.

    Args:
        data: The data to save (Pydantic model or any JSON-serializable data)
        filename: Name of the file to save
        directory: Directory to save to (default: config.get_path("results"))
        indent: JSON indentation level
        ensure_ascii: Whether to escape non-ASCII characters

    Returns:
        True if save was successful, False otherwise
    """
    if directory is None:
        directory = config.get_path("results")

    return save(data, filename, directory, indent, ensure_ascii)


def save(data: Any, filename: str, directory: str | None = None, indent: int = 2, ensure_ascii: bool = False) -> bool:
    """
    Save data to a JSON file, automatically handling Pydantic models.

    Args:
        data: The data to save (Pydantic model or any JSON-serializable data)
        filename: Name of the file to save
        directory: Directory to save to (default: config.get_path("data"))
        indent: JSON indentation level
        ensure_ascii: Whether to escape non-ASCII characters

    Returns:
        True if save was successful, False otherwise
    """
    if directory is None:
        directory = config.get_path("data")

    filepath = os.path.join(directory, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    try:
        # Convert Pydantic models to dict if necessary
        if hasattr(data, "model_dump"):
            data = data.model_dump()

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)

        config.logger.info(f"Saved data to {filepath}")
        return True
    except Exception as e:
        config.logger.exception(f"Failed to save data to {filepath}: {e}")
        return False


def load(model_class: type[T], filename: str, directory: str | None = None, default: Any | None = None) -> T | None:
    """
    Load data from a JSON file and convert to specified model type.

    Args:
        model_class: Pydantic model class to load into
        filename: File to load from
        directory: Directory to load from (default: config.get_path("data"))
        default: Default value if file doesn't exist or load fails

    Returns:
        Instance of model_class or default value if loading fails
    """
    if directory is None:
        directory = config.get_path("data")

    filepath = os.path.join(directory, filename)

    if not os.path.exists(filepath):
        config.logger.info(f"File not found: {filepath}, returning default")
        return default

    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        # Convert dict to model instance
        result = model_class.model_validate(data)
        config.logger.info(f"Loaded data from {filepath}")
        return result
    except Exception as e:
        config.logger.exception(f"Failed to load data from {filepath}: {e}")
        return default


def load_dict(filename: str, directory: str | None = None, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Load dictionary data from a JSON file.

    Args:
        filename: File to load from
        directory: Directory to load from (default: config.get_path("data"))
        default: Default value if file doesn't exist or load fails

    Returns:
        Dictionary loaded from JSON or default value if loading fails
    """
    if default is None:
        default = {}

    if directory is None:
        directory = config.get_path("data")

    filepath = os.path.join(directory, filename)

    if not os.path.exists(filepath):
        config.logger.info(f"File not found: {filepath}, returning empty dict")
        return default

    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        config.logger.info(f"Loaded dictionary data from {filepath}")
        return data
    except Exception as e:
        config.logger.exception(f"Failed to load data from {filepath}: {e}")
        return default


def load_list(filename: str, directory: str | None = None, default: list[Any] | None = None) -> list[Any]:
    """
    Load list data from a JSON file.

    Args:
        filename: File to load from
        directory: Directory to load from (default: config.get_path("data"))
        default: Default value if file doesn't exist or load fails

    Returns:
        List loaded from JSON or default value if loading fails
    """
    if default is None:
        default = []

    if directory is None:
        directory = config.get_path("data")

    filepath = os.path.join(directory, filename)

    if not os.path.exists(filepath):
        config.logger.info(f"File not found: {filepath}, returning empty list")
        return default

    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        config.logger.info(f"Loaded list data from {filepath}")
        return data
    except Exception as e:
        config.logger.exception(f"Failed to load data from {filepath}: {e}")
        return default


def save_to_path(data: Any, filepath: str, indent: int = 2, ensure_ascii: bool = False) -> bool:
    """
    Save data to a JSON file at a specific path.

    Args:
        data: The data to save (Pydantic model or any JSON-serializable data)
        filepath: Full file path to save to
        indent: JSON indentation level
        ensure_ascii: Whether to escape non-ASCII characters

    Returns:
        True if save was successful, False otherwise
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    try:
        # Convert Pydantic models to dict if necessary
        if hasattr(data, "model_dump"):
            data = data.model_dump()

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)

        config.logger.info(f"Saved data to {filepath}")
        return True
    except Exception as e:
        config.logger.exception(f"Failed to save data to {filepath}: {e}")
        return False


def load_from_path(model_class: type[T], filepath: str, default: Any | None = None) -> T | None:
    """
    Load data from a JSON file at a specific path.

    Args:
        model_class: Pydantic model class to load into
        filepath: Full file path to load from
        default: Default value if file doesn't exist or load fails

    Returns:
        Instance of model_class or default value if loading fails
    """
    if not os.path.exists(filepath):
        config.logger.info(f"File not found: {filepath}, returning default")
        return default

    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        # Convert dict to model instance
        result = model_class.model_validate(data)
        config.logger.info(f"Loaded data from {filepath}")
        return result
    except Exception as e:
        config.logger.exception(f"Failed to load data from {filepath}: {e}")
        return default


def save_dict(data: dict[str, Any], filepath: str, indent: int = 2, ensure_ascii: bool = False) -> bool:
    """
    Save dictionary data to a JSON file.

    This is a convenience wrapper around save_to_path for dictionaries.

    Args:
        data: Dictionary to save
        filepath: Full file path to save to
        indent: JSON indentation level
        ensure_ascii: Whether to escape non-ASCII characters

    Returns:
        True if save was successful, False otherwise
    """
    return save_to_path(data, filepath, indent, ensure_ascii)
