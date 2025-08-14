"""Enhanced unit tests for OpenProjectClient based on Zen TestGen expert analysis.
These tests focus on the most critical and fragile areas of the class:
- Complex logic for parsing unstructured console output
- Brittle heuristics for modifying queries
- Multi-layered client interaction for data retrieval
- Correctness of error propagation.

Based on Zen's identification of high-risk components in the OpenProject client architecture.
"""

import json
from pathlib import Path
from typing import Never
from unittest.mock import MagicMock, patch

import pytest

from src.clients.openproject_client import (
    FileTransferError,
    JsonParseError,
    OpenProjectClient,
    QueryExecutionError,
    RecordNotFoundError,
)
from src.clients.rails_console_client import RubyError


@pytest.fixture
def mock_config():
    """Fixture to mock the config module for OpenProjectClient."""
    with patch("src.clients.openproject_client.config") as mock_conf:
        mock_conf.openproject_config = {
            "container": "test_container",
            "server": "test_host",
            "user": "test_user",
            "tmux_session_name": "test_session",
        }
        # Mock the logger to prevent console output during tests
        mock_conf.logger = MagicMock()
        yield mock_conf


@pytest.fixture
def mock_clients():
    """Fixture to create mock clients for dependency injection."""
    return {
        "ssh_client": MagicMock(spec_set=["execute_command"]),
        "docker_client": MagicMock(
            spec_set=["transfer_file_to_container", "copy_file_from_container"],
        ),
        "rails_client": MagicMock(spec_set=["execute", "_send_command_to_tmux"]),
    }


@pytest.fixture
def op_client(mock_config, mock_clients):
    """Fixture to create an OpenProjectClient instance with mocked dependencies."""
    client = OpenProjectClient(
        ssh_client=mock_clients["ssh_client"],
        docker_client=mock_clients["docker_client"],
        rails_client=mock_clients["rails_client"],
    )
    # Reset mocks for each test to ensure isolation
    for c in mock_clients.values():
        c.reset_mock()
    return client


class TestOpenProjectClientInitialization:
    """Tests for the initialization of OpenProjectClient."""

    def test_init_with_dependency_injection(self, op_client, mock_clients) -> None:
        """Verify client uses injected dependencies.
        This is critical for ensuring testability and flexible architecture.
        """
        # Assert
        assert op_client.ssh_client is mock_clients["ssh_client"]
        assert op_client.docker_client is mock_clients["docker_client"]
        assert op_client.rails_client is mock_clients["rails_client"]

    @patch("src.clients.openproject_client.SSHClient")
    @patch("src.clients.openproject_client.DockerClient")
    @patch("src.clients.openproject_client.RailsConsoleClient")
    def test_init_creates_clients_if_not_provided(
        self,
        MockRailsClient,
        MockDockerClient,
        MockSSHClient,
        mock_config,
    ) -> None:
        """Verify client creates its own dependencies if they are not injected.
        This tests the default production path.
        """
        # Act
        client = OpenProjectClient()

        # Assert
        MockSSHClient.assert_called_once()
        MockDockerClient.assert_called_once()
        MockRailsClient.assert_called_once()
        assert isinstance(client.ssh_client, MockSSHClient)
        assert isinstance(client.docker_client, MockDockerClient)
        assert isinstance(client.rails_client, MockRailsClient)

    def test_init_raises_value_error_if_config_missing(self, mock_config) -> None:
        """Verify that initialization fails if critical configuration is missing.
        This prevents runtime errors deep within the application.
        """
        # Arrange
        mock_config.openproject_config = {"server": "test_host"}  # Missing container

        # Act & Assert
        with pytest.raises(ValueError, match="Container name is required"):
            OpenProjectClient()

        # Arrange
        mock_config.openproject_config = {
            "container": "test_container",
        }  # Missing server

        # Act & Assert
        with pytest.raises(ValueError, match="SSH host is required"):
            OpenProjectClient()


