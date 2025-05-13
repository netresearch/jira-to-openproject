#!/usr/bin/env python3
"""FileManager

Handles all file operations for the application, providing a centralized
interface for file creation, tracking, and cleanup.
"""

from __future__ import annotations

import datetime
import json
import os
import random
import shutil
import string
import time
from typing import Any

from src import config

logger = config.logger


class FileRegistry:
    """Tracks files created by the application to facilitate cleanup and management.
    """

    def __init__(self) -> None:
        """Initialize the file registry."""
        self._files: dict[str, set[str]] = {
            "temp": set(),
            "data": set(),
            "debug": set(),
            "script": set(),
            "output": set(),
        }

    def register(self, file_path: str, category: str = "temp") -> None:
        """Register a file in the appropriate category.

        Args:
            file_path: Absolute path to the file
            category: File category (temp, data, debug, script, output)

        """
        if category not in self._files:
            self._files[category] = set()

        self._files[category].add(file_path)
        logger.debug(f"Registered {category} file: {file_path}")

    def unregister(self, file_path: str, category: str | None = None) -> None:
        """Remove a file from the registry.

        Args:
            file_path: Absolute path to the file
            category: Optional category, if None searches all categories

        """
        if category and category in self._files:
            categories = [category]
        else:
            categories = list(self._files.keys())

        for cat in categories:
            if file_path in self._files[cat]:
                self._files[cat].remove(file_path)
                logger.debug(f"Unregistered {cat} file: {file_path}")
                break

    def get_files(self, category: str | None = None) -> list[str]:
        """Get all registered files, optionally filtered by category.

        Args:
            category: Optional category to filter by

        Returns:
            List of file paths

        """
        if category:
            return list(self._files.get(category, set()))

        all_files = []
        for files in self._files.values():
            all_files.extend(list(files))
        return all_files

    def cleanup(self, category: str | None = None, older_than: int | None = None) -> tuple[int, int]:
        """Clean up registered files.

        Args:
            category: Optional category to clean up
            older_than: Optional age in seconds, only files older than this will be removed

        Returns:
            Tuple of (files_attempted, files_deleted)

        """
        files_to_clean = self.get_files(category)
        deleted_count = 0

        for file_path in files_to_clean:
            try:
                # Check file age if specified
                if older_than is not None:
                    file_age = time.time() - os.path.getmtime(file_path)
                    if file_age < older_than:
                        continue

                # Delete the file if it exists
                if os.path.exists(file_path):
                    if os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                    else:
                        os.remove(file_path)
                    deleted_count += 1
                    logger.debug(f"Deleted file: {file_path}")

                # Always unregister, even if file doesn't exist
                self.unregister(file_path)

            except Exception as e:
                logger.error(f"Error cleaning up file {file_path}: {e!s}")

        return len(files_to_clean), deleted_count


