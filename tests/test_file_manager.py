#!/usr/bin/env python3
"""
Test module for FileManager.

This module contains test cases for FileManager to validate file operations and tracking.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from src.utils.file_manager import FileManager, FileRegistry


class TestFileRegistry(unittest.TestCase):
    """Test cases for the FileRegistry class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        self.registry = FileRegistry()

    def test_register_unregister(self) -> None:
        """Test registering and unregistering files."""
        # Register a file
        test_file = "/tmp/test_file.txt"
        self.registry.register(test_file, "temp")

        # Verify it's registered
        self.assertIn(test_file, self.registry.get_files("temp"))

        # Unregister the file
        self.registry.unregister(test_file)

        # Verify it's no longer registered
        self.assertNotIn(test_file, self.registry.get_files("temp"))

    def test_register_custom_category(self) -> None:
        """Test registering files with custom categories."""
        # Register a file with a custom category
        test_file = "/tmp/test_file.txt"
        self.registry.register(test_file, "custom")

        # Verify it's registered in the custom category
        self.assertIn(test_file, self.registry.get_files("custom"))

        # Verify it's also in the full list
        self.assertIn(test_file, self.registry.get_files())

    def test_get_files(self) -> None:
        """Test retrieving files from the registry."""
        # Register files in different categories
        temp_file = "/tmp/temp_file.txt"
        data_file = "/tmp/data_file.txt"

        self.registry.register(temp_file, "temp")
        self.registry.register(data_file, "data")

        # Get all files
        all_files = self.registry.get_files()
        self.assertIn(temp_file, all_files)
        self.assertIn(data_file, all_files)

        # Get only temp files
        temp_files = self.registry.get_files("temp")
        self.assertIn(temp_file, temp_files)
        self.assertNotIn(data_file, temp_files)

        # Get only data files
        data_files = self.registry.get_files("data")
        self.assertIn(data_file, data_files)
        self.assertNotIn(temp_file, data_files)

    @patch('os.path.exists')
    @patch('os.remove')
    @patch('os.path.getmtime')
    def test_cleanup(self, mock_getmtime, mock_remove, mock_exists) -> None:
        """Test cleaning up registered files."""
        # Setup mocks
        mock_exists.return_value = True
        mock_getmtime.return_value = 0  # Files will be "old"

        # Register files
        test_file1 = "/tmp/test_file1.txt"
        test_file2 = "/tmp/test_file2.txt"

        self.registry.register(test_file1, "temp")
        self.registry.register(test_file2, "data")

        # Clean up only temp files
        attempted, deleted = self.registry.cleanup("temp")
        self.assertEqual(attempted, 1)
        self.assertEqual(deleted, 1)
        mock_remove.assert_called_once_with(test_file1)

        # Clean up all remaining files
        mock_remove.reset_mock()
        attempted, deleted = self.registry.cleanup()
        self.assertEqual(attempted, 1)  # Only 1 file left
        self.assertEqual(deleted, 1)
        mock_remove.assert_called_once_with(test_file2)