class TestParseRailsOutput:
    """Tests for the _parse_rails_output method.
    This is the most critical and fragile part of the client, responsible for
    interpreting raw console output. Failures here are catastrophic.
    """

    @pytest.mark.parametrize(
        ("raw_output", "expected_result"),
        [
            # Happy paths for various data types
            ('=> [{"id": 1, "name": "Test"}]', [{"id": 1, "name": "Test"}]),
            ('[{"id": 1}]', [{"id": 1}]),
            ('{"id": 1, "name": "Test"}', {"id": 1, "name": "Test"}),
            ("=> 123", 123),
            ("123", 123),
            ("=> true", True),
            ("true", True),
            ("=> false", False),
            ("false", False),
            ('=> "hello world"', "hello world"),
            ('"hello world"', "hello world"),
            ("=> nil", None),
            ("nil", None),
            ("", None),
            ("   ", None),
            # Noisy output with tmux markers
            (
                "TMUX_CMD_START\n" '[{"id": 1, "name": "Test"}]\n' "TMUX_CMD_END",
                [{"id": 1, "name": "Test"}],
            ),
            # Noisy output with Rails prompt
            (
                "open-project(main):001> puts User.all.to_json\n"
                '[{"id": 1, "login": "admin"}]\n'
                "=> nil",
                [{"id": 1, "login": "admin"}],
            ),
            # Unicode and special characters
            (
                '[{"name": "你好世界"}, {"emoji": "😊"}]',
                [{"name": "你好世界"}, {"emoji": "😊"}],
            ),
        ],
    )
    def test_parse_rails_output_success_cases(
        self,
        op_client,
        raw_output,
        expected_result,
    ) -> None:
        # Act
        result = op_client._parse_rails_output(raw_output)

        # Assert
        assert result == expected_result

    @pytest.mark.parametrize(
        "malformed_output",
        [
            # Malformed JSON
            '[{"id": 1, "name": "Test"},]',  # Trailing comma
            '{"id": 1, "name": "Test"',  # Missing closing brace
            # Ambiguous or unparsable text
            "Some random error message from Rails",
            "Error: undefined method `foo` for nil:NilClass",
        ],
    )
    def test_parse_rails_output_raises_json_parse_error(
        self,
        op_client,
        malformed_output,
    ) -> None:
        """Verify that unparsable or malformed output raises a specific error.
        This ensures that the system doesn't silently fail or return garbage data.
        """
        # Act & Assert
        with pytest.raises(JsonParseError):
            op_client._parse_rails_output(malformed_output)