class FileManager:
    """Manages file operations for the application, providing a centralized
    interface for file creation, reading, and cleanup.
    """

    # Singleton instance
    _instance: FileManager | None = None

    def __new__(cls, *args: Any, **kwargs: Any) -> FileManager:
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
            self.base_dir = base_dir
        else:
            # Default to project root directory
            self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

        # Create registry
        self.registry = FileRegistry()

        # Setup directories
        self.var_dir = os.path.join(self.base_dir, "var")
        self.debug_dir = os.path.join(self.var_dir, "debug")
        self.data_dir = os.path.join(self.var_dir, "data")
        self.temp_dir = os.path.join(self.var_dir, "temp")

        # Ensure directories exist
        self._setup_directories()

        # Mark as initialized
        self._initialized = True
        logger.debug("FileManager initialized")

    def _setup_directories(self) -> None:
        """Ensure all required directories exist and are writable.
        """
        directories = [self.var_dir, self.debug_dir, self.data_dir, self.temp_dir]

        for directory in directories:
            os.makedirs(directory, exist_ok=True)

            # Verify directory is writable
            if not os.access(directory, os.W_OK):
                logger.error(f"Directory is not writable: {directory}")
            else:
                logger.debug(f"Directory verified: {directory}")

    def generate_unique_id(self) -> str:
        """Generate a unique identifier with microsecond precision.

        Returns:
            A unique ID string

        """
        # Format: timestamp_microseconds_randomchars
        timestamp = datetime.datetime.now()
        microseconds = timestamp.microsecond
        timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")
        random_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        return f"{timestamp_str}_{microseconds:06d}_{random_suffix}"

    def create_debug_session(self, session_id: str | None = None) -> str:
        """Create a debug session directory for related files.

        Args:
            session_id: Optional session ID, generated if not provided

        Returns:
            Path to debug session directory

        """
        if session_id is None:
            session_id = self.generate_unique_id()

        session_dir = os.path.join(self.debug_dir, f"debug_{session_id}")
        os.makedirs(session_dir, exist_ok=True)

        # Register the directory
        self.registry.register(session_dir, "debug")

        # Create a debug log file
        debug_log_path = os.path.join(session_dir, "debug_log.txt")
        with open(debug_log_path, "w") as debug_log:
            debug_log.write(f"=== Debug Session {datetime.datetime.now()} ===\n")
            debug_log.write(f"Session ID: {session_id}\n\n")

        return session_dir

    def add_to_debug_log(self, session_dir: str, message: str) -> None:
        """Add a message to the debug log file.

        Args:
            session_dir: Path to debug session directory
            message: Message to add to the log

        """
        debug_log_path = os.path.join(session_dir, "debug_log.txt")

        try:
            with open(debug_log_path, "a") as debug_log:
                debug_log.write(f"{message}\n")
        except Exception as e:
            logger.error(f"Error writing to debug log: {e!s}")

    def create_data_file(
        self,
        data: Any,
        filename: str | None = None,
        session_dir: str | None = None,
    ) -> str:
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
        if session_dir and os.path.exists(session_dir):
            file_path = os.path.join(session_dir, filename)
        else:
            file_path = os.path.join(self.data_dir, filename)

        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Write data to file
        try:
            with open(file_path, "w") as f:
                if isinstance(data, dict | list):
                    json.dump(data, f, ensure_ascii=False, indent=2)
                else:
                    f.write(str(data))

            # Verify file was created
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                logger.debug(f"Created file: {file_path} ({file_size} bytes)")
            else:
                logger.error(f"Failed to create file: {file_path}")
                raise OSError(f"Failed to create file: {file_path}")

            # Register the file
            self.registry.register(file_path, "data")

            return file_path

        except Exception as e:
            logger.error(f"Error creating data file: {e!s}")
            raise

    def create_script_file(
        self,
        content: str,
        filename: str | None = None,
        session_dir: str | None = None,
    ) -> str:
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
        if session_dir and os.path.exists(session_dir):
            file_path = os.path.join(session_dir, filename)
        else:
            file_path = os.path.join(self.data_dir, filename)

        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Write content to file
        try:
            with open(file_path, "w") as f:
                f.write(content)

            # Verify file was created
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                logger.debug(f"Created script file: {file_path} ({file_size} bytes)")
            else:
                logger.error(f"Failed to create script file: {file_path}")
                raise OSError(f"Failed to create script file: {file_path}")

            # Register the file
            self.registry.register(file_path, "script")

            return file_path

        except Exception as e:
            logger.error(f"Error creating script file: {e!s}")
            raise

    def read_file(self, file_path: str) -> str:
        """Read content from a file.

        Args:
            file_path: Path to the file to read

        Returns:
            Content of the file as a string

        """
        try:
            with open(file_path) as f:
                content = f.read()
            return content
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e!s}")
            raise

    def read_json_file(self, file_path: str) -> Any:
        """Read and parse a JSON file.

        Args:
            file_path: Path to the JSON file

        Returns:
            Parsed JSON content

        """
        try:
            with open(file_path) as f:
                data = json.load(f)
            return data
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing JSON from {file_path}: {e!s}")
            raise
        except Exception as e:
            logger.error(f"Error reading JSON file {file_path}: {e!s}")
            raise

    def copy_file(self, source_path: str, dest_path: str) -> str:
        """Copy a file from source to destination.

        Args:
            source_path: Path to source file
            dest_path: Path to destination

        Returns:
            Path to the destination file

        """
        try:
            # Ensure destination directory exists
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)

            # Copy the file
            shutil.copy2(source_path, dest_path)

            # Verify file was copied
            if os.path.exists(dest_path):
                file_size = os.path.getsize(dest_path)
                logger.debug(f"Copied file: {source_path} -> {dest_path} ({file_size} bytes)")
            else:
                logger.error(f"Failed to copy file: {source_path} -> {dest_path}")
                raise OSError(f"Failed to copy file: {source_path} -> {dest_path}")

            # Register the file
            self.registry.register(dest_path, "temp")

            return dest_path

        except Exception as e:
            logger.error(f"Error copying file: {e!s}")
            raise

    def cleanup_old_files(self, days: int = 7) -> None:
        """Clean up files older than the specified number of days.

        Args:
            days: Number of days, files older than this will be removed

        """
        seconds = days * 24 * 60 * 60  # Convert days to seconds
        attempted, deleted = self.registry.cleanup(older_than=seconds)
        logger.info(f"Cleaned up {deleted} old files (attempted {attempted})")

    def cleanup_all(self) -> None:
        """Clean up all registered files.
        """
        attempted, deleted = self.registry.cleanup()
        logger.info(f"Cleaned up {deleted} registered files (attempted {attempted})")

    def register_debug_file(self) -> str:
        """Register a debug file and return a unique ID.
        This ID can be used to track debug information across operations.

        Returns:
            A unique identifier string for debug tracing

        """
        debug_id = self.generate_unique_id()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_dir = os.path.join(self.debug_dir, f"debug_{timestamp}_{debug_id}")
        os.makedirs(debug_dir, exist_ok=True)
        return debug_id
