import pytest

pytestmark = pytest.mark.integration


#!/usr/bin/env python3
"""Integration tests for Docker Compose security configuration.

This module tests that compose.yml properly configures all services with
non-root users and resource limits for security hardening.
"""

import unittest
from pathlib import Path

import yaml


class TestComposeSecurityConfiguration(unittest.TestCase):
    """Test cases for Docker Compose security configuration."""

    def setUp(self) -> None:
        """Set up test environment."""
        self.project_root = Path(__file__).parent.parent.parent
        self.compose_file = self.project_root / "compose.yml"

        # Ensure compose file exists
        if not self.compose_file.exists():
            self.skipTest("compose.yml not found")

    def test_compose_file_exists_and_valid(self) -> None:
        """Test that compose.yml exists and is valid YAML."""
        # Test file exists
        assert self.compose_file.exists(), "compose.yml file not found"

        # Test valid YAML
        with open(self.compose_file) as f:
            try:
                compose_config = yaml.safe_load(f)
            except yaml.YAMLError as e:
                self.fail(f"compose.yml is not valid YAML: {e}")

        # Test has services section
        assert "services" in compose_config, "compose.yml missing services section"
        assert len(compose_config["services"]) > 0, "No services defined in compose.yml"

    def test_all_services_have_user_mapping(self) -> None:
        """Test that services requiring custom user mapping have it configured."""
        with open(self.compose_file) as f:
            compose_config = yaml.safe_load(f)

        services = compose_config.get("services", {})

        # Services that should have custom user mapping (non-Alpine images)
        services_needing_custom_user = ["app", "mock-jira", "mock-openproject"]

        # Services that should NOT have custom user mapping (Alpine images with built-in users)
        services_with_builtin_users = ["redis", "postgres"]

        for service_name, service_config in services.items():
            with self.subTest(service=service_name):
                if service_name in services_needing_custom_user:
                    assert "user" in service_config, f"Service {service_name} missing user configuration"

                    user_config = service_config["user"]

                    # Should use environment variable format
                    assert "${DOCKER_UID:-1000}:${DOCKER_GID:-1000}" in user_config, (
                        f"Service {service_name} user config should use UID/GID environment variables"
                    )

                elif service_name in services_with_builtin_users:
                    # These services should NOT have custom user mapping - they use built-in non-root users
                    assert "user" not in service_config, (
                        f"Service {service_name} should not have custom user mapping (uses built-in non-root user)"
                    )

    def test_all_services_have_resource_limits(self) -> None:
        """Test that all services have resource limits configured."""
        with open(self.compose_file) as f:
            compose_config = yaml.safe_load(f)

        services = compose_config.get("services", {})

        for service_name, service_config in services.items():
            with self.subTest(service=service_name):
                assert "deploy" in service_config, f"Service {service_name} missing deploy configuration"

                deploy_config = service_config["deploy"]
                assert "resources" in deploy_config, f"Service {service_name} missing resources in deploy"

                resources = deploy_config["resources"]
                assert "limits" in resources, f"Service {service_name} missing resource limits"

                limits = resources["limits"]
                assert "cpus" in limits, f"Service {service_name} missing CPU limits"
                assert "memory" in limits, f"Service {service_name} missing memory limits"

    def test_service_specific_configurations(self) -> None:
        """Test service-specific security configurations."""
        with open(self.compose_file) as f:
            compose_config = yaml.safe_load(f)

        services = compose_config.get("services", {})

        # Test app service has appropriate limits for main application
        if "app" in services:
            app_limits = services["app"]["deploy"]["resources"]["limits"]
            # App service should have higher limits as it's the main application
            cpu_limit = app_limits["cpus"]
            memory_limit = app_limits["memory"]

            # Convert to comparable values
            cpu_val = float(cpu_limit)
            assert cpu_val >= 1.0, "App service should have at least 1 CPU core"

            # Memory should be at least 512MB
            if memory_limit.endswith("M"):
                memory_val = int(memory_limit[:-1])
                assert memory_val >= 512, "App service should have at least 512MB memory"
            elif memory_limit.endswith("G"):
                memory_val = float(memory_limit[:-1]) * 1024
                assert memory_val >= 512, "App service should have at least 512MB memory"

        # Test database service has appropriate limits
        if "postgres" in services:
            postgres_limits = services["postgres"]["deploy"]["resources"]["limits"]
            cpu_limit = postgres_limits["cpus"]
            memory_limit = postgres_limits["memory"]

            # Database should have substantial resources
            cpu_val = float(cpu_limit)
            assert cpu_val >= 0.5, "Postgres service should have at least 0.5 CPU"

            # Check memory is reasonable for database
            if memory_limit.endswith("G"):
                memory_val = float(memory_limit[:-1])
                assert memory_val >= 0.5, "Postgres service should have at least 512MB memory"

    def test_security_best_practices(self) -> None:
        """Test that security best practices are followed."""
        with open(self.compose_file) as f:
            compose_config = yaml.safe_load(f)

        services = compose_config.get("services", {})

        for service_name, service_config in services.items():
            with self.subTest(service=service_name):
                # Services should not expose unnecessary ports to host
                if "ports" in service_config:
                    # Only certain services should expose ports
                    allowed_port_services = ["mock-jira", "mock-openproject"]
                    if service_name not in allowed_port_services:
                        # App service may expose development ports, but others shouldn't
                        if service_name != "app":
                            self.fail(
                                f"Service {service_name} should not expose ports for security",
                            )

    def test_volume_security(self) -> None:
        """Test that volume mounts are configured securely."""
        with open(self.compose_file) as f:
            compose_config = yaml.safe_load(f)

        services = compose_config.get("services", {})

        for service_name, service_config in services.items():
            if "volumes" in service_config:
                volumes = service_config["volumes"]

                for volume in volumes:
                    with self.subTest(service=service_name, volume=volume):
                        # Check for different types of volume mounts
                        if ":" in volume:
                            source_path = volume.split(":")[0]

                            # Named volumes (managed by Docker) are secure for data persistence
                            # They don't expose host filesystem and are managed by Docker
                            safe_named_volumes = ["redis_data", "postgres_data"]

                            # Safe bind mounts for development
                            safe_bind_mounts = [".", "./api-specs"]

                            # Dangerous patterns to avoid
                            if "docker.sock" in volume:
                                self.fail(
                                    f"Service {service_name} has dangerous Docker socket mount: {volume}",
                                )

                            # Check if it's a named volume or safe bind mount
                            is_safe = source_path in safe_named_volumes or any(
                                source_path.startswith(safe) for safe in safe_bind_mounts
                            )

                            if not is_safe:
                                self.fail(
                                    f"Service {service_name} has potentially unsafe volume mount: {volume}",
                                )

    def test_env_example_has_docker_security_vars(self) -> None:
        """Test that .env.example includes Docker security variables."""
        env_example_file = self.project_root / ".env.example"

        if not env_example_file.exists():
            self.skipTest(".env.example not found")

        with open(env_example_file) as f:
            env_content = f.read()

        # Check for Docker security configuration section
        assert "DOCKER SECURITY SETTINGS" in env_content, ".env.example missing Docker security settings section"

        # Check for required variables
        assert "DOCKER_UID" in env_content, ".env.example missing DOCKER_UID"
        assert "DOCKER_GID" in env_content, ".env.example missing DOCKER_GID"

        # Check default values are secure (non-root)
        lines = env_content.split("\n")
        uid_line = next((line for line in lines if line.startswith("DOCKER_UID")), None)
        gid_line = next((line for line in lines if line.startswith("DOCKER_GID")), None)

        if uid_line:
            assert "1000" in uid_line, "DOCKER_UID should default to 1000 (non-root)"
        if gid_line:
            assert "1000" in gid_line, "DOCKER_GID should default to 1000 (non-root)"


