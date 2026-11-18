# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Private temporary storage shared by a migration transfer and its Rails consumer."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from shlex import quote
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from src.infrastructure.openproject.docker_client import DockerClient


@contextmanager
def private_container_directory(docker_client: DockerClient) -> Iterator[Path]:
    """Allocate a 0700 directory as the same container user that runs Rails."""
    output, _, status = docker_client.execute_command("env -u TMPDIR mktemp -d -t j2o-stage.XXXXXXXXXX")
    name = output.strip()
    path = PurePosixPath(name)
    if (
        status
        or not path.is_absolute()
        or ".." in path.parts
        or not re.fullmatch(r"j2o-stage\.[a-zA-Z0-9]{10}", path.name)
    ):
        msg = "Could not allocate a private OpenProject staging directory"
        raise OSError(msg)
    try:
        yield Path(name)
    finally:
        try:
            _, _, status = docker_client.execute_command(f"rm -rf -- {quote(name)}")
            if status:
                msg = "Could not remove OpenProject staging directory"
                raise OSError(msg)
        except Exception:
            logging.getLogger(__name__).exception("Failed to clean private OpenProject staging directory %s", path)
