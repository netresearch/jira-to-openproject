#!/usr/bin/env python3
"""Test module for FileManager.

This module contains test cases for FileManager to validate file operations and tracking.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.utils.file_manager import FileManager, FileRegistry


class TestFileRegistry(unittest.TestCase):
    """Test cases for the FileRegistry class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        self.registry = FileRegistry()

    def test_register_unregister(self) -> None:
        """Test registering and unregistering files."""
        # Register a file
        test_file = Path("/tmp/test_file.txt")
        self.registry.register(test_file, "temp")

        # Verify it's registered
        assert test_file in self.registry.get_files("temp")

        # Unregister the file
        self.registry.unregister(test_file)

        # Verify it's no longer registered
        assert test_file not in self.registry.get_files("temp")

    def test_register_custom_category(self) -> None:
        """Test registering files with custom categories."""
        # Register a file with a custom category
        test_file = Path("/tmp/test_file.txt")
        self.registry.register(test_file, "custom")

        # Verify it's registered in the custom category
        assert test_file in self.registry.get_files("custom")

        # Verify it's also in the full list
        assert test_file in self.registry.get_files()

    def test_get_files(self) -> None:
        """Test retrieving files from the registry."""
        # Register files in different categories
        temp_file = Path("/tmp/temp_file.txt")
        data_file = Path("/tmp/data_file.txt")

        self.registry.register(temp_file, "temp")
        self.registry.register(data_file, "data")

        # Get all files
        all_files = self.registry.get_files()
        assert temp_file in all_files
        assert data_file in all_files

        # Get only temp files
        temp_files = self.registry.get_files("temp")
        assert temp_file in temp_files
        assert data_file not in temp_files

        # Get only data files
        data_files = self.registry.get_files("data")
        assert data_file in data_files
        assert temp_file not in data_files

    @patch("os.path.getmtime")
    def test_cleanup(self, mock_getmtime: MagicMock) -> None:
        """Test cleaning up registered files."""
        # Setup mocks
        mock_getmtime.return_value = 0  # Files will be "old"

        # Register files
        test_file1 = Path("/tmp/test_file1.txt")
        test_file2 = Path("/tmp/test_file2.txt")

        self.registry.register(test_file1, "temp")
        self.registry.register(test_file2, "data")

        # Create proper mocks for Path operations
        with patch.object(Path, "exists", return_value=True) as mock_exists:
            with patch.object(Path, "unlink") as mock_unlink:
                with patch.object(Path, "is_dir", return_value=False) as mock_is_dir:
                    # Clean up only temp files
                    attempted, deleted = self.registry.cleanup("temp")
                    assert attempted == 1
                    assert deleted == 1
                    mock_unlink.assert_called_once()
                    mock_exists.assert_called()  # Verify exists was called
                    mock_is_dir.assert_called()  # Verify is_dir was called

                    # Reset mocks
                    mock_unlink.reset_mock()
                    mock_exists.reset_mock()
                    mock_is_dir.reset_mock()

                    # Clean up all remaining files
                    attempted, deleted = self.registry.cleanup()
                    assert attempted == 1  # Only 1 file left
                    assert deleted == 1
                    mock_unlink.assert_called_once()
                    mock_exists.assert_called()  # Verify exists was called
                    mock_is_dir.assert_called()  # Verify is_dir was called


