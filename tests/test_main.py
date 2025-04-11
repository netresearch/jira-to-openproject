"""
Tests for the main entry point (src/main.py).
"""

import unittest
from unittest.mock import patch, MagicMock
import sys
import argparse
from src.main import main


class TestMainEntryPoint(unittest.TestCase):
    """Test cases for the main entry point."""

    @patch('src.main.argparse.ArgumentParser.parse_args')
    @patch('src.main.run_migration')
    def test_migrate_command(self, mock_run_migration, mock_parse_args):
        """Test the 'migrate' command."""
        # Set up mock args
        mock_args = argparse.Namespace(
            command='migrate',
            dry_run=True,
            components=['users', 'projects'],
            no_backup=False,
            force=False,
            direct_migration=False,
            restore=None,
            tmux=False
        )
        mock_parse_args.return_value = mock_args

        # Set up mock result
        mock_run_migration.return_value = {
            "overall": {"status": "success"},
            "components": {
                "users": {"status": "success"},
                "projects": {"status": "success"}
            }
        }

        # Test main function with mocked exit
        with patch('sys.exit') as mock_exit:
            main()

            # Check that run_migration was called with correct arguments
            mock_run_migration.assert_called_once_with(
                dry_run=True,
                components=['users', 'projects'],
                no_backup=False,
                force=False,
                direct_migration=False
            )

            # Check that sys.exit was called with 0 (success)
            mock_exit.assert_called_once_with(0)

    @patch('src.main.argparse.ArgumentParser.parse_args')
    @patch('src.main.export_work_packages')
    @patch('src.main.JiraClient')
    @patch('src.main.OpenProjectClient')
    @patch('src.main.OpenProjectRailsClient')
    def test_export_command(self, mock_rails_client, mock_op_client, mock_jira_client,
                            mock_export_wp, mock_parse_args):
        """Test the 'export' command."""
        # Set up mock args
        mock_args = argparse.Namespace(
            command='export',
            dry_run=False,
            force=True,
            projects=['TEST1', 'TEST2']
        )
        mock_parse_args.return_value = mock_args

        # Set up mock clients
        mock_jira_client_instance = MagicMock()
        mock_op_client_instance = MagicMock()
        mock_jira_client.return_value = mock_jira_client_instance
        mock_op_client.return_value = mock_op_client_instance

        # Set up mock result
        mock_export_wp.return_value = {
            "status": "success",
            "exported_projects": 2,
            "total_work_packages": 100
        }

        # Test main function with mocked exit
        with patch('sys.exit') as mock_exit:
            main()

            # Check that export_work_packages was called with correct arguments
            mock_export_wp.assert_called_once()
            args, kwargs = mock_export_wp.call_args
            self.assertEqual(kwargs['jira_client'], mock_jira_client_instance)
            self.assertEqual(kwargs['op_client'], mock_op_client_instance)
            self.assertEqual(kwargs['dry_run'], False)
            self.assertEqual(kwargs['force'], True)
            self.assertEqual(kwargs['project_keys'], ['TEST1', 'TEST2'])

            # Check that sys.exit was called with 0 (success)
            mock_exit.assert_called_once_with(0)

    @patch('src.main.argparse.ArgumentParser.parse_args')
    @patch('src.main.import_work_packages_to_rails')
    def test_import_command(self, mock_import_wp, mock_parse_args):
        """Test the 'import' command."""
        # Set up mock args
        mock_args = argparse.Namespace(
            command='import',
            project='TEST1',
            export_dir='/path/to/exports'
        )
        mock_parse_args.return_value = mock_args

        # Set up mock result
        mock_import_wp.return_value = {
            "status": "success",
            "imported_work_packages": 50
        }

        # Test main function with mocked exit
        with patch('sys.exit') as mock_exit:
            main()

            # Check that import_work_packages_to_rails was called with correct arguments
            mock_import_wp.assert_called_once_with(
                export_dir='/path/to/exports',
                project_key='TEST1'
            )

            # Check that sys.exit was called with 0 (success)
            mock_exit.assert_called_once_with(0)

    @patch('src.main.argparse.ArgumentParser.parse_args')
    @patch('src.main.argparse.ArgumentParser.print_help')
    def test_no_command(self, mock_print_help, mock_parse_args):
        """Test behavior when no command is provided."""
        # Set up mock args with no command
        mock_args = argparse.Namespace(command=None)
        mock_parse_args.return_value = mock_args

        # Test main function with mocked exit
        with patch('sys.exit') as mock_exit:
            main()

            # Check that help was printed and exit code 1 was used
            mock_print_help.assert_called_once()
            mock_exit.assert_called_once_with(1)


if __name__ == '__main__':
    unittest.main()
