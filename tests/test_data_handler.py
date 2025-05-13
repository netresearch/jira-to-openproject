"""
Tests for the data_handler utility module.
"""

import os
import tempfile
import unittest

from src.models import ComponentResult
from src.utils import data_handler


class TestDataHandler(unittest.TestCase):
    """Test cases for the data_handler module."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Create a temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        self.test_filename = "test_data.json"
        self.test_filepath = os.path.join(self.temp_dir, self.test_filename)

    def tearDown(self) -> None:
        """Clean up after tests."""
        # Remove test files
        if os.path.exists(self.test_filepath):
            os.remove(self.test_filepath)
        os.rmdir(self.temp_dir)

    def test_save_and_load_pydantic_model(self) -> None:
        """Test saving and loading a Pydantic model."""
        # Create a test model
        test_result = ComponentResult(
            success=True, message="Test successful", details={"total": 10, "success": 8, "failed": 2}
        )

        # Save the model
        success = data_handler.save(test_result, self.test_filename, directory=self.temp_dir)
        self.assertTrue(success)
        self.assertTrue(os.path.exists(self.test_filepath))

        # Load the model
        loaded_result = data_handler.load(ComponentResult, self.test_filename, directory=self.temp_dir)

        # Verify loaded model matches original
        self.assertIsNotNone(loaded_result)
        self.assertEqual(loaded_result.success, test_result.success)
        self.assertEqual(loaded_result.message, test_result.message)
        self.assertEqual(loaded_result.details, test_result.details)

    def test_save_and_load_dict(self) -> None:
        """Test saving and loading a dictionary."""
        test_data = {"key1": "value1", "key2": 123, "nested": {"a": 1, "b": 2}}

        # Save the dictionary
        success = data_handler.save(test_data, self.test_filename, directory=self.temp_dir)
        self.assertTrue(success)

        # Load as a dictionary
        loaded_data = data_handler.load_dict(self.test_filename, directory=self.temp_dir)

        # Verify loaded data matches original
        self.assertEqual(loaded_data, test_data)

    def test_save_and_load_list(self) -> None:
        """Test saving and loading a list."""
        test_data = [1, 2, 3, {"key": "value"}, [4, 5, 6]]

        # Save the list
        success = data_handler.save(test_data, self.test_filename, directory=self.temp_dir)
        self.assertTrue(success)

        # Load as a list
        loaded_data = data_handler.load_list(self.test_filename, directory=self.temp_dir)

        # Verify loaded data matches original
        self.assertEqual(loaded_data, test_data)

    def test_save_and_load_with_path(self) -> None:
        """Test saving and loading using direct file paths."""
        # Create a test model
        test_result = ComponentResult(success=False, message="Path test", errors=["Error 1", "Error 2"])

        # Save using path
        success = data_handler.save_to_path(test_result, self.test_filepath)
        self.assertTrue(success)

        # Load using path
        loaded_result = data_handler.load_from_path(ComponentResult, self.test_filepath)

        # Verify loaded model matches original
        self.assertIsNotNone(loaded_result)
        self.assertEqual(loaded_result.success, test_result.success)
        self.assertEqual(loaded_result.message, test_result.message)
        self.assertEqual(loaded_result.errors, test_result.errors)

    def test_load_nonexistent_file(self) -> None:
        """Test loading a file that doesn't exist."""
        # Try to load a nonexistent file
        result = data_handler.load(ComponentResult, "nonexistent.json", directory=self.temp_dir)
        self.assertIsNone(result)

        # Try with a default value
        default = ComponentResult(success=False, message="Default")
        result = data_handler.load(ComponentResult, "nonexistent.json", directory=self.temp_dir, default=default)
        self.assertEqual(result, default)

    def test_handling_invalid_json(self) -> None:
        """Test handling invalid JSON data."""
        # Create an invalid JSON file
        with open(self.test_filepath, "w") as f:
            f.write("{invalid json")

        # Try to load the invalid file
        result = data_handler.load(ComponentResult, self.test_filename, directory=self.temp_dir)
        self.assertIsNone(result)

        # Try with a default value
        default = ComponentResult(success=False, message="Default")
        result = data_handler.load(ComponentResult, self.test_filename, directory=self.temp_dir, default=default)
        self.assertEqual(result, default)