class TestDockerfileSecurityConfiguration(unittest.TestCase):
    """Test cases for Dockerfile security configuration."""

    def setUp(self) -> None:
        """Set up test environment."""
        self.project_root = Path(__file__).parent.parent.parent
        self.dockerfile = self.project_root / "Dockerfile"

        if not self.dockerfile.exists():
            self.skipTest("Dockerfile not found")

    def test_dockerfile_uses_non_root_user(self) -> None:
        """Test that Dockerfile creates and uses non-root user."""
        with open(self.dockerfile) as f:
            dockerfile_content = f.read()

        # Check for user creation
        assert "useradd" in dockerfile_content, "Dockerfile should create a non-root user"
        assert "appuser" in dockerfile_content, "Dockerfile should create 'appuser'"

        # Check for USER directive
        assert "USER appuser" in dockerfile_content, "Dockerfile should switch to non-root user"

        # Check UID is 1000 (non-root)
        assert "-u 1000" in dockerfile_content, "User should be created with UID 1000"

    def test_dockerfile_proper_file_ownership(self) -> None:
        """Test that Dockerfile sets proper file ownership."""
        with open(self.dockerfile) as f:
            dockerfile_content = f.read()

        # Check for chown operations
        assert "chown" in dockerfile_content, "Dockerfile should set proper file ownership"
        assert "appuser:appuser" in dockerfile_content, "Files should be owned by appuser"

    def test_dockerfile_security_best_practices(self) -> None:
        """Test that Dockerfile follows security best practices."""
        with open(self.dockerfile) as f:
            dockerfile_content = f.read()

        lines = dockerfile_content.split("\n")

        # Should not run as root by the end
        user_lines = [line for line in lines if line.startswith("USER ")]
        if user_lines:
            last_user = user_lines[-1]
            assert "root" not in last_user, "Dockerfile should not end with root user"


if __name__ == "__main__":
    unittest.main()
