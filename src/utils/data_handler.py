"""Data handler module for serialization and deserialization of data.

This module provides a consistent interface for loading and saving data,
with special handling for Pydantic models.
"""

import json
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from src import config
from src.models.migration_error import MigrationError

T = TypeVar("T", bound=BaseModel)


def save_results(
    data: Any,
    filename: Path | str,
    directory: Path | str | None = None,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> bool:
    """Save data to a JSON file, automatically handling Pydantic models.

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


def save(
    data: Any,
    filename: str | Path,
    directory: str | Path | None = None,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> bool:
    """Save data to a JSON file, automatically handling Pydantic models.

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

    # Convert to Path objects
    directory = Path(directory)
    filename = Path(filename) if not isinstance(filename, Path) else filename

    # Make sure we're just using the filename part if a full path was provided
    if len(filename.parts) > 1:
        filename = Path(filename.name)

    filepath = directory / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Convert Pydantic models to dict if necessary
        if isinstance(data, BaseModel):
            data = data.model_dump()

        with filepath.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)

        config.logger.info("Saved data to %s", filepath)
        return True
    except Exception:
        config.logger.exception("Failed to save data to %s", filepath)
        return False


def load[T](
    model_class: type[T],
    filename: str | Path,
    directory: str | Path | None = None,
) -> T:
    """Load data from a JSON file and convert to specified model type.

    Args:
        model_class: Pydantic model class to load into
        filename: File to load from
        directory: Directory to load from (default: config.get_path("data"))

    Returns:
        Instance of model_class

    Raises:
        FileNotFoundError: If the file doesn't exist
        MigrationError: If data loading or parsing fails

    """
    if directory is None:
        directory = config.get_path("data")

    # Convert to Path objects
    directory = Path(directory)
    filename = Path(filename) if not isinstance(filename, Path) else filename

    # Make sure we're just using the filename part if a full path was provided
    if len(filename.parts) > 1:
        filename = Path(filename.name)

    filepath = directory / filename

    if not filepath.exists():
        msg = f"File not found: {filepath}"
        raise FileNotFoundError(msg)

    try:
        with filepath.open("r", encoding="utf-8") as f:
            data = json.load(f)

        # Convert dict to model instance (Pydantic v2 only)
        model_cls = cast(type[BaseModel], model_class)
        result = cast(T, model_cls.model_validate(data))

        config.logger.info("Loaded data from %s", filepath)
        return result
    except Exception as e:
        msg = f"Failed to load data from {filepath}: {e}"
        raise MigrationError(msg) from e


def load_dict(
    filename: Path,
    directory: Path | None = None,
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load dictionary data from a JSON file.

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

    # Convert to Path objects
    directory = Path(directory)
    filename = Path(filename) if not isinstance(filename, Path) else filename

    # Get full path
    file_path = directory / filename

    try:
        # Optimistic execution: attempt to load directly
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            config.logger.warning("File %s does not contain a dictionary", file_path)
            return default

        config.logger.info("Loaded dictionary data from %s", file_path)
        return data
    except FileNotFoundError:
        config.logger.debug("File does not exist: %s", file_path)
        return default
    except json.JSONDecodeError:
        # Only check file size after JSON parsing fails
        if file_path.stat().st_size == 0:
            config.logger.debug("File is empty: %s", file_path)
        else:
            config.logger.exception("Error parsing JSON from %s", file_path)
        return default
    except Exception:
        config.logger.exception("Error reading JSON file %s", file_path)
        return default


def load_list(
    filename: str | Path,
    directory: str | Path | None = None,
    default: list[Any] | None = None,
) -> list[Any]:
    """Load list data from a JSON file.

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

    # Convert to Path objects
    directory = Path(directory)
    filename = Path(filename) if not isinstance(filename, Path) else filename

    # Get full path
    file_path = directory / filename

    try:
        # Optimistic execution: attempt to load directly
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            config.logger.warning("File %s does not contain a list", file_path)
            return default

        config.logger.info("Loaded list data from %s", file_path)
        return data
    except FileNotFoundError:
        config.logger.debug("File does not exist: %s", file_path)
        return default
    except json.JSONDecodeError:
        # Only check file size after JSON parsing fails
        if file_path.stat().st_size == 0:
            config.logger.debug("File is empty: %s", file_path)
        else:
            config.logger.exception("Error parsing JSON from %s", file_path)
        return default
    except Exception:
        config.logger.exception("Error reading JSON file %s", file_path)
        return default


def save_to_path(
    data: Any,
    filepath: Path | str,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> bool:
    """Save data to a JSON file at a specific path.

    Args:
        data: Data to save
        filepath: Path to save to
        indent: JSON indentation level
        ensure_ascii: Whether to escape non-ASCII characters

    Returns:
        True if save was successful, False otherwise

    """
    # Convert to Path object if it's a string
    filepath = Path(filepath) if isinstance(filepath, str) else filepath

    try:
        # Ensure parent directory exists
        filepath.parent.mkdir(parents=True, exist_ok=True)

        # Handle Pydantic models by converting to dict first
        if isinstance(data, BaseModel):
            data = data.model_dump()

        with filepath.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)

        # Verify file was created successfully
        if filepath.exists():
            config.logger.info("Saved data to %s", filepath)
            return True
        config.logger.error("Failed to save data to %s", filepath)
        return False

    except Exception as e:
        config.logger.exception("Failed to save data to %s: %s", filepath, e)
        return False


def save_dict(
    data: dict[str, Any],
    filepath: Path,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> bool:
    """Save dictionary data to a JSON file.

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


def load_model[T](
    model_class: type[T],
    filename: str | Path,
    directory: str | Path | None = None,
) -> T | None:
    """Load a Pydantic model from a JSON file.

    Args:
        model_class: Pydantic model class to instantiate
        filename: File to load from
        directory: Directory to load from (default: config.get_path("data"))

    Returns:
        Pydantic model instance or None if loading fails

    """
    if directory is None:
        directory = config.get_path("data")

    # Convert to Path objects
    directory = Path(directory)
    filename = Path(filename) if not isinstance(filename, Path) else filename

    # Get full path
    file_path = directory / filename

    try:
        if not file_path.exists():
            config.logger.debug("File does not exist: %s", file_path)
            return None

        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        config.logger.info("Loaded data from %s", file_path)
        model_cls = cast(type[BaseModel], model_class)
        return cast(T, model_cls.model_validate(data))
    except json.JSONDecodeError:
        config.logger.exception("Error parsing JSON from %s", file_path)
        return None
    except Exception:
        config.logger.exception("Error loading model from %s", file_path)
        return None