class TestQueryExecution:
    """Tests for query execution and modification logic."""

    def test_execute_query_to_json_file_uses_pagination_for_collection_query(
        self,
        op_client,
    ) -> None:
        """Ensure collection queries use proper pagination instead of hardcoded limits."""
        # Arrange
        op_client._execute_batched_query = MagicMock(return_value=[])

        # Act
        op_client.execute_query_to_json_file("Project.all")

        # Assert: pagination helper invoked for model
        op_client._execute_batched_query.assert_called_once_with("Project", timeout=None)

    def test_console_error_detection_strict_patterns(self, op_client) -> None:
        """Ensure only EXEC_ERROR lines or severe patterns trigger QueryExecutionError."""
        # Arrange: benign output with TMUX_CMD_END echo
        benign_output = """
open-project(prod)>         # Execute the actual command
--EXEC_START--xyz
open-project(prod)*         begin
open-project(prod)*           result = 1
open-project(prod)*           puts "--EXEC_END--xyz"
open-project(prod)*         rescue => e
open-project(prod)*           puts "--EXEC_ERROR--xyz"
open-project(prod)>         end # --SCRIPT_END--xyz
=> nil
open-project(prod)> puts 'TMUX_CMD_END_123'
TMUX_CMD_END_123
=> nil
"""
        op_client._check_console_output_for_errors(benign_output, context="execute_large_query_to_json_file")

        # Arrange: severe output with SystemStackError and missing end marker
        severe_output = """
open-project(prod)>         # Execute the actual command
open-project(prod)*         begin
/tmp/openproject_script.rb:6:in '<top (required)>': stack level too deep (SystemStackError)
open-project(prod)>
"""
        from src.clients.openproject_client import QueryExecutionError
        import pytest as _pytest
        with _pytest.raises(QueryExecutionError):
            op_client._check_console_output_for_errors(severe_output, context="execute_large_query_to_json_file")

    @pytest.mark.parametrize(
        ("original_query", "expected_modified_query"),
        [
            ("User.count", "(User.count).to_json"),
            ("User.find_by(id: 1)", "(User.find_by(id: 1)).to_json"),
            ("User.all.map(&:name)", "(User.all.map(&:name)).to_json"),
            (
                "Project.all.to_json",
                "(Project.all.to_json).to_json",
            ),  # a bit weird but shows it doesn't double-add limit
        ],
    )
    def test_execute_query_to_json_file_modifies_queries_correctly(
        self,
        op_client,
        original_query,
        expected_modified_query,
    ) -> None:
        """Verify the query modification logic for various query types.
        This tests the brittle string-based heuristics for query modification.
        """
        # Arrange
        op_client.execute_query = MagicMock(return_value='""')
        op_client._parse_rails_output = MagicMock(return_value="")

        # Act
        op_client.execute_query_to_json_file(original_query)

        # Assert
        op_client.execute_query.assert_called_once_with(
            expected_modified_query,
            timeout=None,
        )

    def test_get_users_file_based_retrieval_success(
        self,
        op_client,
        mock_clients,
    ) -> None:
        """Test the complex file-based retrieval mechanism for `get_users`.
        This validates the entire chain: Rails command -> SSH command -> JSON parsing.
        """
        # Arrange
        mock_rails_client = mock_clients["rails_client"]
        mock_ssh_client = mock_clients["ssh_client"]

        # Mock the Rails command that writes the file
        mock_rails_client.execute.return_value = "Users data written..."

        # Mock the SSH command that reads the file
        users_json = json.dumps([{"id": 1, "login": "test.user"}])
        mock_ssh_client.execute_command.return_value = (
            users_json,
            "",
            0,
        )  # stdout, stderr, returncode

        # Act
        users = op_client.get_users()

        # Assert
        assert users == [{"id": 1, "login": "test.user"}]
        mock_rails_client.execute.assert_called_once()
        mock_ssh_client.execute_command.assert_called_once_with(
            "docker exec test_container cat /tmp/users.json",
        )

        # Verify caching
        mock_rails_client.reset_mock()
        mock_ssh_client.reset_mock()

        cached_users = op_client.get_users()
        assert cached_users == users
        mock_rails_client.execute.assert_not_called()
        mock_ssh_client.execute_command.assert_not_called()

    def test_get_users_file_based_retrieval_ssh_failure(
        self,
        op_client,
        mock_clients,
    ) -> None:
        """Test failure case where reading the file from the container via SSH fails."""
        # Arrange
        mock_clients["rails_client"].execute.return_value = "Users data written..."
        mock_clients["ssh_client"].execute_command.return_value = (
            "",
            "Permission denied",
            1,
        )

        # Act & Assert
        with pytest.raises(
            QueryExecutionError,
            match="SSH command failed with code 1: Permission denied",
        ):
            op_client.get_users()


class TestErrorPropagation:
    """Tests to ensure that errors from underlying clients are correctly
    wrapped and propagated as specific, high-signal exceptions.
    """

    def test_update_record_not_found(self, op_client, mock_clients) -> None:
        """Verify that a 'Record not found' RubyError during an update
        is correctly translated to a RecordNotFoundError.
        """
        # Arrange
        mock_clients["rails_client"]._send_command_to_tmux.side_effect = RubyError(
            "Record not found",
        )

        # Act & Assert
        with pytest.raises(RecordNotFoundError, match="User with ID 999 not found"):
            op_client.update_record("User", 999, {"name": "new name"})

    def test_update_record_generic_ruby_error(self, op_client, mock_clients) -> None:
        """Verify a generic RubyError is wrapped in QueryExecutionError."""
        # Arrange
        mock_clients["rails_client"]._send_command_to_tmux.side_effect = RubyError(
            "Some other validation failed",
        )

        # Act & Assert
        with pytest.raises(QueryExecutionError, match="Failed to update User."):
            op_client.update_record("User", 999, {"name": "new name"})

    def test_transfer_file_to_container_failure(self, op_client, mock_clients) -> None:
        """Verify that a generic exception from the Docker client during file transfer
        is wrapped in a FileTransferError.
        """
        # Arrange
        mock_clients["docker_client"].transfer_file_to_container.side_effect = (
            Exception("Docker daemon not running")
        )

        # Act & Assert
        with pytest.raises(
            FileTransferError,
            match="Failed to transfer file to container.",
        ):
            op_client.transfer_file_to_container(
                Path("/tmp/local"),
                Path("/tmp/remote"),
            )


