#!/usr/bin/env python3
"""FileManager.

Handles all file operations for the application, providing a centralized
interface for file creation, tracking, and cleanup.
"""

from __future__ import annotations

import datetime
import json
import random
import shutil
import string
import time
from pathlib import Path
from typing import Any, Self

from src import config

logger = config.logger


class FileRegistry:
    """Tracks files created by the application to facilitate cleanup and management."""

    def __init__(self) -> None:
        """Initialize the file registry."""
        self._files: dict[str, set[Path]] = {
            "temp": set(),
            "data": set(),
            "debug": set(),
            "script": set(),
            "output": set(),
        }

    def register(self, file_path: Path, category: str = "temp") -> None:
        """Register a file in the appropriate category.

        Args:
            file_path: Absolute path to the file
            category: File category (temp, data, debug, script, output)

        """
        if category not in self._files:
            self._files[category] = set()

        self._files[category].add(file_path)
        logger.debug("Registered %s file: %s", category, file_path)

    def unregister(self, file_path: Path, category: str | None = None) -> None:
        """Remove a file from the registry.

        Args:
            file_path: Absolute path to the file
            category: Optional category, if None searches all categories

        """
        categories = [category] if category and category in self._files else list(self._files.keys())

        for cat in categories:
            if file_path in self._files[cat]:
                self._files[cat].remove(file_path)
                logger.debug("Unregistered %s file: %s", cat, file_path)
                break

    def get_files(self, category: str | None = None) -> list[Path]:
        """Get all registered files, optionally filtered by category.

        Args:
            category: Optional category to filter by

        Returns:
            List of file paths

        """
        if category:
            return list(self._files.get(category, set()))

        all_files: list[Path] = []
        for files in self._files.values():
            all_files.extend(list(files))
        return all_files

    def cleanup(
        self,
        category: str | None = None,
        older_than: int | None = None,
    ) -> tuple[int, int]:
        """Clean up registered files.

        Args:
            category: Optional category to clean up
            older_than: Optional age in seconds, only files older than this will be
            removed

        Returns:
            Tuple of (files_attempted, files_deleted)

        """
        files_to_clean = self.get_files(category)
        deleted_count = 0

        for file_path in files_to_clean:
            try:
                # Check file age if specified
                if older_than is not None:
                    file_age = time.time() - file_path.stat().st_mtime
                    if file_age < older_than:
                        continue

                # Delete the file if it exists
                if file_path.exists():
                    if file_path.is_dir():
                        shutil.rmtree(file_path)
                    else:
                        file_path.unlink()
                    deleted_count += 1
                    logger.debug("Deleted file: %s", file_path)

                # Always unregister, even if file doesn't exist
                self.unregister(file_path)

            except Exception:
                logger.exception("Error cleaning up file %s", file_path)

        return len(files_to_clean), deleted_count


