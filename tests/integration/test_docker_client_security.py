#!/usr/bin/env python3
"""Test module for DockerClient security hardening features.

This module contains test cases for validating Docker security hardening
features including non-root users and resource limits.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import pytest

from src.clients.docker_client import DockerClient


class TestDockerClientHardening(unittest.TestCase):
    """Test cases for the security-related features of DockerClient."""

    def setUp(self) -> None:
        """Set up the test environment."""
        self.ssh_client_patcher = patch("src.clients.docker_client.SSHClient")
        self.mock_ssh_client_class = self.ssh_client_patcher.start()
        self.mock_ssh_client = MagicMock()
        self.mock_ssh_client_class.return_value = self.mock_ssh_client

        self.logger_patcher = patch("src.clients.docker_client.logger")
        self.mock_logger = self.logger_patcher.start()

        # Mock container existence check to pass during initialization
        self.mock_ssh_client.execute_command.return_value = ("test_container\n", "", 0)
        self.docker_client = DockerClient(
            container_name="test_container", ssh_client=self.mock_ssh_client
        )
        # Reset mock after initialization
        self.mock_ssh_client.execute_command.reset_mock()

    def tearDown(self) -> None:
        """Clean up after each test."""
        self.ssh_client_patcher.stop()
        self.logger_patcher.stop()

    def test_run_container_basic_functionality(self) -> None:
        """Test run_container with minimal parameters."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("container_id", "", 0)

        # Act
        stdout, stderr, returncode = self.docker_client.run_container(
            image="test-image:latest"
        )

        # Assert
        assert stdout == "container_id"
        assert stderr == ""
        assert returncode == 0
        
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_str = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "docker run" in cmd_str
        assert "test-image:latest" in cmd_str

    def test_run_container_with_user_parameter(self) -> None:
        """Test run_container correctly adds user parameter."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("container_id", "", 0)

        # Act
        self.docker_client.run_container(
            image="test-image:latest",
            user="1000:1000"
        )

        # Assert
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_str = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "--user 1000:1000" in cmd_str

    def test_run_container_with_resource_limits(self) -> None:
        """Test run_container correctly adds CPU and memory limits."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("container_id", "", 0)

        # Act
        self.docker_client.run_container(
            image="test-image:latest",
            cpu_limit="0.5",
            memory_limit="512M"
        )

        # Assert
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_str = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "--cpus 0.5" in cmd_str
        assert "--memory 512M" in cmd_str

    def test_run_container_with_all_security_options(self) -> None:
        """Test run_container correctly builds a command with all security parameters."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("container_id", "", 0)

        # Act
        self.docker_client.run_container(
            image="test-image:latest",
            name="secure-container",
            user="1001:1001",
            cpu_limit="0.5",
            memory_limit="512m",
            environment={"DEBUG": "true"},
            volumes={"/host/path": "/container/path"},
            ports={"8080": "80"},
            network="secure-network",
            remove=True,
        )

        # Assert
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_str = self.mock_ssh_client.execute_command.call_args[0][0]

        assert "docker run" in cmd_str
        assert "--user 1001:1001" in cmd_str
        assert "--cpus 0.5" in cmd_str
        assert "--memory 512m" in cmd_str
        assert "--name secure-container" in cmd_str
        assert "--rm" in cmd_str
        assert "-e DEBUG=true" in cmd_str
        assert "-v /host/path:/container/path" in cmd_str
        assert "-p 8080:80" in cmd_str
        assert "--network secure-network" in cmd_str
        assert "test-image:latest" in cmd_str

    def test_run_container_with_command(self) -> None:
        """Test run_container correctly appends command at the end."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("container_id", "", 0)

        # Act
        self.docker_client.run_container(
            image="test-image:latest",
            command="python app.py"
        )

        # Assert
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_str = self.mock_ssh_client.execute_command.call_args[0][0]
        assert cmd_str.endswith("test-image:latest python app.py")

    def test_run_container_detach_false(self) -> None:
        """Test run_container without detach flag."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("output", "", 0)

        # Act
        self.docker_client.run_container(
            image="test-image:latest",
            detach=False
        )

        # Assert
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_str = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "-d" not in cmd_str

    def test_run_container_execution_failure(self) -> None:
        """Test run_container handles execution failures correctly."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("", "Error: no such image", 1)

        # Act
        stdout, stderr, returncode = self.docker_client.run_container(
            image="nonexistent-image:latest"
        )

        # Assert
        assert stdout == ""
        assert stderr == "Error: no such image"
        assert returncode == 1

    def test_get_container_info_success(self) -> None:
        """Test successful parsing of docker inspect output."""
        # Arrange
        inspect_output = json.dumps([{
            "Config": {
                "User": "1000:1000",
                "Image": "my-image:v1"
            },
            "HostConfig": {
                "Memory": 1073741824,  # 1GB
                "CpuQuota": 50000,
                "CpuPeriod": 100000,
                "SecurityOpt": ["no-new-privileges"],
                "ReadonlyRootfs": True
            }
        }])
        self.mock_ssh_client.execute_command.return_value = (inspect_output, "", 0)

        # Act
        info = self.docker_client.get_container_info()

        # Assert
        self.mock_ssh_client.execute_command.assert_called_once_with("docker inspect test_container")
        assert info["user"] == "1000:1000"
        assert info["image"] == "my-image:v1"
        assert info["memory"] == "1073741824"
        assert info["cpu_quota"] == "50000"
        assert info["cpu_period"] == "100000"
        assert info["security_opt"] == ["no-new-privileges"]
        assert info["readonly_rootfs"] == "True"

    def test_get_container_info_missing_data_returns_defaults(self) -> None:
        """Test that missing keys in inspect output result in default values."""
        # Arrange
        inspect_output = json.dumps([{
            "Config": {
                "Image": "my-image:v1"
                # User is missing
            },
            "HostConfig": {
                # All resource limits are missing
            }
        }])
        self.mock_ssh_client.execute_command.return_value = (inspect_output, "", 0)

        # Act
        info = self.docker_client.get_container_info()

        # Assert
        assert info["user"] == "root"  # Should default to root
        assert info["memory"] == "0"
        assert info["cpu_quota"] == "0"
        assert info["security_opt"] == []
        assert info["readonly_rootfs"] == "False"

    def test_get_container_info_inspect_fails(self) -> None:
        """Test that a failed docker inspect command raises a ValueError."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("", "Error: no such container", 1)

        # Act & Assert
        with pytest.raises(ValueError, match="Failed to inspect container"):
            self.docker_client.get_container_info()

    def test_get_container_info_invalid_json(self) -> None:
        """Test that invalid JSON output from docker inspect raises a ValueError."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("not valid json", "", 0)

        # Act & Assert
        with pytest.raises(ValueError, match="Failed to get container info"):
            self.docker_client.get_container_info()

    def test_get_container_info_empty_inspect_data(self) -> None:
        """Test that empty list from docker inspect raises a ValueError."""
        # Arrange
        self.mock_ssh_client.execute_command.return_value = ("[]", "", 0)

        # Act & Assert
        with pytest.raises(ValueError, match="No data returned for container"):
            self.docker_client.get_container_info()


