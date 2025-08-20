#!/usr/bin/env python3
"""Install .irbrc into the OpenProject Rails container.

This helper reads SSH/container settings from configuration (env/.env)
and transfers `contrib/openproject.irbrc` into `/app/.irbrc` inside the
configured OpenProject container on the configured OpenProject host.

Usage:
  - From host: make install-irbrc
  - Direct:     python scripts/install_irbrc.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

from src.clients.docker_client import DockerClient
from src.clients.ssh_client import SSHClient
from src.config import logger, openproject_config


def main() -> int:
    load_dotenv()

    server = openproject_config.get("server")
    user = openproject_config.get("user")
    container = openproject_config.get("container")

    if not server or not user or not container:
        logger.error(
            "Missing OpenProject remote settings. Ensure J2O_OPENPROJECT_SERVER, "
            "J2O_OPENPROJECT_USER, and J2O_OPENPROJECT_CONTAINER are set."
        )
        return 1

    repo_root = Path(__file__).resolve().parents[1]
    irbrc_src = repo_root / "contrib" / "openproject.irbrc"
    if not irbrc_src.exists():
        logger.error("IRBRC source file not found: %s", irbrc_src)
        return 1

    # Establish SSH, then Docker client
    ssh = SSHClient(
        host=str(server),
        user=str(user),
        operation_timeout=30,
        retry_count=3,
        retry_delay=0.5,
    )

    docker = DockerClient(
        container_name=str(container),
        ssh_client=ssh,
        command_timeout=30,
        retry_count=3,
        retry_delay=0.5,
    )

    # Transfer to container path
    container_path = Path("/app/.irbrc")
    try:
        docker.transfer_file_to_container(irbrc_src, container_path)
        # Ensure ownership/permissions are sane for app user
        docker.execute_command("chmod 644 /app/.irbrc", user="root")
        logger.info("Installed .irbrc to %s:%s", server, container_path)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to install .irbrc: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())