class FileManager:
    """Manages file operations for the application.

    Provides a centralized interface for file creation, reading, and cleanup.
    """

    # Singleton instance
    _instance: FileManager | None = None

    def __new__(cls, *_args: object, **_kwargs: object) -> Self:
        """Create a singleton instance of the FileManager."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, base_dir: str | None = None) -> None:
        """Initialize the file manager.

        Args:
            base_dir: Base directory for file operations

        """
        # Only initialize once (singleton pattern)
        if hasattr(self, "_initialized") and self._initialized:
            return

        # Set base directory
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            # Default to project root directory
            self.base_dir = Path(__file__).parent.parent.parent

        # Create registry
        self.registry = FileRegistry()

        # Setup directories
        self.var_dir: Path = self.base_dir / "var"
        self.debug_dir: Path = self.var_dir / "debug"
        self.data_dir: Path = self.var_dir / "data"
        self.temp_dir: Path = self.var_dir / "temp"

        # Ensure directories exist
        self._setup_directories()

        # Mark as initialized
        self._initialized = True
        logger.debug("FileManager initialized")

    def _setup_directories(self) -> None:
        """Ensure all required directories exist and are writable."""
        directories = [self.var_dir, self.debug_dir, self.data_dir, self.temp_dir]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

            # Verify directory is writable
            if not directory.exists():
                logger.error("Directory is not writable: %s", directory)
            else:
                logger.debug("Directory verified: %s", directory)

    def generate_unique_id(self) -> str:
        """Generate a unique identifier with microsecond precision.

        Returns:
            A unique ID string

        """
        timestamp = datetime.datetime.now(tz=datetime.UTC)
        microseconds = timestamp.microsecond
        timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")
        random_suffix = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=4),
        )
        return f"{timestamp_str}_{microseconds:06d}_{random_suffix}"

    def create_debug_session(self, name: str | None = None) -> Path:
        """Create a debug session directory with a unique name.

        Args:
            name: Optional name to include in the session directory name

        Returns:
            Path to the debug session directory

        """
        session_id = self.generate_unique_id()
        session_dir = f"{name}_{session_id}" if name else session_id

        debug_dir = Path(self.debug_dir) / session_dir
        debug_dir.mkdir(parents=True, exist_ok=True)

        return debug_dir

    def add_to_debug_log(self, session_dir: Path, message: str) -> None:
        """Add a message to the debug log file.

        Args:
            session_dir: Path to debug session directory
            message: Message to add to the log

        """
        debug_log_path = session_dir / "debug_log.txt"

        try:
            with debug_log_path.open("a") as debug_log:
                debug_log.write(f"{message}\n")
        except Exception:
            logger.exception("Error writing to debug log")

    def create_data_file(
        self,
        data: dict[str, Any] | list[Any] | str,
        filename: str | Path | None = None,
        session_dir: Path | str | None = None,
    ) -> Path:
        """Create a data file with the provided content.

        Args:
            data: Data to write to the file
            filename: Optional filename, generated if not provided
            session_dir: Optional debug session directory to link with this file

        Returns:
            Path to the created file

        """
        if filename is None:
            unique_id = self.generate_unique_id()
            filename = f"{unique_id}_data.json"

        # Determine the file path
        if session_dir and Path(session_dir).exists():
            session_dir_path = Path(session_dir)
            file_path = session_dir_path / filename
        else:
            file_path = Path(self.data_dir) / filename

        # Ensure the parent directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Write the content based on the type
            with file_path.open("w", encoding="utf-8") as f:
                if isinstance(data, str):
                    f.write(data)
                else:
                    # Convert to JSON for dictionaries and lists
                    json.dump(data, f, indent=2, ensure_ascii=False)

            # Register the file
            self.registry.register(file_path, "data")
            return file_path

        except Exception as e:
            logger.exception("Failed to create data file: %s", e)
            msg = f"Failed to create data file: {e}"
            raise OSError(msg) from e

    def create_script_file(
        self,
        content: str,
        filename: str | None = None,
        session_dir: str | None = None,
    ) -> Path:
        """Create a script file with the provided content.

        Args:
            content: Script content
            filename: Optional filename, generated if not provided
            session_dir: Optional debug session directory to link with this file

        Returns:
            Path to the created script file

        """
        if filename is None:
            unique_id = self.generate_unique_id()
            filename = f"{unique_id}_script.rb"

        # Determine the file path
        if session_dir and Path(session_dir).exists():
            file_path = Path(session_dir) / filename
        else:
            file_path = self.data_dir / filename

        # Ensure directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write content to file
        try:
            with file_path.open("w") as f:
                f.write(content)

            # Verify file was created
            if file_path.exists():
                file_size = file_path.stat().st_size
                logger.debug("Created script file: %s (%d bytes)", file_path, file_size)
            else:
                logger.error("Failed to create script file: %s", file_path)
                msg = f"Failed to create script file: {file_path}"
                raise OSError(msg)

            # Register the file
            self.registry.register(file_path, "script")

        except Exception:
            logger.exception("Error creating script file")
            raise

        return file_path

    def read_file(self, file_path: Path) -> str:
        """Read content from a file.

        Args:
            file_path: Path to the file to read

        Returns:
            Content of the file as a string

        """
        try:
            with file_path.open("r") as f:
                return f.read()
        except Exception:
            logger.exception("Error reading file %s", file_path)
            raise

    def read_json_file(self, file_path: Path | str) -> dict[str, Any]:
        """Read a JSON file.

        Args:
            file_path: Path to the file

        Returns:
            Dictionary containing the parsed JSON

        Raises:
            FileNotFoundError: If the file does not exist
            json.JSONDecodeError: If the file is not valid JSON
            Exception: For other errors

        """
        path = Path(file_path)

        if not path.exists():
            msg = f"File not found: {path}"
            raise FileNotFoundError(msg)

        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.exception("Failed to parse JSON: %s", e)
            raise
        except Exception as e:
            logger.exception("Failed to read file: %s", e)
            raise

    def copy_file(self, source_path: Path, dest_path: Path) -> Path:
        """Copy a file from source to destination.

        Args:
            source_path: Path to source file
            dest_path: Path to destination

        Returns:
            Path to the destination file

        """
        try:
            # Ensure destination directory exists
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Copy the file
            shutil.copy2(source_path, dest_path)

            # Verify file was copied
            if dest_path.exists():
                file_size = dest_path.stat().st_size
                logger.debug(
                    "Copied file: %s -> %s (%s bytes)",
                    source_path,
                    dest_path,
                    file_size,
                )
            else:
                logger.error("Failed to copy file: %s -> %s", source_path, dest_path)
                msg = f"Failed to copy file: {source_path} -> {dest_path}"
                raise OSError(msg)

            # Register the file
            self.registry.register(dest_path, "temp")

        except Exception:
            logger.exception("Error copying file")
            raise

        return dest_path

    def cleanup_old_files(self, days: int = 7) -> None:
        """Clean up files older than the specified number of days.

        Args:
            days: Number of days, files older than this will be removed

        """
        seconds = days * 24 * 60 * 60  # Convert days to seconds
        attempted, deleted = self.registry.cleanup(older_than=seconds)
        logger.info("Cleaned up %d old files (attempted %d)", deleted, attempted)

    def cleanup_all(self) -> None:
        """Clean up all registered files."""
        attempted, deleted = self.registry.cleanup()
        logger.info("Cleaned up %d registered files (attempted %d)", deleted, attempted)

    def register_debug_file(self) -> str:
        """Register a debug file and return a unique ID.

        This ID can be used to track debug information across operations.

        Returns:
            A unique identifier string for debug tracing

        """
        debug_id = self.generate_unique_id()
        timestamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d_%H%M%S")
        debug_dir = self.debug_dir / f"debug_{timestamp}_{debug_id}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_id

    def join(self, *paths: str | Path) -> Path:
        """Join path components into a single Path object.

        Args:
            *paths: Path components to join

        Returns:
            Combined Path object

        """
        # Start with an empty path
        result = Path()

        # Join each component using / operator
        for component in paths:
            result = result / Path(component)

        return result
