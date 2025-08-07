"""Integration tests for migration orchestration with chaos engineering patterns.
These tests validate the system's resilience under random failure conditions.

Based on Zen TestGen analysis identifying gaps in:
- Multi-component failure cascades
- Error recovery workflows
- State consistency during failures
- Client layer integration issues
"""

import random
from typing import Never
from unittest.mock import MagicMock, patch

import pytest

from src.clients.jira_client import JiraError
from src.clients.openproject_client import ConnectionError, OpenProjectError
from src.migration import Migration
from src.migrations.base_migration import MigrationError
from src.utils.state_manager import StateCorruptionError


class TestMigrationOrchestrationChaos:
    """Chaos engineering tests for migration orchestration."""

    @pytest.fixture
    def mock_migration_components(self):
        """Fixture providing mocked migration components."""
        return {
            "jira_client": MagicMock(),
            "openproject_client": MagicMock(),
            "state_manager": MagicMock(),
            "work_package_migration": MagicMock(),
            "project_migration": MagicMock(),
            "user_migration": MagicMock(),
        }

    @pytest.fixture
    def migration_instance(self, mock_migration_components):
        """Create Migration instance with mocked components."""
        with patch.multiple(
            "src.migration",
            JiraClient=lambda: mock_migration_components["jira_client"],
            OpenProjectClient=lambda: mock_migration_components["openproject_client"],
            StateManager=lambda: mock_migration_components["state_manager"],
        ):
            migration = Migration()
            # Inject mock migration modules
            migration.work_package_migration = mock_migration_components[
                "work_package_migration"
            ]
            migration.project_migration = mock_migration_components["project_migration"]
            migration.user_migration = mock_migration_components["user_migration"]
            return migration

    def test_jira_client_failure_during_initialization(
        self,
        migration_instance,
        mock_migration_components,
    ) -> None:
        """CHAOS TEST: JIRA client fails during migration initialization.
        Validates error propagation and cleanup when first component fails.
        """
        # Arrange: JIRA client fails
        mock_migration_components["jira_client"].connect.side_effect = JiraError(
            "Connection timeout",
        )

        # Act & Assert
        with pytest.raises(MigrationError, match="Failed to initialize JIRA client"):
            migration_instance.run_migration()

        # Verify cleanup was attempted
        mock_migration_components[
            "state_manager"
        ].cleanup_failed_migration.assert_called_once()

    def test_openproject_client_failure_mid_migration(
        self,
        migration_instance,
        mock_migration_components,
    ) -> None:
        """CHAOS TEST: OpenProject client fails after successful JIRA initialization.
        Tests partial failure recovery and state preservation.
        """
        # Arrange: JIRA succeeds, OpenProject fails
        mock_migration_components["jira_client"].connect.return_value = True
        mock_migration_components["openproject_client"].initialize.side_effect = (
            ConnectionError("SSH connection lost")
        )

        # Act & Assert
        with pytest.raises(
            MigrationError,
            match="Failed to initialize OpenProject client",
        ):
            migration_instance.run_migration()

        # Verify state preservation
        mock_migration_components["state_manager"].save_migration_state.assert_called()
        mock_migration_components["state_manager"].mark_migration_failed.assert_called()

    @pytest.mark.parametrize(
        ("failure_point", "expected_cleanup_calls"),
        [
            (
                "user_migration",
                ["cleanup_users", "cleanup_projects", "cleanup_work_packages"],
            ),
            ("project_migration", ["cleanup_projects", "cleanup_work_packages"]),
            ("work_package_migration", ["cleanup_work_packages"]),
        ],
    )
    def test_cascading_cleanup_on_migration_failure(
        self,
        migration_instance,
        mock_migration_components,
        failure_point,
        expected_cleanup_calls,
    ) -> None:
        """CHAOS TEST: Migration fails at different stages, verify proper cleanup cascade.
        Tests that cleanup happens in reverse order of initialization.
        """
        # Arrange: All components initialize successfully
        mock_migration_components["jira_client"].connect.return_value = True
        mock_migration_components["openproject_client"].initialize.return_value = True

        # Set up specific failure point
        mock_migration_components[failure_point].run.side_effect = MigrationError(
            f"{failure_point} failed",
        )

        # Act
        with pytest.raises(MigrationError):
            migration_instance.run_migration()

        # Assert: Verify cleanup cascade
        for cleanup_method in expected_cleanup_calls:
            getattr(migration_instance, cleanup_method).assert_called_once()

    def test_random_component_failures_with_recovery(
        self,
        migration_instance,
        mock_migration_components,
    ) -> None:
        """CHAOS TEST: Random failures across components with automatic retry.
        Simulates real-world intermittent failures and recovery mechanisms.
        """
        failure_components = ["jira_client", "openproject_client", "state_manager"]

        # Arrange: Random failures
        for component_name in failure_components:
            component = mock_migration_components[component_name]
            # 30% chance of failure on first call, success on retry
            component.connect = MagicMock(
                side_effect=[
                    (
                        ConnectionError("Random failure")
                        if random.random() < 0.3
                        else True
                    ),
                    True,  # Success on retry
                ],
            )

        # Enable retry mechanism
        migration_instance.retry_failed_components = True
        migration_instance.max_retries = 2

        # Act
        result = migration_instance.run_migration()

        # Assert: Migration should succeed after retries
        assert result.success is True
        assert result.retry_count > 0

    def test_state_corruption_recovery(
        self,
        migration_instance,
        mock_migration_components,
    ) -> None:
        """CHAOS TEST: State becomes corrupted during migration.
        Tests recovery from inconsistent state between migration records and snapshots.
        """
        # Arrange: State corruption during migration
        mock_migration_components["state_manager"].save_migration_state.side_effect = (
            StateCorruptionError("Migration record exists but snapshot is corrupted")
        )

        # Act & Assert
        with pytest.raises(MigrationError, match="State corruption detected"):
            migration_instance.run_migration()

        # Verify recovery procedures
        mock_migration_components[
            "state_manager"
        ].attempt_state_recovery.assert_called_once()
        mock_migration_components[
            "state_manager"
        ].create_emergency_backup.assert_called_once()

    def test_concurrent_migration_conflict(
        self,
        migration_instance,
        mock_migration_components,
    ) -> None:
        """CHAOS TEST: Multiple migration instances attempt to run simultaneously.
        Tests resource contention and conflict resolution.
        """
        # Arrange: Simulate another migration in progress
        mock_migration_components["state_manager"].check_migration_lock.return_value = (
            False
        )
        mock_migration_components[
            "state_manager"
        ].acquire_migration_lock.side_effect = TimeoutError(
            "Another migration is already running",
        )

        # Act & Assert
        with pytest.raises(MigrationError, match="Migration already in progress"):
            migration_instance.run_migration()

        # Verify no migration was attempted
        mock_migration_components["work_package_migration"].run.assert_not_called()

    def test_memory_pressure_scenario(
        self,
        migration_instance,
        mock_migration_components,
    ) -> None:
        """CHAOS TEST: System under memory pressure during large dataset migration.
        Tests graceful degradation and resource management.
        """
        # Arrange: Simulate memory pressure
        mock_migration_components["work_package_migration"].run.side_effect = (
            MemoryError("Cannot allocate memory for large dataset")
        )

        # Enable memory-efficient mode
        migration_instance.enable_memory_efficient_mode = True

        # Act & Assert
        with pytest.raises(MigrationError, match="Memory exhaustion during migration"):
            migration_instance.run_migration()

        # Verify memory-efficient fallback was attempted
        mock_migration_components[
            "work_package_migration"
        ].run_in_batches.assert_called()

    def test_network_partition_during_migration(
        self,
        migration_instance,
        mock_migration_components,
    ) -> None:
        """CHAOS TEST: Network partition occurs during SSH → Docker → Rails Console chain.
        Tests network failure resilience and reconnection logic.
        """
        # Arrange: Network partition during operation
        mock_migration_components["openproject_client"].execute_query.side_effect = [
            ConnectionError("Network unreachable"),  # First call fails
            ConnectionError("SSH connection lost"),  # Second call fails
            "Success",  # Third call succeeds after reconnection
        ]

        # Enable network resilience mode
        migration_instance.enable_network_resilience = True
        migration_instance.network_retry_count = 3

        # Act
        result = migration_instance.run_migration()

        # Assert: Should succeed after network recovery
        assert result.success is True
        assert result.network_failures_recovered > 0

    def test_resource_cleanup_on_interrupted_migration(
        self,
        migration_instance,
        mock_migration_components,
    ) -> None:
        """CHAOS TEST: Migration is interrupted (SIGINT/SIGTERM).
        Tests graceful shutdown and resource cleanup.
        """

        # Arrange: Simulate interruption during migration
        def simulate_interruption(*args, **kwargs) -> Never:
            msg = "Migration interrupted by user"
            raise KeyboardInterrupt(msg)

        mock_migration_components["work_package_migration"].run.side_effect = (
            simulate_interruption
        )

        # Act & Assert
        with pytest.raises(KeyboardInterrupt):
            migration_instance.run_migration()

        # Verify graceful cleanup
        mock_migration_components[
            "state_manager"
        ].save_interrupted_state.assert_called_once()
        migration_instance.cleanup_temp_files.assert_called_once()
        migration_instance.release_all_locks.assert_called_once()