class TestDockerClientSecurityValidation(unittest.TestCase):
    """Integration-style tests for Docker security validation."""

    def setUp(self) -> None:
        """Set up the test environment."""
        self.ssh_client_patcher = patch("src.clients.docker_client.SSHClient")
        self.mock_ssh_client_class = self.ssh_client_patcher.start()
        self.mock_ssh_client = MagicMock()
        self.mock_ssh_client_class.return_value = self.mock_ssh_client

        self.logger_patcher = patch("src.clients.docker_client.logger")
        self.mock_logger = self.logger_patcher.start()

        # Mock container existence check to pass during initialization
        self.mock_ssh_client.execute_command.return_value = ("test_container\n", "", 0)
        self.docker_client = DockerClient(
            container_name="test_container", ssh_client=self.mock_ssh_client
        )
        # Reset mock after initialization
        self.mock_ssh_client.execute_command.reset_mock()

    def tearDown(self) -> None:
        """Clean up after each test."""
        self.ssh_client_patcher.stop()
        self.logger_patcher.stop()

    def test_container_runs_as_non_root_user(self) -> None:
        """Test that container info shows non-root user execution."""
        # Arrange
        inspect_output = json.dumps([{
            "Config": {"User": "1000:1000"},
            "HostConfig": {}
        }])
        self.mock_ssh_client.execute_command.return_value = (inspect_output, "", 0)

        # Act
        info = self.docker_client.get_container_info()

        # Assert
        assert info["user"] != "root"
        assert info["user"] != ""
        assert "1000" in info["user"]

    def test_container_has_resource_limits(self) -> None:
        """Test that container info shows resource limits are enforced."""
        # Arrange
        inspect_output = json.dumps([{
            "Config": {},
            "HostConfig": {
                "Memory": 536870912,  # 512MB
                "CpuQuota": 50000,    # 0.5 CPU
                "CpuPeriod": 100000
            }
        }])
        self.mock_ssh_client.execute_command.return_value = (inspect_output, "", 0)

        # Act
        info = self.docker_client.get_container_info()

        # Assert
        assert int(info["memory"]) > 0, "Memory limit should be set"
        assert int(info["cpu_quota"]) > 0, "CPU quota should be set"
        assert int(info["cpu_period"]) > 0, "CPU period should be set"

    def test_container_has_security_options(self) -> None:
        """Test that container has proper security options configured."""
        # Arrange
        inspect_output = json.dumps([{
            "Config": {},
            "HostConfig": {
                "SecurityOpt": ["no-new-privileges"],
                "ReadonlyRootfs": True
            }
        }])
        self.mock_ssh_client.execute_command.return_value = (inspect_output, "", 0)

        # Act
        info = self.docker_client.get_container_info()

        # Assert
        assert "no-new-privileges" in info["security_opt"]
        assert info["readonly_rootfs"] == "True"


if __name__ == "__main__":
    unittest.main() 