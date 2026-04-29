"""Helper functions for working with Docker in tests."""

import subprocess
import time


def check_docker_available() -> bool:
    """Check if Docker is available and running.

    Returns:
        bool: True if Docker is available, False otherwise

    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def pull_docker_image(image_name: str) -> bool:
    """Pull a Docker image.

    Args:
        image_name: Name of the image to pull

    Returns:
        bool: True if successful, False otherwise

    """
    try:
        result = subprocess.run(
            ["docker", "pull", image_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def start_docker_container(
    image_name: str,
    container_name: str | None = None,
    ports: dict[str, str] | None = None,
    env_vars: dict[str, str] | None = None,
    volumes: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Start a Docker container.

    Args:
        image_name: Docker image to use
        container_name: Optional container name
        ports: Optional port mappings (host:container)
        env_vars: Optional environment variables
        volumes: Optional volume mappings (host:container)

    Returns:
        Tuple[bool, str]: Success status and container ID or error message

    """
    cmd = ["docker", "run", "-d"]

    # Add container name if specified
    if container_name:
        cmd.extend(["--name", container_name])

    # Add port mappings if specified
    if ports:
        for host_port, container_port in ports.items():
            cmd.extend(["-p", f"{host_port}:{container_port}"])

    # Add environment variables if specified
    if env_vars:
        for name, value in env_vars.items():
            cmd.extend(["-e", f"{name}={value}"])

    # Add volumes if specified
    if volumes:
        for host_path, container_path in volumes.items():
            cmd.extend(["-v", f"{host_path}:{container_path}"])

    # Add image name
    cmd.append(image_name)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()
    except Exception as e:
        return False, str(e)


def stop_docker_container(container_id_or_name: str) -> bool:
    """Stop and remove a Docker container.

    Args:
        container_id_or_name: Container ID or name

    Returns:
        bool: True if successful, False otherwise

    """
    try:
        # Stop the container
        stop_result = subprocess.run(
            ["docker", "stop", container_id_or_name],
            capture_output=True,
            text=True,
            check=False,
        )

        # Remove the container
        rm_result = subprocess.run(
            ["docker", "rm", container_id_or_name],
            capture_output=True,
            text=True,
            check=False,
        )

        return stop_result.returncode == 0 and rm_result.returncode == 0
    except Exception:
        return False


def start_jira_test_container(
    container_name: str = "jira_test",
    port: str = "8080",
) -> tuple[bool, str]:
    """Start a Jira test container.

    Args:
        container_name: Container name
        port: Host port for Jira

    Returns:
        Tuple[bool, str]: Success status and container ID or error message

    """
    image_name = "atlassian/jira-software:latest"

    ports = {
        port: "8080",
    }

    env_vars = {
        "JIRA_SETUP_MODE": "QUICK",
    }

    return start_docker_container(
        image_name=image_name,
        container_name=container_name,
        ports=ports,
        env_vars=env_vars,
    )


def start_openproject_test_container(
    container_name: str = "openproject_test",
    port: str = "8081",
) -> tuple[bool, str]:
    """Start an OpenProject test container.

    Args:
        container_name: Container name
        port: Host port for OpenProject

    Returns:
        Tuple[bool, str]: Success status and container ID or error message

    """
    image_name = "openproject/community:latest"

    ports = {
        port: "80",
    }

    env_vars = {
        "OPENPROJECT_TOKEN_SECRET": "test-secret",
        "OPENPROJECT_ADMIN_EMAIL": "admin@example.com",
        "OPENPROJECT_ADMIN_PASSWORD": "admin-password",
        "DATABASE_TYPE": "sqlite",
    }

    return start_docker_container(
        image_name=image_name,
        container_name=container_name,
        ports=ports,
        env_vars=env_vars,
    )


def wait_for_service_startup(
    url: str,
    max_attempts: int = 30,
    delay_seconds: int = 2,
) -> bool:
    """Wait for an HTTP service to be available.

    Args:
        url: URL to check
        max_attempts: Maximum number of attempts
        delay_seconds: Delay between attempts in seconds

    Returns:
        bool: True if service available, False if timed out

    """
    import requests

    for _ in range(max_attempts):
        try:
            response = requests.get(url, timeout=5)
            if response.status_code < 500:  # Accept any non-server error response
                return True
        except requests.RequestException:
            pass

        time.sleep(delay_seconds)

    return False