class TestClientLayerChaosIntegration:
    """Chaos tests for the layered client architecture."""

    @pytest.fixture
    def layered_client_stack(self):
        """Mock the SSH → Docker → Rails Console client chain."""
        ssh_client = MagicMock()
        docker_client = MagicMock()
        rails_client = MagicMock()

        # Set up proper client relationships
        docker_client.ssh_client = ssh_client
        rails_client.docker_client = docker_client

        return {"ssh": ssh_client, "docker": docker_client, "rails": rails_client}

    def test_ssh_connection_drops_during_operation(self, layered_client_stack) -> None:
        """CHAOS TEST: SSH connection drops while Rails console is executing commands.
        Tests error propagation through the client stack.
        """
        # Arrange: SSH connection fails mid-operation
        layered_client_stack["ssh"].execute_command.side_effect = [
            ("partial output", "", 0),  # First part succeeds
            ConnectionError("SSH connection lost"),  # Connection drops
            ("remaining output", "", 0),  # Reconnection succeeds
        ]

        rails_client = layered_client_stack["rails"]
        rails_client.execute_with_retry = True

        # Act
        result = rails_client.execute_long_running_query("User.all.to_json")

        # Assert: Should recover and complete
        assert "partial output" in result
        assert "remaining output" in result
        assert layered_client_stack["ssh"].reconnect.called

    def test_docker_container_becomes_unresponsive(self, layered_client_stack) -> None:
        """CHAOS TEST: Docker container stops responding during file transfer.
        Tests timeout handling and container restart procedures.
        """
        # Arrange: Docker operations timeout
        layered_client_stack["docker"].transfer_file_to_container.side_effect = (
            TimeoutError("Container unresponsive after 30 seconds")
        )

        # Act & Assert
        with pytest.raises(OpenProjectError, match="Container became unresponsive"):
            layered_client_stack["docker"].transfer_file_to_container(
                "/tmp/local",
                "/tmp/remote",
            )

        # Verify container restart was attempted
        layered_client_stack["docker"].restart_container.assert_called_once()

    def test_rails_console_memory_exhaustion(self, layered_client_stack) -> None:
        """CHAOS TEST: Rails console runs out of memory during large query.
        Tests graceful handling of Ruby process crashes.
        """
        # Arrange: Rails console crashes with memory error
        layered_client_stack["rails"].execute.side_effect = Exception(
            "Ruby process killed (OOM): Cannot allocate memory",
        )

        # Act & Assert
        with pytest.raises(OpenProjectError, match="Rails console memory exhaustion"):
            layered_client_stack["rails"].execute(
                "WorkPackage.all.includes(:attachments).to_json",
            )

        # Verify console restart and memory-efficient retry
        layered_client_stack["rails"].restart_console.assert_called_once()
        layered_client_stack["rails"].execute_in_batches.assert_called()