class TestFileManager(unittest.TestCase):
    """Test cases for the FileManager class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Use a temporary directory for testing
        self.temp_dir = tempfile.mkdtemp()

        # Create a patcher for the config.logger
        self.logger_patcher = patch('src.utils.file_manager.logger')
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
        self.assertIs(fm1, fm2)

    def test_directory_setup(self) -> None:
        """Test that directories are set up correctly."""
        # Check that directories were created
        self.assertTrue(os.path.exists(self.file_manager.var_dir))
        self.assertTrue(os.path.exists(self.file_manager.debug_dir))
        self.assertTrue(os.path.exists(self.file_manager.data_dir))
        self.assertTrue(os.path.exists(self.file_manager.temp_dir))

    def test_generate_unique_id(self) -> None:
        """Test generating unique IDs."""
        # Generate two IDs and verify they're different
        id1 = self.file_manager.generate_unique_id()
        id2 = self.file_manager.generate_unique_id()

        self.assertNotEqual(id1, id2)

        # Verify the format (should have date and time components)
        self.assertRegex(id1, r'\d{8}_\d{6}_\d{6}_[a-z0-9]{4}')

    def test_create_debug_session(self) -> None:
        """Test creating debug sessions."""
        # Create a debug session
        session_dir = self.file_manager.create_debug_session()

        # Verify it was created
        self.assertTrue(os.path.exists(session_dir))

        # Verify the debug log file was created
        log_file = os.path.join(session_dir, "debug_log.txt")
        self.assertTrue(os.path.exists(log_file))

        # Verify it's registered
        self.assertIn(session_dir, self.file_manager.registry.get_files("debug"))

    def test_add_to_debug_log(self) -> None:
        """Test adding messages to debug logs."""
        # Create a debug session
        session_dir = self.file_manager.create_debug_session()

        # Add a message
        test_message = "Test debug message"
        self.file_manager.add_to_debug_log(session_dir, test_message)

        # Verify the message was added
        log_file = os.path.join(session_dir, "debug_log.txt")
        with open(log_file) as f:
            content = f.read()

        self.assertIn(test_message, content)

    def test_create_data_file(self) -> None:
        """Test creating data files."""
        # Create a data file with dictionary data
        test_data = {"key": "value", "number": 42}
        file_path = self.file_manager.create_data_file(test_data)

        # Verify it was created
        self.assertTrue(os.path.exists(file_path))

        # Verify it's registered
        self.assertIn(file_path, self.file_manager.registry.get_files("data"))

        # Verify the content
        with open(file_path) as f:
            content = json.load(f)

        self.assertEqual(content, test_data)

    def test_create_script_file(self) -> None:
        """Test creating script files."""
        # Create a script file
        script_content = "puts 'Hello, World!'"
        file_path = self.file_manager.create_script_file(script_content)

        # Verify it was created
        self.assertTrue(os.path.exists(file_path))

        # Verify it's registered
        self.assertIn(file_path, self.file_manager.registry.get_files("script"))

        # Verify the content
        with open(file_path) as f:
            content = f.read()

        self.assertEqual(content, script_content)

    def test_read_file(self) -> None:
        """Test reading files."""
        # Create a file with content
        test_content = "Test file content"
        test_file = os.path.join(self.file_manager.temp_dir, "test_file.txt")

        with open(test_file, 'w') as f:
            f.write(test_content)

        # Read the file
        content = self.file_manager.read_file(test_file)

        # Verify the content
        self.assertEqual(content, test_content)

    def test_read_json_file(self) -> None:
        """Test reading JSON files."""
        # Create a JSON file with content
        test_data = {"key": "value", "number": 42}
        test_file = os.path.join(self.file_manager.temp_dir, "test_file.json")

        with open(test_file, 'w') as f:
            json.dump(test_data, f)

        # Read the JSON file
        data = self.file_manager.read_json_file(test_file)

        # Verify the content
        self.assertEqual(data, test_data)

    def test_copy_file(self) -> None:
        """Test copying files."""
        # Create a source file
        test_content = "Test file content"
        source_file = os.path.join(self.file_manager.temp_dir, "source.txt")

        with open(source_file, 'w') as f:
            f.write(test_content)

        # Destination file
        dest_file = os.path.join(self.file_manager.temp_dir, "destination.txt")

        # Copy the file
        result = self.file_manager.copy_file(source_file, dest_file)

        # Verify the file was copied
        self.assertTrue(os.path.exists(dest_file))
        self.assertEqual(result, dest_file)

        # Verify the content
        with open(dest_file) as f:
            content = f.read()

        self.assertEqual(content, test_content)

        # Verify it's registered
        self.assertIn(dest_file, self.file_manager.registry.get_files("temp"))

    @patch.object(FileRegistry, 'cleanup')
    def test_cleanup_old_files(self, mock_cleanup) -> None:
        """Test cleaning up old files."""
        # Set up mock
        mock_cleanup.return_value = (10, 5)

        # Call cleanup_old_files
        self.file_manager.cleanup_old_files(days=7)

        # Verify cleanup was called with the right parameters
        mock_cleanup.assert_called_once()
        # First positional argument should be None, second should be seconds (7 days)
        self.assertEqual(mock_cleanup.call_args[1]['older_than'], 7 * 24 * 60 * 60)

        # Verify logger was called
        self.mock_logger.info.assert_called_once()

    @patch.object(FileRegistry, 'cleanup')
    def test_cleanup_all(self, mock_cleanup) -> None:
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