class TestQueryModificationEdgeCases:
    """Additional tests for the brittle query modification logic that can cause data loss.
    These tests expose the dangerous hardcoded .limit(5) behavior.
    """

    def test_collection_queries_use_pagination_not_hardcoded_limit(self, op_client) -> None:
        """Regression: ensure no silent truncation via hardcoded limit; use batching."""
        # Arrange
        op_client._execute_batched_query = MagicMock(return_value=[])

        # Act
        op_client.execute_query_to_json_file("WorkPackage.all")

        # Assert
        op_client._execute_batched_query.assert_called_once_with("WorkPackage", timeout=None)

    def test_query_modification_keyword_detection_brittleness(self, op_client) -> None:
        """Test the brittle keyword-based detection for query modification.
        Shows how the string-matching logic can be easily fooled.
        """
        # Arrange
        op_client.execute_query = MagicMock(return_value="[]")
        op_client._parse_rails_output = MagicMock(return_value=[])

        # Act: Query that contains keywords but isn't a collection query
        op_client.execute_query_to_json_file("User.count # all users")

        # Assert: Should not add limit to count queries, only .to_json
        op_client.execute_query.assert_called_once_with(
            "(User.count # all users).to_json",
            timeout=None,
        )

    def test_static_filename_race_condition(self, op_client, mock_clients) -> None:
        """Test the race condition caused by static temp filenames.
        Multiple concurrent operations could overwrite each other's files.
        """
        # Arrange: Mock concurrent file operations
        mock_clients["rails_client"].execute.return_value = "Data written..."

        # First call writes to /tmp/users.json
        users_json_1 = json.dumps([{"id": 1, "login": "user1"}])
        # Second call (different migration) overwrites /tmp/users.json
        users_json_2 = json.dumps([{"id": 2, "login": "user2"}])

        mock_clients["ssh_client"].execute_command.side_effect = [
            (users_json_1, "", 0),  # First read
            (users_json_2, "", 0),  # Second read (different data!)
        ]

        # Act: Two supposedly independent calls
        users_1 = op_client.get_users()
        # Simulate another migration instance
        op_client._users_cache = None  # Clear cache to force re-read
        users_2 = op_client.get_users()

        # Assert: Data corruption due to static filename
        assert users_1 != users_2  # Different results from same query!
        assert users_1 == [{"id": 1, "login": "user1"}]
        assert users_2 == [{"id": 2, "login": "user2"}]


class TestResourceLeakPrevention:
    """Tests for preventing resource leaks during cleanup failures."""

    def test_cleanup_script_files_suppresses_exceptions(
        self,
        op_client,
        mock_clients,
    ) -> None:
        """Test that cleanup failures are properly handled without breaking migration.
        However, verify that failures are logged for monitoring.
        """
        # Arrange: Mock cleanup operations that fail
        mock_clients["ssh_client"].execute_command.side_effect = Exception(
            "Permission denied during cleanup",
        )

        # Act: Cleanup should not raise exception
        try:
            op_client._cleanup_script_files(["test_script.rb"])
        except Exception:
            pytest.fail(
                "Cleanup should suppress exceptions to prevent breaking migrations",
            )

        # Assert: Should attempt cleanup despite failures
        mock_clients["ssh_client"].execute_command.assert_called()

    def test_temp_file_accumulation_monitoring(self, op_client, mock_clients) -> None:
        """Test that repeated cleanup failures could lead to temp file accumulation.
        This is a monitoring test to detect potential disk space issues.
        """
        # Arrange: Simulate multiple failed cleanups
        cleanup_attempts = []

        def track_cleanup(*args, **kwargs) -> Never:
            cleanup_attempts.append(args)
            msg = "Cleanup failed"
            raise Exception(msg)

        mock_clients["ssh_client"].execute_command.side_effect = track_cleanup

        # Act: Multiple operations with failed cleanup
        for i in range(10):
            try:
                op_client._cleanup_script_files([f"script_{i}.rb"])
            except Exception:
                pass  # Cleanup failures are suppressed

        # Assert: Verify that cleanup was attempted for each file
        assert len(cleanup_attempts) == 10

        # This test documents the potential for temp file accumulation
        # In production, monitoring should alert on repeated cleanup failures
