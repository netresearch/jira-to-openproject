#!/usr/bin/env python3
"""Tests for the BaseMigration class, focused on dependency injection."""

import unittest
from unittest.mock import MagicMock, patch

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.base_migration import BaseMigration


class TestBaseMigration(unittest.TestCase):
    """Test cases for BaseMigration class."""

    @patch("src.migrations.base_migration.JiraClient")
    @patch("src.migrations.base_migration.OpenProjectClient")
    def test_init_without_dependencies(self, mock_op_client: MagicMock, mock_jira_client: MagicMock) -> None:
        """Test initialization without providing any client instances."""
        # Setup the mocks
        mock_jira_instance = MagicMock()
        mock_op_instance = MagicMock()
        mock_jira_client.return_value = mock_jira_instance
        mock_op_client.return_value = mock_op_instance

        # Create BaseMigration instance without dependencies
        migration = BaseMigration()

        # Verify clients were created
        mock_jira_client.assert_called_once()
        mock_op_client.assert_called_once()

        # Verify the instance has the expected clients
        assert migration.jira_client == mock_jira_instance
        assert migration.op_client == mock_op_instance

    def test_init_with_jira_client(self) -> None:
        """Test initialization with JiraClient provided."""
        # Create a mock JiraClient
        mock_jira = MagicMock(spec=JiraClient)

        # Create BaseMigration with the mock JiraClient
        with patch("src.migrations.base_migration.OpenProjectClient") as mock_op_client:
            mock_op_instance = MagicMock()
            mock_op_client.return_value = mock_op_instance

            migration = BaseMigration(jira_client=mock_jira)

            # Verify JiraClient was not created, but OpenProjectClient was
            assert migration.jira_client == mock_jira
            assert migration.op_client == mock_op_instance
            mock_op_client.assert_called_once()

    def test_init_with_op_client(self) -> None:
        """Test initialization with OpenProjectClient provided."""
        # Create a mock OpenProjectClient
        mock_op = MagicMock(spec=OpenProjectClient)

        # Create BaseMigration with the mock OpenProjectClient
        with patch("src.migrations.base_migration.JiraClient") as mock_jira_client:
            mock_jira_instance = MagicMock()
            mock_jira_client.return_value = mock_jira_instance

            migration = BaseMigration(op_client=mock_op)

            # Verify OpenProjectClient was not created, but JiraClient was
            assert migration.op_client == mock_op
            assert migration.jira_client == mock_jira_instance
            mock_jira_client.assert_called_once()

    def test_init_with_both_clients(self) -> None:
        """Test initialization with both clients provided."""
        # Create mock clients
        mock_jira = MagicMock(spec=JiraClient)
        mock_op = MagicMock(spec=OpenProjectClient)

        # Create BaseMigration with all mock clients
        migration = BaseMigration(jira_client=mock_jira, op_client=mock_op)

        # Verify both clients were used and not recreated
        assert migration.jira_client == mock_jira
        assert migration.op_client == mock_op


if __name__ == "__main__":
    unittest.main()
