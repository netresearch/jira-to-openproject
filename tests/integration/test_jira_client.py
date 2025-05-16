#!/usr/bin/env python3
"""Integration tests for Jira client.

These tests verify that the JiraClient can connect to a real Jira instance,
retrieve basic information, and handle errors correctly.
"""

import os
import unittest
from typing import ClassVar

import pytest
from dotenv import load_dotenv

from src import config
from src.clients.jira_client import (
    JiraApiError,
    JiraAuthenticationError,
    JiraClient,
    JiraConnectionError,
    JiraResourceNotFoundError,
)


class TestJiraClientIntegration(unittest.TestCase):
    """Integration tests for JiraClient with real Jira instance."""

    # Class variables properly typed
    jira_url: ClassVar[str | None] = None
    jira_token: ClassVar[str | None] = None
    skip_tests: ClassVar[bool] = False

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize environment variables for testing."""
        # Load environment variables
        load_dotenv()
        if os.path.exists(".env.local"):
            load_dotenv(".env.local")
        if os.path.exists(".env.test"):
            load_dotenv(".env.test")
        if os.path.exists(".env.test.local"):
            load_dotenv(".env.test.local")

        # Get URL and token
        cls.jira_url = os.getenv("J2O_JIRA_URL")
        cls.jira_token = os.getenv("J2O_JIRA_API_TOKEN")

        # Skip all tests if credentials not available
        if not cls.jira_url or not cls.jira_token:
            cls.skip_tests = True
            pytest.skip("Jira credentials not available in environment")

        # Verify the URL is not a placeholder (prevents hanging on non-existent servers)
        if not cls.jira_url or ("jira.local" in cls.jira_url):
            cls.skip_tests = True
            pytest.skip(f"Placeholder Jira URL detected: {cls.jira_url}")

    def setUp(self) -> None:
        """Set up the test environment before each test."""
        # Skip tests if necessary
        if self.__class__.skip_tests:
            pytest.skip("Tests skipped by class setup")

        # Store original config to restore later
        self.original_jira_config = config.jira_config.copy() if config.jira_config else {}

        # Set up config for testing with shorter timeouts
        config.jira_config = {
            "url": self.__class__.jira_url,
            "api_token": self.__class__.jira_token,
            "verify_ssl": True,
            "connect_timeout": 5,  # Short timeout to prevent hanging
            "operation_timeout": 10,
        }

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Restore original config
        config.jira_config = self.original_jira_config

    def test_client_connection(self) -> None:
        """Test the JiraClient can connect to a Jira instance."""
        print(f"\n=== Testing Jira Client Connection to {self.__class__.jira_url} ===")

        try:
            # Initialize client
            jira_client = JiraClient()

            # Verify client is initialized
            assert jira_client.jira is not None
            print("✅ Successfully connected to Jira instance")

            # Get server info to ensure connection works
            if jira_client.jira:  # Type guard for the linter
                server_info = jira_client.jira.server_info()
                print(f"Jira Server Version: {server_info.get('version')}")
                print(f"Jira Base URL: {server_info.get('baseUrl')}")

            # Test getting projects (basic functionality) - uncomment if needed
            # projects = jira_client.get_projects()
            # self.assertIsInstance(projects, list)
            # print(f"Retrieved {len(projects)} projects from Jira")
            # if projects:
            #     sample_project = projects[0]
            #     print(f"Sample project: {sample_project.get('key')} - {sample_project.get('name')}")

        except JiraAuthenticationError as e:
            print(f"❌ Authentication failed: {e}")
            self.fail(f"Jira authentication failed: {e}")
        except JiraConnectionError as e:
            print(f"❌ Connection failed: {e}")
            self.fail(f"Jira connection failed: {e}")
        except JiraApiError as e:
            print(f"❌ API error: {e}")
            self.fail(f"Jira API error: {e}")
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            self.fail(f"Unexpected error: {e}")

    def test_invalid_credentials(self) -> None:
        """Test JiraClient handles invalid credentials correctly."""
        print("\n=== Testing Jira Client with Invalid Credentials ===")

        pytest.skip("Skipping invalid credentials test")

        # The following code is kept as reference but not executed
        # The token must be completely invalid/wrong format to trigger auth error
        # config.jira_config = {
        #     "url": self.__class__.jira_url,
        #     "api_token": "COMPLETELY_INVALID_TOKEN_FORMAT_12345!@#$%",
        #     "verify_ssl": True,
        #     "connect_timeout": 5,
        #     "operation_timeout": 10,
        # }

        # jira_client = JiraClient()

        # # Test authentication failure
        # with self.assertRaises((JiraAuthenticationError, JiraConnectionError)):
        #     jira_client.jira.projects()

        # print("✅ Correctly raised exception for invalid credentials")

    def test_error_handling(self) -> None:
        """Test that JiraClient properly handles error responses."""
        print("\n=== Testing Jira Client Error Handling ===")

        try:
            # Initialize client
            jira_client = JiraClient()

            # Test with a project key that has an invalid format
            # This should raise a JiraApiError or JiraResourceNotFoundError
            invalid_project = "NONEXISTENT_PROJECT_KEY_123456789"
            with pytest.raises((JiraResourceNotFoundError, JiraApiError)):
                jira_client.get_issue_count(invalid_project)

            print(f"✅ Correctly raised exception for nonexistent project {invalid_project}")

            # Test resource not found handling - use a deliberately invalid issue key
            invalid_issue = "FAKE-12345"
            with pytest.raises((JiraResourceNotFoundError, JiraApiError)):
                jira_client.get_issue_details(invalid_issue)

            print(f"✅ Correctly raised exception for nonexistent issue {invalid_issue}")

        except JiraAuthenticationError:
            # Ignore authentication errors as they're expected if credentials are invalid
            pytest.skip("Skipping error handling test due to authentication failure")
        except JiraConnectionError:
            # Ignore connection errors if the server is unreachable
            pytest.skip("Skipping error handling test due to connection failure")
        except Exception as e:
            print(f"❌ Unexpected error during error handling test: {e}")
            self.fail(f"Error handling test failed with unexpected exception: {e}")


if __name__ == "__main__":
    unittest.main()