class TestFileManager(unittest.TestCase):
    """Test cases for the FileManager class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Use a temporary directory for testing
        self.temp_dir = tempfile.mkdtemp()

        # Create a patcher for the config.logger
        self.logger_patcher = patch("src.utils.file_manager.logger")
        self.mock_logger = self.logger_patcher.start()

        # Initialize FileManager with the temp directory
        self.file_manager = FileManager(self.temp_dir)

        # Reset the singleton for each test
        FileManager._instance = None

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop the logger patcher
        self.logger_patcher.stop()

        # Clean up the temporary directory
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_singleton_pattern(self) -> None:
        """Test that FileManager is a singleton."""
        # Create two instances
        fm1 = FileManager(self.temp_dir)
        fm2 = FileManager(self.temp_dir)

        # Verify they are the same instance
        assert fm1 is fm2

    def test_directory_setup(self) -> None:
        """Test that directories are set up correctly."""
        # Check that directories were created
        assert Path(self.file_manager.var_dir).exists()
        assert Path(self.file_manager.debug_dir).exists()
        assert Path(self.file_manager.data_dir).exists()
        assert Path(self.file_manager.temp_dir).exists()

    def test_generate_unique_id(self) -> None:
        """Test generating unique IDs."""
        # Generate two IDs and verify they're different
        id1 = self.file_manager.generate_unique_id()
        id2 = self.file_manager.generate_unique_id()

        assert id1 != id2

        # Verify the format (should have date and time components)
        assert re.search(r"\d{8}_\d{6}_\d{6}_[a-z0-9]{4}", id1)

    def test_create_debug_session(self) -> None:
        """Test creating debug sessions."""
        # Create a MagicMock for the Path object
        path_mock = MagicMock(spec=Path)
        path_mock.exists.return_value = True

        # Patch Path to return our mock
        with patch("src.utils.file_manager.Path", return_value=path_mock) as MockPath:
            # Make the / operator return our mock path
            path_mock.__truediv__.return_value = path_mock

            # Set up other mock attributes
            path_mock.mkdir.return_value = None
            path_mock.open.return_value.__enter__.return_value = MagicMock()

            # Create a debug session
            session_dir = self.file_manager.create_debug_session()

            # Verify it was created (in our mock world)
            assert session_dir is path_mock

            # Verify mkdir was called to create the directory
            path_mock.mkdir.assert_called_once()

            # Verify Path was called with the proper path components
            MockPath.assert_called()

    def test_add_to_debug_log(self) -> None:
        """Test adding messages to debug logs."""
        # Create a debug session
        session_dir = self.file_manager.create_debug_session()

        # Add a message
        test_message = "Test debug message"
        self.file_manager.add_to_debug_log(session_dir, test_message)

        # Verify the message was added
        log_file = Path(session_dir) / "debug_log.txt"
        with log_file.open("r") as f:
            content = f.read()

        assert test_message in content

    def test_create_data_file(self) -> None:
        """Test creating data files."""
        # Create a data file with dictionary data
        test_data = {"key": "value", "number": 42}
        file_path = self.file_manager.create_data_file(test_data)

        # Verify it was created
        assert Path(file_path).exists()

        # Verify it's registered
        assert file_path in self.file_manager.registry.get_files("data")

        # Verify the content
        with file_path.open("r") as f:
            content = json.load(f)

        assert content == test_data

    def test_create_script_file(self) -> None:
        """Test creating script files."""
        # Create a script file
        script_content = "puts 'Hello, World!'"
        file_path = self.file_manager.create_script_file(script_content)

        # Verify it was created
        assert Path(file_path).exists()

        # Verify it's registered
        assert file_path in self.file_manager.registry.get_files("script")

        # Verify the content
        with file_path.open("r") as f:
            content = f.read()

        assert content == script_content

    def test_read_file(self) -> None:
        """Test reading files."""
        # Create a file with content
        test_content = "Test file content"
        test_file = Path(self.file_manager.temp_dir) / "test_file.txt"

        with test_file.open("w") as f:
            f.write(test_content)

        # Read the file
        content = self.file_manager.read_file(test_file)

        # Verify the content
        assert content == test_content

    def test_read_json_file(self) -> None:
        """Test reading JSON files."""
        # Create a JSON file with content
        test_data = {"key": "value", "number": 42}
        test_file = Path(self.file_manager.temp_dir) / "test_file.json"

        with test_file.open("w") as f:
            json.dump(test_data, f)

        # Read the JSON file
        data = self.file_manager.read_json_file(test_file)

        # Verify the content
        assert data == test_data

    def test_copy_file(self) -> None:
        """Test copying files."""
        # Create a source file
        test_content = "Test file content"
        source_file = Path(self.file_manager.temp_dir) / "source.txt"

        with source_file.open("w") as f:
            f.write(test_content)

        # Destination file
        dest_file = Path(self.file_manager.temp_dir) / "destination.txt"

        # Copy the file
        result = self.file_manager.copy_file(source_file, dest_file)

        # Verify the file was copied
        assert Path(dest_file).exists()
        assert result == dest_file

        # Verify the content
        with dest_file.open("r") as f:
            content = f.read()

        assert content == test_content

        # Verify it's registered
        assert dest_file in self.file_manager.registry.get_files("temp")

    @patch.object(FileRegistry, "cleanup")
    def test_cleanup_old_files(self, mock_cleanup: MagicMock) -> None:
        """Test cleaning up old files."""
        # Set up mock
        mock_cleanup.return_value = (10, 5)

        # Call cleanup_old_files
        self.file_manager.cleanup_old_files(days=7)

        # Verify cleanup was called with the right parameters
        mock_cleanup.assert_called_once()
        # First positional argument should be None, second should be seconds (7 days)
        assert mock_cleanup.call_args[1]["older_than"] == 7 * 24 * 60 * 60

        # Verify logger was called
        self.mock_logger.info.assert_called_once()

    @patch.object(FileRegistry, "cleanup")
    def test_cleanup_all(self, mock_cleanup: MagicMock) -> None:
        """Test cleaning up all files."""
        # Set up mock
        mock_cleanup.return_value = (10, 10)

        # Call cleanup_all
        self.file_manager.cleanup_all()

        # Verify cleanup was called with no parameters
        mock_cleanup.assert_called_once_with()

        # Verify logger was called
        self.mock_logger.info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
