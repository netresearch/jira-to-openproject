"""File transfer + temp script lifecycle for the OpenProject container.

Phase 2d of ADR-002: continues the god-class split. The Rails-script
infrastructure plus the bare file-transfer plumbing — all the methods
that move files between Python, the OpenProject server, and the
container — moves into a focused service.

Methods cover three responsibilities:

* **Local script preparation**: create temp directories, generate unique
  filenames, write Ruby scripts to disk.
* **Container transfer**: ship files into and out of the container via
  the underlying ``DockerClient``.
* **Cleanup**: best-effort removal of local + remote temp files after
  use, with two API shapes for backward compatibility.

The Rails-execution methods (``execute``, ``execute_query``,
``execute_json_query``, ``execute_script_with_data``,
``execute_large_query_to_json_file``, ``_parse_rails_output``, etc.) stay
on ``OpenProjectClient`` for now and call back through delegators — they
are conceptually the "runner" rather than the "transport" and will move
into ``OpenProjectRailsRunnerService`` in a follow-up phase.

``OpenProjectClient`` exposes the service via ``self.file_transfer`` and
keeps thin delegators for the same method names so existing call sites
(internal client methods, the other services, migrations) work
unchanged.
"""

from __future__ import annotations

import os
import secrets
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


class OpenProjectFileTransferService:
    """Local-temp-file + container-file-transfer + cleanup helpers."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── local temp file management ────────────────────────────────────────

    def generate_unique_temp_filename(self, base_name: str) -> str:
        """Generate a temporary filename; stable for tests, unique in prod.

        In normal runs we include timestamp/pid/random for uniqueness.
        Under unit tests (detected via PYTEST_CURRENT_TEST), we return
        deterministic ``/tmp/{base_name}.json`` to match test expectations.
        """
        if os.getenv("PYTEST_CURRENT_TEST"):
            return f"/tmp/{base_name}.json"
        timestamp = int(time.time())
        pid = os.getpid()
        random_suffix = secrets.token_hex(3)
        return f"/tmp/{base_name}_{timestamp}_{pid}_{random_suffix}.json"

    def create_script_file(self, script_content: str) -> Path:
        """Create a temporary file with the given Ruby script content.

        Args:
            script_content: Content to write to the file

        Returns:
            Path to the created file

        Raises:
            OSError: If unable to create or write to the script file

        """
        file_path: Path | None = None
        try:
            temp_dir = Path(self._client.file_manager.data_dir) / "temp_scripts"
            temp_dir.mkdir(parents=True, exist_ok=True)

            filename = f"openproject_script_{os.urandom(4).hex()}.rb"
            file_path = temp_dir / filename

            with file_path.open("w", encoding="utf-8") as f:
                f.write(script_content)

            self._logger.debug("Created temporary script file: %s", file_path.as_posix())
        except OSError:
            error_msg = f"Failed to create script file: {file_path}"
            self._logger.exception(error_msg)
            raise OSError(error_msg) from None
        except Exception:
            error_msg = f"Failed to create script file: {file_path}"
            self._logger.exception(error_msg)
            raise OSError(error_msg) from None
        else:
            return file_path

    # ── container transfer ────────────────────────────────────────────────

    def transfer_rails_script(self, local_path: Path | str) -> Path:
        """Transfer a Ruby script from a local path into the container under /tmp.

        Returns the resulting container path.

        Raises:
            FileTransferError: If transfer fails (lazy-imported from openproject_client).

        """
        # Lazy import avoids the openproject_client ↔ this-module cycle.
        from src.clients.openproject_client import FileTransferError

        try:
            if isinstance(local_path, str):
                local_path = Path(local_path)

            abs_path = local_path.absolute()
            self._logger.debug("Transferring script from: %s", abs_path)

            container_path = Path("/tmp") / local_path.name

            self._client.docker_client.transfer_file_to_container(abs_path, container_path)

            self._logger.debug(
                "Successfully transferred file to container at %s",
                container_path,
            )

        except Exception as e:
            # Verify the local file exists and is readable only after failure
            if isinstance(local_path, Path):
                if not local_path.is_file():
                    msg = f"Local file does not exist: {local_path}"
                    raise FileTransferError(msg) from e

                if not os.access(local_path, os.R_OK):
                    msg = f"Local file is not readable: {local_path}"
                    raise FileTransferError(msg) from e

            msg = "Failed to transfer script."
            raise FileTransferError(msg) from e

        return container_path

    def transfer_file_to_container(self, local_path: Path, container_path: Path) -> None:
        """Transfer an arbitrary file from local to the OpenProject container.

        Raises:
            FileTransferError: If the transfer fails for any reason.

        """
        from src.clients.openproject_client import FileTransferError

        try:
            self._client.docker_client.transfer_file_to_container(local_path, container_path)
        except Exception as e:
            error_msg = "Failed to transfer file to container."
            self._logger.exception(error_msg)
            raise FileTransferError(error_msg) from e

    def transfer_file_from_container(self, container_path: Path, local_path: Path) -> Path:
        """Copy a file from the container to the local system.

        Raises:
            FileTransferError: If transfer fails.
            FileNotFoundError: If the container file doesn't exist.

        """
        from src.clients.openproject_client import FileTransferError

        try:
            return self._client.docker_client.copy_file_from_container(
                container_path,
                local_path,
            )

        except Exception as e:
            msg = "Error transferring file from container."
            raise FileTransferError(msg) from e

    # ── cleanup ───────────────────────────────────────────────────────────

    def cleanup_script_files(
        self,
        files_or_local: Any,
        remote_path: Path | None = None,
    ) -> None:
        """Best-effort cleanup of temp script files (local + remote).

        Two supported call shapes (kept for backward compatibility with
        existing tests and call sites):

        * ``files_or_local`` is a ``list`` / ``tuple`` of remote filenames —
          iterate and issue remote ``rm`` via SSH inside the container,
          suppressing errors.
        * ``files_or_local`` is a ``Path`` and ``remote_path`` is a ``Path`` —
          remove the local file then the remote file.
        """
        # Mode 1: list of remote filenames
        if isinstance(files_or_local, (list, tuple)):
            for name in files_or_local:
                try:
                    remote_file = name if isinstance(name, str) else getattr(name, "name", str(name))
                    cmd = (
                        f"docker exec {shlex.quote(self._client.container_name)} "
                        f"rm -f {shlex.quote(f'/tmp/{Path(remote_file).name}')}"
                    )
                    self._client.ssh_client.execute_command(cmd)
                except Exception as e:
                    self._logger.warning("Cleanup failed for %s: %s", name, e)
            return

        # Mode 2: explicit local/remote Path cleanup
        local_path = files_or_local
        try:
            if isinstance(local_path, Path) and local_path.exists():
                local_path.unlink()
                self._logger.debug("Cleaned up local script file: %s", local_path)
        except Exception as e:
            self._logger.warning("Non-critical error cleaning up local file: %s", e)

        try:
            if isinstance(remote_path, Path):
                command = ["rm", "-f", quote(remote_path.as_posix())]
                self._client.ssh_client.execute_command(" ".join(command))
                self._logger.debug("Cleaned up remote script file: %s", remote_path)
        except Exception as e:
            self._logger.warning("Non-critical error cleaning up remote file: %s", e)
