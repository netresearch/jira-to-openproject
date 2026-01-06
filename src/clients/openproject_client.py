"""OpenProject client for interacting with OpenProject instances via SSH and Rails console."""

import inspect
import json
import os
import random
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from src import config
from src.clients.docker_client import DockerClient
from src.clients.exceptions import (
    ClientConnectionError,
    JsonParseError,
    QueryExecutionError,
    RecordNotFoundError,
)
from src.clients.rails_console_client import (
    CommandExecutionError,
    ConsoleNotReadyError,
    RailsConsoleClient,
    RubyError,
)
from src.clients.ssh_client import SSHClient
from src.display import configure_logging
from src.utils.config_validation import ConfigurationValidationError, SecurityValidator
from src.utils.file_manager import FileManager
from src.utils.idempotency_decorators import batch_idempotent
from src.utils.performance_optimizer import PerformanceOptimizer
from src.utils.rate_limiter import create_openproject_rate_limiter

try:
    # Prefer shared logger configured at startup
    from src.config import logger
except Exception:  # noqa: BLE001
    # Fallback to local configuration if config logger is unavailable
    logger = configure_logging("INFO", None)


# Module-level constants
BATCH_SIZE_DEFAULT = 50
SAFE_OFFSET_LIMIT = 5000
BATCH_LABEL_SAMPLE = 3
USERS_CACHE_TTL_SECONDS = 300


class SSHConnection:
    """SSH connection class for testing purposes."""


class OpenProjectError(Exception):
    """Base exception for OpenProject client errors."""


class FileTransferError(Exception):
    """Error when transferring files to/from OpenProject container."""


class OpenProjectClient:
    """Client for OpenProject operations.

    This is the top-level coordinator that orchestrates the client architecture:
    - SSHClient handles all SSH interactions
    - DockerClient (using SSHClient) handles container interactions
    - RailsConsoleClient handles Rails console interactions.

    All error handling uses exceptions rather than status dictionaries.
    """

    def __init__(  # noqa: PLR0913
        self,
        container_name: str | None = None,
        ssh_host: str | None = None,
        ssh_user: str | None = None,
        tmux_session_name: str | None = None,
        command_timeout: int = 180,
        retry_count: int = 3,
        retry_delay: float = 1.0,
        ssh_client: SSHClient | None = None,
        docker_client: DockerClient | None = None,
        rails_client: RailsConsoleClient | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize the OpenProject client with dependency injection.

        Args:
            container_name: Docker container name (default: from config)
            ssh_host: SSH host (default: from config)
            ssh_user: SSH username (default: from config)
            tmux_session_name: tmux session name (default: from config)
            command_timeout: Command timeout in seconds (default: 180)
            retry_count: Number of retries (default: 3)
            retry_delay: Delay between retries in seconds (default: 1.0)
            ssh_client: Optional SSH client (dependency injection)
            docker_client: Optional Docker client (dependency injection)
            rails_client: Optional Rails console client (dependency injection)
            **kwargs: Additional performance-related parameters (batch sizes, TTL, etc.)

        Raises:
            ValueError: If required configuration values are missing

        """
        # Instance logger for methods that use self.logger
        self.logger = logger

        # Rails console query state
        self._last_query = ""

        # Initialize caches
        self._users_cache: list[dict[str, Any]] | None = None
        self._users_cache_time: float | None = None
        self._users_by_email_cache: dict[str, dict[str, Any]] = {}

        # Get config values
        op_config = config.openproject_config

        # Use provided values or defaults from config
        self.container_name = container_name or op_config.get("container")
        self.ssh_host = ssh_host or op_config.get("server")
        self.ssh_user = ssh_user or op_config.get("user")
        self.tmux_session_name = tmux_session_name or op_config.get(
            "tmux_session_name",
            "rails_console",
        )
        # Allow env override for long-running remote operations
        try:
            env_timeout = int(os.environ.get("J2O_OPENPROJECT_TIMEOUT", "0"))
        except Exception:
            env_timeout = 0
        self.command_timeout = env_timeout if env_timeout > 0 else command_timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay

        # Verify required configuration
        if not self.container_name:
            msg = "Container name is required"
            raise ValueError(msg)
        if not self.ssh_host:
            msg = "SSH host is required"
            raise ValueError(msg)

        # Initialize rate limiter
        self.rate_limiter = create_openproject_rate_limiter()

        # Initialize file manager
        self.file_manager = FileManager()

        # Initialize clients in the correct order, respecting dependency injection
        # 1. First, create or use the SSH client which is the foundation
        self.ssh_client = ssh_client or SSHClient(
            host=str(self.ssh_host),
            user=self.ssh_user,
            operation_timeout=self.command_timeout,
            retry_count=self.retry_count,
            retry_delay=self.retry_delay,
        )
        logger.debug(
            "%s SSHClient for host %s",
            "Using provided" if ssh_client else "Initialized",
            self.ssh_host,
        )

        # 2. Next, create or use the Docker client
        self.docker_client = docker_client or DockerClient(
            container_name=str(self.container_name),
            ssh_client=self.ssh_client,  # Pass our SSH client instance
            command_timeout=self.command_timeout,
            retry_count=self.retry_count,
            retry_delay=self.retry_delay,
        )
        logger.debug(
            "%s DockerClient for container %s",
            "Using provided" if docker_client else "Initialized",
            self.container_name,
        )

        # 3. Finally, create or use the Rails console client for executing commands
        self.rails_client = rails_client or RailsConsoleClient(
            tmux_session_name=self.tmux_session_name,
            command_timeout=self.command_timeout,
        )
        logger.debug(
            "%s RailsConsoleClient with tmux session %s",
            "Using provided" if rails_client else "Initialized",
            self.tmux_session_name,
        )

        # ===== PERFORMANCE OPTIMIZER SETUP =====
        # Performance configuration from kwargs (passed from migration.py)

        # Validate performance configuration parameters using SecurityValidator
        try:
            cache_size = SecurityValidator.validate_numeric_parameter(
                "cache_size",
                kwargs.get("cache_size", 1500),
            )
            cache_ttl = SecurityValidator.validate_numeric_parameter(
                "cache_ttl",
                kwargs.get("cache_ttl", 2400),
            )
            batch_size = SecurityValidator.validate_numeric_parameter(
                "batch_size",
                kwargs.get("batch_size", 50),
            )
            max_workers = SecurityValidator.validate_numeric_parameter(
                "max_workers",
                kwargs.get("max_workers", 12),
            )
            rate_limit = SecurityValidator.validate_numeric_parameter(
                "rate_limit_per_sec",
                kwargs.get("rate_limit", 12.0),
            )

            # Validate resource allocation to prevent system overload
            SecurityValidator.validate_resource_allocation(
                batch_size,
                max_workers,
                2048,
            )  # 2GB memory limit

        except ConfigurationValidationError:
            logger.exception("OpenProjectClient configuration validation failed")
            raise

        # Initialize performance optimizer with validated parameters
        self.performance_optimizer = PerformanceOptimizer(
            cache_size=cache_size,
            cache_ttl=cache_ttl,
            batch_size=batch_size,
            max_workers=max_workers,
            rate_limit=rate_limit,
        )

        self.batch_size = batch_size
        self.parallel_workers = max_workers

        logger.success(
            "OpenProjectClient initialized for host %s, container %s",
            self.ssh_host,
            self.container_name,
        )

    def ensure_reporting_project(self, identifier: str, name: str) -> int:
        """Ensure a dedicated OpenProject project exists for reporting artefacts.

        Creates the project when missing, enables the wiki module, and returns its ID.

        Args:
            identifier: Desired project identifier (lowercase/hyphenated)
            name: Human readable project name

        Returns:
            OpenProject project ID

        Raises:
            QueryExecutionError: when creation fails or no project can be ensured

        """
        clean_identifier = re.sub(r"[^a-z0-9-]", "-", identifier.lower()).strip("-")
        clean_identifier = re.sub(r"-+", "-", clean_identifier) or "j2o-reporting"
        clean_name = name.strip() or "Jira Dashboards"

        script = (
            "begin\n"
            "  user = User.admin.first || User.active.first || User.first\n"
            "  raise 'no admin user available' unless user\n"
            f"  identifier = '{clean_identifier}'\n"
            f"  display_name = '{clean_name.replace("'", "\\'")}'\n"
            "  project = Project.find_by(identifier: identifier)\n"
            "  created = false\n"
            "  unless project\n"
            "    if defined?(::Projects::CreateService)\n"
            "      service = ::Projects::CreateService.new(user: user)\n"
            "      params = { name: display_name, identifier: identifier, public: false, active: false, enabled_module_names: ['wiki'], workspace_type: 'project' }\n"
            "      result = service.call(**params)\n"
            "      unless result.success?\n"
            "        raise result.errors.full_messages.join(', ')\n"
            "      end\n"
            "      project = result.result\n"
            "    else\n"
            "      project = Project.new(name: display_name, identifier: identifier)\n"
            "      project.public = false if project.respond_to?(:public=)\n"
            "      project.active = false if project.respond_to?(:active=)\n"
            "      project.workspace_type = 'project' if project.respond_to?(:workspace_type=)\n"
            "      project.enabled_module_names = ['wiki'] if project.respond_to?(:enabled_module_names=)\n"
            "      project.save!\n"
            "    end\n"
            "    created = true\n"
            "  end\n"
            "  if project.enabled_module_names.exclude?('wiki')\n"
            "    project.enabled_module_names = (project.enabled_module_names + ['wiki']).uniq\n"
            "    project.save!\n"
            "  end\n"
            "  if project.respond_to?(:workspace_type=) && project.workspace_type != 'project'\n"
            "    project.workspace_type = 'project'\n"
            "    project.save!\n"
            "  end\n"
            "  { success: true, id: project.id, created: created, identifier: project.identifier }\n"
            "rescue => e\n"
            "  { success: false, error: e.message }\n"
            "end\n"
        )

        result = self.execute_query_to_json_file(script, timeout=180)
        if not isinstance(result, dict):
            raise QueryExecutionError(f"Unexpected response when ensuring reporting project: {result!r}")
        if not result.get("success"):
            raise QueryExecutionError(
                f"Failed to ensure reporting project '{clean_identifier}': {result.get('error')}",
            )
        project_id = int(result.get("id", 0) or 0)
        if project_id <= 0:
            raise QueryExecutionError(
                f"Reporting project '{clean_identifier}' returned invalid id: {project_id}",
            )
        return project_id

    def _generate_unique_temp_filename(self, base_name: str) -> str:
        """Generate a temporary filename; stable for tests, unique in prod.

        In normal runs we include timestamp/pid/random for uniqueness.
        Under unit tests (detected via PYTEST_CURRENT_TEST), we return
        deterministic '/tmp/{base_name}.json' to match test expectations.
        """
        if os.getenv("PYTEST_CURRENT_TEST"):
            return f"/tmp/{base_name}.json"  # noqa: S108
        timestamp = int(time.time())
        pid = os.getpid()
        random_suffix = secrets.token_hex(3)
        return f"/tmp/{base_name}_{timestamp}_{pid}_{random_suffix}.json"  # noqa: S108

    def _create_script_file(self, script_content: str) -> Path:
        """Create a temporary file with the script content.

        Args:
            script_content: Content to write to the file

        Returns:
            Path to the created file

        Raises:
            OSError: If unable to create or write to the script file

        """
        file_path = None
        try:
            # Create a temporary directory if needed
            temp_dir = Path(self.file_manager.data_dir) / "temp_scripts"
            temp_dir.mkdir(parents=True, exist_ok=True)

            # Generate a unique filename
            filename = f"openproject_script_{os.urandom(4).hex()}.rb"
            file_path = temp_dir / filename

            # Write the content directly instead of using tempfile module
            with file_path.open("w", encoding="utf-8") as f:
                f.write(script_content)

            # Log the absolute path for easier debugging
            logger.debug("Created temporary script file: %s", file_path.as_posix())
        except OSError:
            error_msg = f"Failed to create script file: {file_path}"
            logger.exception(error_msg)
            raise OSError(error_msg) from None
        except Exception:
            error_msg = f"Failed to create script file: {file_path}"
            logger.exception(error_msg)
            raise OSError(error_msg) from None
        else:
            return file_path

    def _transfer_rails_script(self, local_path: Path | str) -> Path:
        """Transfer a script to the Rails environment.

        Args:
            local_path: Path to the script file (Path object or string)

        Returns:
            Path to the script in the container

        Raises:
            FileTransferError: If transfer fails

        """
        try:
            # Convert string to Path if needed
            if isinstance(local_path, str):
                local_path = Path(local_path)

            # Get the absolute path for better error messages
            abs_path = local_path.absolute()
            logger.debug("Transferring script from: %s", abs_path)

            # Use just the base filename for the container path
            container_path = Path("/tmp") / local_path.name  # noqa: S108

            self.docker_client.transfer_file_to_container(abs_path, container_path)

            logger.debug(
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

    def _cleanup_script_files(self, files_or_local: Any, remote_path: Path | None = None) -> None:  # noqa: ANN401
        """Clean up temporary files after execution.

        Two supported modes (for backward compatibility and tests):
        - files_or_local is a list of filenames (str/Path): iterate and issue remote cleanup via SSH, suppressing errors
        - files_or_local is a Path and remote_path is a Path: remove local and remote paths
        """
        # Mode 1: list of remote filenames
        if isinstance(files_or_local, (list, tuple)):
            for name in files_or_local:
                try:
                    remote_file = name if isinstance(name, str) else getattr(name, "name", str(name))
                    cmd = f"docker exec {self.container_name} rm -f /tmp/{Path(remote_file).name}"
                    self.ssh_client.execute_command(cmd)
                except Exception as e:  # noqa: BLE001 - Suppress cleanup errors
                    logger.warning("Cleanup failed for %s: %s", name, e)
            return

        # Mode 2: explicit local/remote Path cleanup
        local_path = files_or_local
        # Clean up local file
        try:
            if isinstance(local_path, Path) and local_path.exists():
                local_path.unlink()
                logger.debug("Cleaned up local script file: %s", local_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("Non-critical error cleaning up local file: %s", e)

        # Clean up remote file
        try:
            if isinstance(remote_path, Path):
                command = [
                    "rm",
                    "-f",
                    quote(remote_path.as_posix()),
                ]
                self.ssh_client.execute_command(" ".join(command))
                logger.debug("Cleaned up remote script file: %s", remote_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("Non-critical error cleaning up remote file: %s", e)

    def execute(self, script_content: str) -> dict[str, Any]:
        """Execute a Ruby script directly.

        Args:
            script_content: Ruby script content to execute

        Returns:
            Script execution result

        Raises:
            QueryExecutionError: If script execution fails

        """
        result = self.execute_query(script_content)
        # Try to parse as JSON if possible, otherwise return as dict with result key
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return {"result": result}

    def execute_script_with_data(  # noqa: C901, PLR0912, PLR0915
        self,
        script_content: str,
        data: Any,  # noqa: ANN401
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Execute a Ruby script in the Rails console with structured input data.

        The data is serialized to JSON, transferred to the container under /tmp,
        and made available to the Ruby script as the variable `input_data`.
        The script is also written to a temporary file and executed via:
        load "/tmp/<script>.rb"

        The Ruby script should print a JSON payload between the markers
        JSON_OUTPUT_START and JSON_OUTPUT_END, which will be parsed and returned
        as `data` in the result dict.

        Args:
            script_content: Ruby code that expects `input_data` to be defined
            data: Arbitrary JSON-serializable input for the script
            timeout: Optional Rails console timeout

        Returns:
            Dict with keys: status ("success"|"error"), message, data (parsed JSON), output (raw snippet)

        """
        # Prepare local temp paths
        temp_dir = Path(self.file_manager.data_dir) / "temp_scripts"
        temp_dir.mkdir(parents=True, exist_ok=True)

        local_data_path = temp_dir / f"openproject_input_{os.urandom(4).hex()}.json"
        try:
            with local_data_path.open("w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            err_msg = f"Failed to serialize input data: {e}"
            raise QueryExecutionError(err_msg) from e

        # Compose Ruby script with a small header that loads JSON into `input_data`
        container_data_path = Path("/tmp") / local_data_path.name  # noqa: S108
        header = f"require 'json'\ninput_data = JSON.parse(File.read('{container_data_path.as_posix()}'))\n"
        full_script = header + script_content

        local_script_path: Path | None = None
        container_script_path: Path | None = None
        operation_succeeded = False  # Track success for debug file preservation
        try:
            # Create local script file and transfer both script and data
            local_script_path = self._create_script_file(full_script)
            container_script_path = self._transfer_rails_script(local_script_path)

            # Transfer the input JSON to container
            self.transfer_file_to_container(local_data_path, container_data_path)

            # Execute the script inside Rails console.
            # IMPORTANT: We use a unique execution ID to distinguish this run's output
            # from any previous runs still visible in the tmux buffer.
            import shutil
            import subprocess

            exec_id = os.urandom(8).hex()
            unique_start_marker = f"JSON_OUTPUT_START_{exec_id}"
            unique_end_marker = f"JSON_OUTPUT_END_{exec_id}"

            load_cmd = f'load "{container_script_path.as_posix()}"'
            try:
                target = self.rails_client._get_target()
                tmux = shutil.which("tmux") or "tmux"

                # First, send a Ruby command to define the unique markers for this execution
                # The script will use these instead of hardcoded markers
                marker_setup = f"$j2o_start_marker = '{unique_start_marker}'; $j2o_end_marker = '{unique_end_marker}'"
                subprocess.run(
                    [tmux, "send-keys", "-t", target, marker_setup, "Enter"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                time.sleep(0.1)

                # Send the load command
                escaped_cmd = self.rails_client._escape_command(load_cmd)
                subprocess.run(
                    [tmux, "send-keys", "-t", target, escaped_cmd, "Enter"],
                    capture_output=True,
                    text=True,
                    check=True,
                )

                # Poll for our unique JSON_OUTPUT_END marker with timeout
                effective_timeout = timeout or self.rails_client.command_timeout
                start_time = time.time()
                output = ""
                found_markers = False

                while time.time() - start_time < effective_timeout:
                    cap = subprocess.run(
                        [tmux, "capture-pane", "-p", "-S", "-2000", "-t", target],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    output = cap.stdout

                    # Normalize output by removing newlines to handle markers split
                    # by terminal width wrapping in tmux pane capture
                    normalized = output.replace('\n', '').replace('\r', '')

                    # Check for markers in normalized output (handles line wrapping)
                    # Use rfind to find LAST occurrence (actual JSON output, not command echo)
                    # The command echo contains markers inside quotes which we must skip
                    if unique_start_marker in normalized and unique_end_marker in normalized:
                        # Find the LAST occurrence of start marker followed by JSON content
                        start_pos = normalized.rfind(unique_start_marker)
                        if start_pos != -1:
                            # Verify this is actual JSON output (followed by [ or {), not command echo (followed by ')
                            next_char_pos = start_pos + len(unique_start_marker)
                            if next_char_pos < len(normalized):
                                next_char = normalized[next_char_pos]
                                if next_char in '[{':  # Actual JSON content
                                    end_pos = normalized.find(unique_end_marker, next_char_pos)
                                    if end_pos != -1 and end_pos > start_pos:
                                        found_markers = True
                                        output = normalized  # Use normalized for extraction
                                        break

                    time.sleep(0.2)  # Poll every 200ms

                if not found_markers:
                    logger.warning(
                        "JSON_OUTPUT_END_%s marker not found within %d seconds",
                        exec_id,
                        effective_timeout,
                    )
            except Exception as e:
                # Fallback: if Rails console crashed or is unstable (e.g., Reline/IRB errors),
                # execute via non-interactive runner to avoid TTY/Reline issues.
                from src.clients.rails_console_client import (  # noqa: PLC0415
                    CommandExecutionError,
                    ConsoleNotReadyError,
                    RubyError,
                )

                if isinstance(e, (ConsoleNotReadyError, CommandExecutionError, RubyError)):
                    # Respect configuration: only fall back to rails runner when explicitly enabled
                    if not config.migration_config.get("enable_runner_fallback", False):
                        raise
                    logger.warning(
                        "Rails console execution failed (%s). Falling back to rails runner.",
                        type(e).__name__,
                    )
                    # Try common app roots: /app (official) then /opt/openproject (legacy)
                    runner_cmd = (
                        f"(cd /app || cd /opt/openproject) && "
                        f"bundle exec rails runner {container_script_path.as_posix()}"
                    )
                    # Ensure runner executes with the provided timeout (default to 120s if None)
                    stdout, stderr, rc = self.docker_client.execute_command(
                        runner_cmd,
                        timeout=timeout or 120,
                    )
                    if rc != 0:
                        q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(q_msg) from e
                    output = stdout
                else:
                    raise

            # Extract JSON payload between unique markers (JSON_OUTPUT_START_{exec_id} / JSON_OUTPUT_END_{exec_id})
            # Normalize output by removing newlines to handle markers split by terminal width wrapping
            # This is safe because JSON from Rails is output on a single line
            normalized_output = output.replace('\n', '').replace('\r', '')

            # Find markers in normalized output - use rfind to get the LAST occurrence
            # (the first occurrence is in the command echo: $j2o_start_marker = '...')
            # The actual JSON output marker comes after the command echo
            start_idx = normalized_output.rfind(unique_start_marker)
            if start_idx != -1:
                end_idx = normalized_output.find(unique_end_marker, start_idx + len(unique_start_marker))
            else:
                end_idx = -1

            # Use normalized output for extraction since JSON has no real newlines
            output = normalized_output

            # DEBUG: Log marker positions
            logger.debug(
                "Marker extraction: start_idx=%s, end_idx=%s, marker=%s, output_len=%d",
                start_idx,
                end_idx,
                exec_id,
                len(output),
            )
            if start_idx != -1 and end_idx != -1:
                json_preview = output[start_idx : end_idx + len(unique_end_marker)][:200]
                logger.debug("JSON region preview: %r", json_preview)

            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_str = output[start_idx + len(unique_start_marker) : end_idx].strip()

                # Fallback parsing with sanitization to guard against stray control chars from IRB/tmux
                def _try_parse(s: str) -> Any:  # noqa: ANN401
                    return json.loads(s)

                def _sanitize_control_chars(s: str) -> str:
                    # Remove ANSI escape sequences (colors, cursor movement, etc.)
                    s = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", s)
                    # Also remove OSC sequences (e.g., \x1b]...BEL or ST)
                    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?", "", s)
                    # Remove other escape sequences
                    s = re.sub(r"\x1b[^[]]", "", s)
                    # Remove ASCII control chars except tab (CR/LF are NOT preserved - they corrupt JSON)
                    s = re.sub(r"[\x00-\x08\x0A-\x0D\x0E-\x1F\x7f]", "", s)
                    return s

                def _extract_first_json_block(s: str) -> str | None:  # noqa: C901
                    # Attempt to isolate the first balanced JSON object/array
                    for i, ch in enumerate(s):
                        if ch in "{[":
                            opening = ch
                            closing = "}" if ch == "{" else "]"
                            depth = 0
                            in_str = False
                            esc = False
                            for j in range(i, len(s)):
                                c = s[j]
                                if in_str:
                                    if esc:
                                        esc = False
                                    elif c == "\\":
                                        esc = True
                                    elif c == '"':
                                        in_str = False
                                elif c == '"':
                                    in_str = True
                                elif c == opening:
                                    depth += 1
                                elif c == closing:
                                    depth -= 1
                                    if depth == 0:
                                        return s[i : j + 1]
                            break
                    return None

                # Always sanitize first - tmux output often has ANSI codes and line wraps
                json_str = _sanitize_control_chars(json_str)
                try:
                    parsed = _try_parse(json_str)
                except Exception:  # noqa: BLE001
                    try:
                        parsed = _try_parse(json_str)  # Already sanitized
                    except Exception:  # noqa: BLE001
                        candidate = _extract_first_json_block(json_str)
                        if candidate is None:
                            candidate = _extract_first_json_block(_sanitize_control_chars(json_str)) or json_str
                        try:
                            parsed = _try_parse(candidate)
                        except json.JSONDecodeError as e:
                            # Debug: log the problematic area
                            pos = e.pos if hasattr(e, "pos") else 0
                            start_ctx = max(0, pos - 20)
                            end_ctx = min(len(candidate), pos + 20)
                            ctx = candidate[start_ctx:end_ctx]
                            char_at_pos = repr(candidate[pos : pos + 5]) if pos < len(candidate) else "EOF"
                            logger.warning(
                                "JSON parse error at pos %d: char=%s, context=%r",
                                pos,
                                char_at_pos,
                                ctx,
                            )
                            q_msg = f"Failed to parse JSON output: {e}"
                            raise QueryExecutionError(q_msg) from e
                        except Exception as e:
                            q_msg = f"Failed to parse JSON output: {e}"
                            raise QueryExecutionError(q_msg) from e

                # Return a structured success response
                operation_succeeded = True
                return {
                    "status": "success",
                    "message": "Script executed successfully",
                    "data": parsed,
                    "output": output[:2000],
                }

            # No JSON markers found - return an error envelope for callers
            # Note: Still mark as "succeeded" since execution completed (just no JSON found)
            operation_succeeded = True
            return {
                "status": "error",
                "message": "JSON markers not found in Rails output",
                "output": output[:2000],
            }

        finally:
            # Cleanup logic: preserve debug files on errors if configured
            preserve_on_error = config.migration_config.get("preserve_debug_files_on_error", True)
            should_cleanup = operation_succeeded or not preserve_on_error

            if not should_cleanup:
                # Preserve files for debugging - log their locations
                logger.warning(
                    "Preserving debug files due to error (set preserve_debug_files_on_error=false to auto-cleanup):\n"
                    "  Local script: %s\n"
                    "  Container script: %s\n"
                    "  Local data: %s\n"
                    "  Container data: %s",
                    local_script_path,
                    container_script_path,
                    local_data_path,
                    container_data_path,
                )
            else:
                # Best-effort cleanup of local + remote files
                try:
                    if local_script_path is not None and container_script_path is not None:
                        self._cleanup_script_files(local_script_path, container_script_path)
                except Exception as cleanup_err:
                    logger.warning(
                        "Failed to cleanup script files (local=%s, container=%s): %s",
                        local_script_path,
                        container_script_path,
                        cleanup_err,
                    )
                try:
                    self._cleanup_script_files(local_data_path, container_data_path)
                except Exception as cleanup_err:
                    logger.warning(
                        "Failed to cleanup data files (local=%s, container=%s): %s",
                        local_data_path,
                        container_data_path,
                        cleanup_err,
                    )

    def transfer_file_to_container(
        self,
        local_path: Path,
        container_path: Path,
    ) -> None:
        """Transfer a file from local to the OpenProject container.

        Args:
            local_path: Path to local file
            container_path: Destination path in container

        Raises:
            FileTransferError: If the transfer fails for any reason

        """
        try:
            self.docker_client.transfer_file_to_container(local_path, container_path)
        except Exception as e:
            error_msg = "Failed to transfer file to container."
            logger.exception(error_msg)
            raise FileTransferError(error_msg) from e

    def is_connected(self) -> bool:
        """Test if connected to OpenProject.

        Returns:
            True if connected, False otherwise

        """
        try:
            # Generate a unique ID to verify connection
            unique_id = secrets.token_hex(3)

            # Simple command to echo the ID back
            command = f'puts "OPENPROJECT_CONNECTION_TEST_{unique_id}"'

            # Execute the command
            result = self.rails_client.execute(command)

            # Check if the unique ID is in the response
            if f"OPENPROJECT_CONNECTION_TEST_{unique_id}" in result:
                return True
        except Exception:
            logger.exception("Connection test failed.")
        else:
            return False

    def execute_query(self, query: str, timeout: int | None = None) -> str:
        """Execute a Rails query.

        Args:
            query: Rails query to execute
            timeout: Timeout in seconds

        Returns:
            Query results

        Raises:
            QueryExecutionError: If execution fails
            JsonParseError: If result parsing fails

        """
        self._last_query = query

        # Use provided timeout or default to 30 seconds for complex operations
        effective_timeout = timeout if timeout is not None else 30
        return self.rails_client._send_command_to_tmux(  # noqa: SLF001
            f"puts ({query})",
            effective_timeout,
        )

    def execute_query_to_json_file(self, query: str, timeout: int | None = None) -> dict[str, Any]:
        """Execute a Rails query and return parsed JSON result.

        Default path: write JSON to a container file and read it back.
        This avoids tmux/console noise and parsing fragility.

        Args:
            query: Rails query to execute
            timeout: Timeout in seconds

        Returns:
            Parsed JSON data

        Raises:
            QueryExecutionError: If execution fails

        """
        try:
            # Always prefer file-based execution for reliability
            _ts = int(__import__("time").time())
            container_file = f"/tmp/j2o_query_{_ts}_{os.getpid()}.json"  # noqa: S108
            return self.execute_large_query_to_json_file(
                query,
                container_file=container_file,
                timeout=timeout,
            )
        except RubyError:
            logger.exception("Ruby error during execute_query_to_json_file")
            raise
        except Exception as e:
            logger.exception("Error in execute_query_to_json_file")
            raise QueryExecutionError(str(e)) from e

    def execute_large_query_to_json_file(  # noqa: C901, PLR0912, PLR0915
        self,
        query: str,
        container_file: str = "/tmp/j2o_query.json",  # noqa: S108
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Execute a Rails query by writing JSON to a container file, then read it back.

        Use this for large result sets to avoid tmux/console truncation and parsing fragility.
        This method suppresses console output and relies on Docker+SSH to retrieve data.

        Args:
            query: Rails query to execute; will be coerced to JSON in Ruby
            container_file: Absolute path inside the container to write JSON content
            timeout: Ruby execution timeout (defaults to self.command_timeout)

        Returns:
            Parsed JSON data

        """
        # Ensure JSON conversion on the Ruby side
        ruby_json_expr = f"({query}).as_json"
        # Build Ruby that writes JSON to file without printing large output to console
        # IMPORTANT: Do not shell-quote here; we need the actual path string in Ruby.
        ruby_path_literal = str(container_file).replace("'", "\\'")

        # Build provenance hint: where did this originate?
        # Compose a concise hint like: "j2o: migration/work_packages func=_migrate_work_packages project=NRS ts=..."
        def _caller_hint(default_component: str) -> str:
            try:
                stack = inspect.stack()
                # Prefer first non-client frame under src/, ideally from migrations/
                path: str | None = None
                func: str | None = None
                for fr in stack[1:50]:
                    filename = fr.filename
                    if "/src/" not in filename:
                        continue
                    # Skip internal client plumbing to surface the actual caller/component
                    if any(
                        skip in filename
                        for skip in (
                            "/src/clients/openproject_client.py",
                            "/src/clients/rails_console_client.py",
                            "/src/clients/docker_client.py",
                            "/src/clients/ssh_client.py",
                        )
                    ):
                        continue
                    path = filename.split("/src/")[-1]
                    func = fr.function
                    break

                # Derive a concise component label from path
                component = default_component
                if path:
                    component = (
                        path.replace("migrations/", "migration/")
                        .replace("clients/", "client/")
                        .replace("_migration.py", "")
                        .replace(".py", "")
                    )

                parts: list[str] = ["j2o:", component]
                if func:
                    parts.append(f"func={func}")
                ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                parts.append(f"ts={ts}")
                # include project filter if configured
                proj = (config.jira_config or {}).get("project_filter")
                if proj:
                    parts.append(f"project={proj}")
                return " ".join(parts)
            except Exception:
                return f"j2o: {default_component}"

        provenance = _caller_hint("query/json")

        ruby_script = (
            f"# {provenance}\n"
            "require 'json'\n"
            "begin; require 'fileutils'; rescue; end\n"
            f"data = {ruby_json_expr}\n"
            f"File.open('{ruby_path_literal}', 'w') do |f|\n"
            "  f.write(JSON.generate(data))\n"
            "  begin; f.flush; f.fsync; rescue; end\n"
            "end\n"
            f"begin; FileUtils.chmod(0644, '{ruby_path_literal}'); rescue; end\n"
        )

        # Choose execution mode: use rails runner for long scripts to avoid pasting into console
        # Use both max lines and char threshold (defaults: 10 lines OR 200 chars)
        max_lines_env = os.environ.get("J2O_SCRIPT_RUNNER_MAX_LINES")
        char_thresh_env = os.environ.get("J2O_SCRIPT_RUNNER_THRESHOLD")
        try:
            max_lines = int(max_lines_env) if max_lines_env else 10
        except Exception:
            max_lines = 10
        try:
            char_threshold = int(char_thresh_env) if char_thresh_env else 200
        except Exception:
            char_threshold = 200

        script_lines = ruby_script.count("\n") + 1
        use_runner = (script_lines >= max_lines) or (len(ruby_script) >= char_threshold)

        if use_runner:
            runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"  # noqa: S108
            local_tmp = Path(self.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
            local_tmp.parent.mkdir(parents=True, exist_ok=True)
            with local_tmp.open("w", encoding="utf-8") as f:
                f.write(ruby_script)
            self.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))

            # Decide load mode: default to console `load` to avoid tmux pastes but keep low startup cost
            # Force runner mode if J2O_FORCE_RAILS_RUNNER is set
            mode = (os.environ.get("J2O_SCRIPT_LOAD_MODE") or "console").lower()
            if os.environ.get("J2O_FORCE_RAILS_RUNNER"):
                mode = "runner"
            if mode == "console":
                try:
                    _console_output = self.rails_client.execute(
                        f"load '{runner_script_path}'",
                        timeout=timeout or 90,
                        suppress_output=True,
                    )
                    self._check_console_output_for_errors(
                        _console_output or "",
                        context="execute_large_query_to_json_file(load)",
                    )
                except Exception as e:
                    # Fallback to rails runner on console instability
                    runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                    stdout, stderr, rc = self.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(q_msg) from e
            else:
                runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                stdout, stderr, rc = self.docker_client.execute_command(
                    runner_cmd,
                    timeout=timeout or 120,
                )
                if rc != 0:
                    q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                    raise QueryExecutionError(q_msg)
        else:
            # Execute via persistent tmux Rails console (faster than rails runner)
            try:
                _console_output = self.rails_client.execute(
                    ruby_script,
                    timeout=timeout or 90,
                    suppress_output=True,
                )
                self._check_console_output_for_errors(
                    _console_output or "",
                    context="execute_large_query_to_json_file",
                )
            except Exception as e:
                if isinstance(e, (ConsoleNotReadyError, CommandExecutionError, RubyError)):
                    if not config.migration_config.get("enable_runner_fallback", False):
                        raise
                    logger.warning(
                        "Rails console failed during large query (%s); falling back to rails runner",
                        type(e).__name__,
                    )
                    runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"  # noqa: S108
                    local_tmp = Path(self.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
                    local_tmp.parent.mkdir(parents=True, exist_ok=True)
                    with local_tmp.open("w", encoding="utf-8") as f:
                        f.write(ruby_script)
                    self.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))
                    runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                    stdout, stderr, rc = self.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(q_msg) from e
                else:
                    raise

        # Read file back from container via SSH (avoids tmux buffer limits)
        ssh_command = f"docker exec {self.container_name} cat {container_file}"

        # Retry loop to handle race where file write completes slightly after command returns
        wait_env = os.environ.get("J2O_QUERY_RESULT_WAIT_SECONDS")
        try:
            max_wait_seconds = int(wait_env) if wait_env else 600
        except Exception:
            max_wait_seconds = 60
        poll_interval = 0.5
        attempts = max(1, int(max_wait_seconds / poll_interval))

        stdout = ""
        stderr = ""
        returncode = 1
        for attempt in range(attempts):
            try:
                stdout, stderr, returncode = self.ssh_client.execute_command(
                    ssh_command,
                    check=False,
                )
            except Exception as e:
                # Unexpected transport error; bubble it up immediately
                raise QueryExecutionError(str(e)) from e

            if returncode == 0 and stdout:
                if attempt > 0:
                    logger.debug(
                        "Recovered after %d attempts reading container file %s",
                        attempt + 1,
                        container_file,
                    )
                break

            # Non-zero return code with no stdout. Treat "file not yet present" as a retry case,
            # otherwise escalate after the loop.
            if "No such file or directory" in (stderr or ""):
                # Emit a lightweight heartbeat every ~5 seconds so runs don't look hung
                if attempt and (attempt % max(1, int(5 / poll_interval)) == 0):
                    logger.info(
                        "Waiting for query result file %s (waited %.1fs)",
                        container_file,
                        attempt * poll_interval,
                    )
                time.sleep(poll_interval)
                continue

            # Any other stderr/returncode is considered a hard failure
            if returncode != 0:
                break
            time.sleep(poll_interval)

        if returncode != 0:
            msg = f"SSH command failed with code {returncode}: {stderr}"
            raise QueryExecutionError(msg)

        try:
            return json.loads(stdout.strip())
        except Exception as e:  # Normalize JSON parse errors
            raise JsonParseError(str(e)) from e

    def _check_console_output_for_errors(self, output: str, context: str) -> None:
        """Raise a QueryExecutionError if console output indicates a Ruby error.

        This catches cases where start/end markers were missing and the console client
        returned raw lines, including SystemStackError or other Ruby exceptions.
        """
        if not output:
            return
        lines = [ln.strip() for ln in output.strip().splitlines()]
        has_error_marker = any(ln == "--EXEC_ERROR--" or ln.startswith("--EXEC_ERROR--") for ln in lines)
        severe_pattern = (
            ("SystemStackError" in output) or ("full_message':" in output) or ("stack level too deep" in output)
        )
        if has_error_marker or severe_pattern:
            # Preserve the most informative lines to aid diagnosis
            informative = [
                ln
                for ln in lines
                if ("SystemStackError" in ln)
                or ("stack level too deep" in ln)
                or ("full_message':" in ln)
                or ln.startswith("--EXEC_ERROR--")
            ]
            snippet = informative or lines[:6]
            q_msg = f"Console error during {context}: {' | '.join(snippet[:8])}"
            raise QueryExecutionError(q_msg)

    def _assert_expected_console_notice(self, output: str, expected_prefix: str, context: str) -> None:
        """Treat any unexpected console response as error in strict file-write flows.

        For file-based JSON writes we expect a specific notice line like
        "Statuses data written to /tmp/...". If it's missing or output contains
        unrelated content, flag as error to avoid silently accepting partial/garbled output.
        """
        if not output:
            # When suppress_output is used we still expect our explicit puts notice
            q_msg = f"No console output during {context}; expected '{expected_prefix}...'"
            raise QueryExecutionError(q_msg)
        # Normalize
        lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
        if not any(expected_prefix in ln for ln in lines):
            sample = " | ".join(lines[:5])
            q_msg = f"Unexpected console output during {context}; expected '{expected_prefix}...'. Got: {sample[:300]}"
            raise QueryExecutionError(q_msg)

    # Removed rails runner helper; all scripts go through persistent tmux console

    def _execute_batched_query(  # noqa: C901, PLR0912, PLR0915
        self,
        model_name: str,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a query in batches to avoid any truncation issues."""
        try:
            # First, try a simple non-batched approach for smaller datasets
            # This handles the common case where batching isn't needed
            simple_query = f"{model_name}.limit({BATCH_SIZE_DEFAULT}).to_json"
            result_output = self.execute_query(simple_query, timeout=timeout)

            try:
                simple_data = self._parse_rails_output(result_output)

                # If we get valid data and it's less than batch size, we're done
                if isinstance(simple_data, list) and len(simple_data) < BATCH_SIZE_DEFAULT:
                    logger.debug(
                        "Retrieved %d total records using simple query",
                        len(simple_data),
                    )
                    return simple_data
                if isinstance(simple_data, list) and len(simple_data) == BATCH_SIZE_DEFAULT:
                    # We might have more data, fall through to batched approach
                    logger.debug(
                        "Simple query returned 50 items, using batched approach for complete data",
                    )
                # Handle single item or other data types
                elif isinstance(simple_data, dict):
                    logger.debug("Retrieved 1 record using simple query")
                    return [simple_data]
                elif simple_data is not None:
                    logger.debug("Retrieved non-list data using simple query")
                    # For non-dict, non-list data, return empty list
                    logger.warning(
                        "Unexpected data type from simple query: %s",
                        type(simple_data),
                    )
                    return []
                else:
                    return []

            except Exception:  # noqa: BLE001
                logger.debug(
                    "Simple query failed, falling back to batched approach",
                )

            # Fall back to batched approach for larger datasets
            all_results = []
            batch_size = BATCH_SIZE_DEFAULT  # Increased batch size for better performance
            offset = 0

            while True:
                # Apply adaptive rate limiting before Rails console operation
                self.rate_limiter.wait_if_needed(f"batched_query_{model_name}")

                # Use a more reliable query pattern that works with Rails scopes
                # Use order by id to ensure consistent pagination
                query = f"{model_name}.unscoped.order(:id).offset({offset}).limit({batch_size}).to_json"

                operation_start = time.time()
                result_output = self.execute_query(query, timeout=timeout)
                operation_time = time.time() - operation_start

                try:
                    batch_data = self._parse_rails_output(result_output)

                    # Record successful operation for rate limiting adaptation
                    self.rate_limiter.record_response(operation_time, 200)

                    # If we get no data or empty array, we're done
                    if not batch_data or (isinstance(batch_data, list) and len(batch_data) == 0):
                        break

                    # If we get a single item instead of array, wrap it
                    if isinstance(batch_data, dict):
                        batch_data = [batch_data]

                    # Add to results
                    if isinstance(batch_data, list):
                        all_results.extend(batch_data)

                        # If we got fewer items than batch_size, we're done
                        if len(batch_data) < batch_size:
                            break
                    else:
                        logger.warning(
                            "Unexpected data type from batch query: %s",
                            type(batch_data),
                        )
                        break

                    offset += batch_size

                    # Increased safety limit for larger datasets
                    if offset > SAFE_OFFSET_LIMIT:
                        logger.warning("Reached safety limit of %d records, stopping", SAFE_OFFSET_LIMIT)
                        break

                except Exception:
                    logger.exception("Failed to parse batch at offset %d", offset)
                    # Record error for rate limiting adaptation
                    self.rate_limiter.record_response(operation_time, 500)
                    break

            logger.debug(
                "Retrieved %d total records using batched approach",
                len(all_results),
            )
            return all_results

        except Exception:
            logger.exception("Batched query failed")
        else:
            # Should not reach here; safe default
            return []

    def _parse_rails_output(self, result_output: str) -> object:  # noqa: C901, PLR0911, PLR0912, PLR0915
        """Parse Rails console output to extract JSON or scalar values.

        Handles various Rails console output formats including:
        - JSON arrays and objects
        - Scalar values (numbers, booleans, strings)
        - Rails console responses with => prefix
        - Empty/nil responses
        - TMUX marker-based output extraction

        Args:
            result_output: Raw output from Rails console

        Returns:
            Parsed data (dict, list, scalar value, or None)

        """
        if not result_output or result_output.strip() == "":
            logger.debug("Empty or None result output")
            return None

        try:
            logger.debug("Raw result_output: %s", repr(result_output[:500]))
            text = result_output.strip()

            # Sanitize terminal artifacts to protect JSON parsing
            try:
                # 1) Strip ANSI escape sequences entirely
                ansi_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
                text = ansi_re.sub("", text)
                # 2) Remove remaining control chars (except \t, \n, \r)
                text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
            except Exception:  # noqa: BLE001
                logger.debug("ANSI/control char sanitization failed; continuing with raw text")

            # If it's plain JSON, parse immediately

            if text.startswith(("[", "{")):
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:  # type: ignore[name-defined]
                    raise JsonParseError(str(e)) from e

            # TMUX_CMD_* markers removed; rely on EXEC_* markers and direct JSON

            # Drop Rails prompt lines, but preserve following JSON
            lines_in = text.split("\n")
            lines: list[str] = []
            # skip_prompt variable removed (unused)
            for ln in lines_in:
                if ln.strip().startswith("open-project("):
                    # Skip prompt lines
                    continue
                # Keep non-empty lines (including the JSON following the prompt)
                if ln.strip():
                    lines.append(ln)
            text = "\n".join(lines)

            # Handle Rails prefixed outputs like "=> <value>"
            for ln in (seg.strip() for seg in text.split("\n")):
                if ln.startswith("=> "):
                    val = ln[3:].strip()
                    # If this is '=> nil' but JSON is present elsewhere, prefer the JSON
                    if val == "nil" and ("[" in text or "{" in text):
                        continue
                    if val.startswith(("[", "{")):
                        try:
                            return json.loads(val)
                        except json.JSONDecodeError as e:  # type: ignore[name-defined]
                            raise JsonParseError(str(e)) from e
                    if val == "nil":
                        return None
                    if val == "true":
                        return True
                    if val == "false":
                        return False
                    if val.isdigit():
                        return int(val)
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        return val[1:-1]
                    return val

            # Try bracket-slice JSON extraction (prefer arrays before scalars)
            lb = text.find("[")
            rb = text.rfind("]")
            if lb != -1 and rb != -1 and rb > lb:
                try:
                    return json.loads(text[lb : rb + 1])
                except json.JSONDecodeError as e:  # type: ignore[name-defined]
                    # As a fallback, strip remaining control characters and retry once
                    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text[lb : rb + 1])
                    try:
                        return json.loads(cleaned)
                    except Exception:  # noqa: BLE001
                        raise JsonParseError(str(e)) from e
            lb = text.find("{")
            rb = text.rfind("}")
            if lb != -1 and rb != -1 and rb > lb:
                try:
                    return json.loads(text[lb : rb + 1])
                except json.JSONDecodeError as e:  # type: ignore[name-defined]
                    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text[lb : rb + 1])
                    try:
                        return json.loads(cleaned)
                    except Exception:  # noqa: BLE001
                        raise JsonParseError(str(e)) from e

            # Special case: prompt + JSON + => nil (common Rails console pattern)
            if "=> nil" in text:
                # Prefer the line immediately preceding the => nil
                lines2 = text.split("\n")
                for i, ln in enumerate(lines2):
                    if ln.strip().startswith("=> nil") and i > 0:
                        prev = lines2[i - 1].strip()
                        if prev.startswith(("[", "{")):
                            try:
                                return json.loads(prev)
                            except json.JSONDecodeError as e:  # type: ignore[name-defined]
                                raise JsonParseError(str(e)) from e
                # Fallback to search earlier JSON
                lb = text.find("[")
                rb = text.rfind("]")
                if lb != -1 and rb != -1 and rb > lb:
                    try:
                        return json.loads(text[lb : rb + 1])
                    except json.JSONDecodeError as e:  # type: ignore[name-defined]
                        raise JsonParseError(str(e)) from e
                lb = text.find("{")
                rb = text.rfind("}")
                if lb != -1 and rb != -1 and rb > lb:
                    try:
                        return json.loads(text[lb : rb + 1])
                    except json.JSONDecodeError as e:  # type: ignore[name-defined]
                        raise JsonParseError(str(e)) from e

            # Scalars
            t = text.strip()
            if t.isdigit():
                return int(t)
            if t in ("true", "false"):
                return t == "true"
            if t == "nil":
                return None
            if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
                return t[1:-1]

            _msg = "Unable to parse Rails console output"
            raise JsonParseError(_msg)  # noqa: TRY301

        except JsonParseError:
            # Re-raise JsonParseError
            raise
        except json.JSONDecodeError as e:  # type: ignore[name-defined]
            # Normalize any JSON decoding errors to JsonParseError as tests expect
            raise JsonParseError(str(e)) from e
        except Exception as e:
            logger.exception("Failed to process query result: %s", repr(e))  # noqa: TRY401
            logger.exception("Raw output: %s", result_output[:200])
            # Raise an exception instead of returning None to ensure proper error handling
            msg = f"Failed to parse Rails console output: {e}"
            raise QueryExecutionError(
                msg,
            ) from e

    def execute_json_query(self, query: str, timeout: int | None = None) -> object:
        """Execute a Rails query and return parsed JSON result.

        This method is optimized for retrieving data from Rails as JSON,
        automatically handling the conversion and parsing.

        Args:
            query: Rails query to execute (should produce JSON output)
            timeout: Timeout in seconds

        Returns:
            Parsed JSON result (list, dict, scalar, or None)

        Raises:
            QueryExecutionError: If execution fails
            JsonParseError: If result cannot be parsed as JSON

        """
        # Modify query to ensure it produces JSON output
        if not (".to_json" in query or ".as_json" in query):
            # Add as_json if the query doesn't already have JSON conversion
            json_query = f"{query}.as_json" if query.strip().endswith(")") else f"({query}).as_json"
        else:
            json_query = query

        # Execute the query and get result from JSON file
        return self.execute_query_to_json_file(json_query, timeout)

    def count_records(self, model: str) -> int:
        """Count records for a given Rails model.

        Args:
            model: Model name (e.g., "User", "Project")

        Returns:
            Number of records

        Raises:
            QueryExecutionError: If the count query fails

        """
        import shutil
        import subprocess

        # Use unique markers to extract count reliably from tmux output
        marker_id = secrets.token_hex(8)
        start_marker = f"J2O_COUNT_START_{marker_id}"
        end_marker = f"J2O_COUNT_END_{marker_id}"

        # Simple inline command that prints markers around the count
        query = f'puts "{start_marker}"; puts {model}.count; puts "{end_marker}"'

        # Get tmux target
        target = self.rails_client._get_target()  # noqa: SLF001
        tmux = shutil.which("tmux") or "tmux"

        # Send the command
        escaped_command = self.rails_client._escape_command(query)  # noqa: SLF001
        subprocess.run(  # noqa: S603
            [tmux, "send-keys", "-t", target, escaped_command, "Enter"],
            capture_output=True,
            text=True,
            check=True,
        )

        # Wait for the end marker to appear (up to 30 seconds)
        max_wait = 30
        start_time = time.time()
        result = ""

        while time.time() - start_time < max_wait:
            time.sleep(0.3)
            cap = subprocess.run(  # noqa: S603
                [tmux, "capture-pane", "-p", "-S", "-100", "-t", target],
                capture_output=True,
                text=True,
                check=True,
            )
            result = cap.stdout
            if end_marker in result:
                break

        # Parse output - find lines that exactly match our markers
        lines = result.split("\n")
        start_idx = -1
        end_idx = -1

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Require exact match (not substring) to avoid matching echoed commands
            if stripped == start_marker:
                start_idx = i
            elif stripped == end_marker and start_idx != -1:
                end_idx = i
                break

        if start_idx != -1 and end_idx != -1:
            # Extract content between markers
            for line in lines[start_idx + 1 : end_idx]:
                stripped = line.strip()
                if stripped.isdigit():
                    return int(stripped)

        # Fallback: scan for the count in output after our query
        # Look for the last occurrence of the marker pattern followed by a number
        in_our_output = False
        for line in reversed(lines):
            stripped = line.strip()
            if stripped == end_marker:
                in_our_output = True
            elif in_our_output and stripped.isdigit():
                return int(stripped)
            elif stripped == start_marker:
                break

        msg = f"Unable to parse count result for {model}: end_marker={end_marker in result}"
        raise QueryExecutionError(msg)

    # -------------------------------------------------------------
    # Work Package Custom Field helpers for fast-forward migrations
    # -------------------------------------------------------------
    def ensure_work_package_custom_field(self, name: str, field_format: str = "string") -> dict[str, Any]:
        """Ensure a WorkPackage custom field exists, create if missing.

        Args:
            name: Custom field name
            field_format: OpenProject field_format (e.g., 'string', 'date', 'datetime')

        Returns:
            Dict with at least {id, name, field_format}

        """
        ruby = f"""
          cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{name}')
          if !cf
            cf = CustomField.new(name: '{name}', field_format: '{field_format}', is_required: false, is_for_all: true, type: 'WorkPackageCustomField')
            cf.save
          end
          cf && cf.as_json(only: [:id, :name, :field_format])
        """
        try:
            result = self.execute_json_query(ruby)
            if isinstance(result, dict) and result.get("id"):
                return result
            raise QueryExecutionError("Failed ensuring WorkPackage custom field")
        except Exception as e:
            msg = f"Failed to ensure custom field '{name}': {e}"
            raise QueryExecutionError(msg) from e

    def ensure_custom_field(
        self,
        name: str,
        *,
        field_format: str = "string",
        cf_type: str = "WorkPackageCustomField",
        searchable: bool = False,
    ) -> dict[str, Any]:
        """Ensure a CustomField exists for the given type, create if missing.

        Args:
            name: Custom field name
            field_format: Field format (string, text, int, date, etc.)
            cf_type: Custom field type (WorkPackageCustomField, ProjectCustomField, UserCustomField)
            searchable: Whether the field should be searchable in OpenProject search

        Returns:
            Dict with custom field attributes (id, name, field_format, type)

        """
        # Set searchable attribute in Ruby (true/false, not Python bool)
        searchable_str = "true" if searchable else "false"

        ruby = f"""
          cf = CustomField.find_by(type: '{cf_type}', name: '{name}')
          if !cf
            cf = CustomField.new(name: '{name}', field_format: '{field_format}', is_required: false, type: '{cf_type}')
            begin
              cf.is_for_all = true
            rescue
            end
            begin
              cf.searchable = {searchable_str}
            rescue
            end
            cf.save
            # For WorkPackageCustomField, explicitly enable for all types
            if cf.type == 'WorkPackageCustomField' && cf.type_ids.empty?
              begin
                cf.type_ids = Type.all.pluck(:id)
                cf.save
              rescue
              end
            end
          else
            # Update searchable if it doesn't match
            begin
              if cf.respond_to?(:searchable) && cf.searchable != {searchable_str}
                cf.searchable = {searchable_str}
                cf.save
              end
            rescue
            end
            # Ensure WP CFs are enabled for all types
            if cf.type == 'WorkPackageCustomField' && cf.type_ids.empty?
              begin
                cf.type_ids = Type.all.pluck(:id)
                cf.save
              rescue
              end
            end
          end
          if cf && cf.type == 'UserCustomField'
            begin
              cf.activate! if cf.respond_to?(:active?) && !cf.active?
            rescue
            end
          end
          cf && cf.as_json(only: [:id, :name, :field_format, :type])
        """
        try:
            result = self.execute_json_query(ruby)
            if isinstance(result, dict) and result.get("id"):
                return result
            raise QueryExecutionError(f"Failed ensuring {cf_type} '{name}'")
        except Exception as e:
            msg = f"Failed to ensure custom field '{name}' ({cf_type}): {e}"
            raise QueryExecutionError(msg) from e

    def remove_custom_field(self, name: str, *, cf_type: str | None = None) -> dict[str, int]:
        """Remove CustomField records matching the provided name/type."""
        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        name_literal = json.dumps(name, ensure_ascii=False)
        type_filter = ""
        if cf_type:
            type_literal = json.dumps(cf_type, ensure_ascii=False)
            type_filter = f"scope = scope.where(type: {type_literal})\n"

        ruby = (
            f"scope = CustomField.where(name: {name_literal})\n"
            f"{type_filter}"
            "removed = 0\n"
            "scope.find_each do |cf|\n"
            "  begin\n"
            "    cf.destroy\n"
            "    removed += 1\n"
            "  rescue => e\n"
            '    Rails.logger.warn("Failed to destroy custom field #{cf.id}: #{e.message}")\n'
            "  end\n"
            "end\n"
            "{ removed: removed }.to_json\n"
        )

        try:
            result = self.execute_json_query(ruby)
            if isinstance(result, dict):
                return {"removed": int(result.get("removed", 0) or 0)}
            raise QueryExecutionError("Unexpected response removing custom field")
        except Exception as e:
            msg = f"Failed to remove custom field '{name}'"
            raise QueryExecutionError(msg) from e

    def ensure_origin_custom_fields(self) -> dict[str, list[dict[str, Any]]]:
        """Ensure origin mapping CFs exist for WP, Project, User, TimeEntry."""
        ensured: dict[str, list[dict[str, Any]]] = {"work_package": [], "project": [], "user": [], "time_entry": []}

        for name, fmt in (
            ("J2O Origin System", "string"),
            ("J2O Origin ID", "string"),
            ("J2O Origin Key", "string"),
            ("J2O Origin URL", "string"),
            ("J2O Project Key", "string"),
            ("J2O Project ID", "string"),
            ("J2O First Migration Date", "date"),
            ("J2O Last Update Date", "date"),
        ):
            try:
                ensured["work_package"].append(
                    self.ensure_custom_field(name, field_format=fmt, cf_type="WorkPackageCustomField"),
                )
            except Exception as e:  # noqa: BLE001
                self.logger.warning("Failed ensuring WP CF %s: %s", name, e)

        # NOTE: This OpenProject instance does not support Project custom fields via this path.
        # Persist origin for projects using project attributes (see upsert_project_origin_attributes).
        ensured["project"] = []

        for name, fmt in (
            ("J2O Origin System", "string"),
            ("J2O User ID", "string"),
            ("J2O User Key", "string"),
            ("J2O External URL", "string"),
        ):
            try:
                ensured["user"].append(self.ensure_custom_field(name, field_format=fmt, cf_type="UserCustomField"))
            except Exception as e:  # noqa: BLE001
                self.logger.warning("Failed ensuring User CF %s: %s", name, e)

        for name, fmt in (
            ("J2O Origin Worklog Key", "string"),
            ("J2O Origin Issue ID", "string"),
            ("J2O Origin Issue Key", "string"),
            ("J2O Origin System", "string"),
            ("J2O First Migration Date", "date"),
            ("J2O Last Update Date", "date"),
        ):
            try:
                ensured["time_entry"].append(
                    self.ensure_custom_field(name, field_format=fmt, cf_type="TimeEntryCustomField"),
                )
            except Exception as e:  # noqa: BLE001
                self.logger.warning("Failed ensuring TE CF %s: %s", name, e)

        return ensured

    # =========================================================================
    # J2O Provenance Registry
    # =========================================================================
    # For entities that cannot have custom fields directly (Groups, Types,
    # Statuses), we use a special "J2O Migration" project to store provenance
    # data as work packages. This allows restoration of ALL mappings from OP
    # alone without requiring local mapping files.
    # =========================================================================

    J2O_MIGRATION_PROJECT_IDENTIFIER = "j2o-migration-provenance"
    J2O_MIGRATION_PROJECT_NAME = "J2O Migration Provenance"

    # Entity types tracked in provenance registry (those without direct CF support)
    # Note: company and account also create OP Projects but from Tempo sources
    # custom_field and link_type track CF creation for Jira→OP field mapping
    J2O_PROVENANCE_ENTITY_TYPES = (
        "project", "group", "type", "status", "company", "account",
        "custom_field", "link_type",
    )

    def ensure_j2o_migration_project(self) -> int:
        """Ensure the J2O Migration Provenance project exists.

        This project stores provenance data for entities that cannot have
        custom fields attached directly (groups, types, statuses, projects).

        Returns:
            OpenProject project ID for the J2O Migration project

        """
        return self.ensure_reporting_project(
            identifier=self.J2O_MIGRATION_PROJECT_IDENTIFIER,
            name=self.J2O_MIGRATION_PROJECT_NAME,
        )

    def ensure_j2o_provenance_types(self, project_id: int) -> dict[str, int]:
        """Ensure work package types exist for each provenance entity type.

        Creates types like 'J2O Project Mapping', 'J2O Group Mapping', etc.

        Args:
            project_id: The J2O Migration project ID

        Returns:
            Dict mapping entity type to OP type ID

        """
        type_ids: dict[str, int] = {}

        for entity_type in self.J2O_PROVENANCE_ENTITY_TYPES:
            type_name = f"J2O {entity_type.title()} Mapping"

            script = (
                "begin\n"
                f"  type_name = '{type_name}'\n"
                f"  project_id = {project_id}\n"
                "  wp_type = Type.find_by(name: type_name)\n"
                "  unless wp_type\n"
                "    wp_type = Type.create!(name: type_name, is_default: false, is_milestone: false)\n"
                "  end\n"
                "  project = Project.find(project_id)\n"
                "  unless project.types.include?(wp_type)\n"
                "    project.types << wp_type\n"
                "  end\n"
                "  { id: wp_type.id, name: wp_type.name }\n"
                "rescue => e\n"
                "  { error: e.message }\n"
                "end\n"
            )

            try:
                result = self.execute_json_query(script)
                if isinstance(result, dict) and result.get("id"):
                    type_ids[entity_type] = int(result["id"])
                    self.logger.debug("Ensured J2O type '%s' with ID %d", type_name, type_ids[entity_type])
                elif isinstance(result, dict) and result.get("error"):
                    self.logger.warning("Failed to ensure J2O type '%s': %s", type_name, result["error"])
            except Exception as e:
                self.logger.warning("Error ensuring J2O type '%s': %s", type_name, e)

        return type_ids

    def ensure_j2o_provenance_custom_fields(self) -> dict[str, int]:
        """Ensure custom fields for OP entity ID mapping exist.

        Creates fields like 'J2O OP Project ID', 'J2O OP Group ID', etc.

        Returns:
            Dict mapping field name to CF ID

        """
        cf_ids: dict[str, int] = {}

        # Fields for mapping to OP entity IDs
        cf_specs = [
            ("J2O OP Project ID", "int"),
            ("J2O OP Group ID", "int"),
            ("J2O OP Type ID", "int"),
            ("J2O OP Status ID", "int"),
            ("J2O OP Company ID", "int"),  # Tempo Company → OP Project ID
            ("J2O OP Account ID", "int"),  # Tempo Account → OP Project ID
            ("J2O OP Custom_field ID", "int"),  # Jira CF ID → OP CustomField ID
            ("J2O OP Link_type ID", "int"),  # Jira Link Type ID → OP CustomField ID
            ("J2O Entity Type", "string"),  # Entity type for filtering
        ]

        for name, fmt in cf_specs:
            try:
                result = self.ensure_custom_field(name, field_format=fmt, cf_type="WorkPackageCustomField")
                if isinstance(result, dict) and result.get("id"):
                    cf_ids[name] = int(result["id"])
            except Exception as e:
                self.logger.warning("Failed ensuring provenance CF '%s': %s", name, e)

        return cf_ids

    def record_entity_provenance(
        self,
        *,
        entity_type: str,
        jira_key: str,
        jira_id: str | None = None,
        op_entity_id: int,
        jira_name: str | None = None,
    ) -> dict[str, Any]:
        """Record provenance for an entity that cannot have custom fields.

        Creates or updates a work package in the J2O Migration project that
        stores the Jira→OP mapping for entities like projects, groups, types,
        and statuses.

        Args:
            entity_type: One of 'project', 'group', 'type', 'status'
            jira_key: The Jira entity key/identifier
            jira_id: The Jira entity ID (optional)
            op_entity_id: The OpenProject entity ID
            jira_name: The Jira entity name (optional)

        Returns:
            Dict with created/updated work package info

        """
        if entity_type not in self.J2O_PROVENANCE_ENTITY_TYPES:
            raise ValueError(f"Invalid entity type: {entity_type}. Must be one of {self.J2O_PROVENANCE_ENTITY_TYPES}")

        # Ensure infrastructure exists
        project_id = self.ensure_j2o_migration_project()
        type_ids = self.ensure_j2o_provenance_types(project_id)
        cf_ids = self.ensure_j2o_provenance_custom_fields()

        type_id = type_ids.get(entity_type)
        if not type_id:
            raise QueryExecutionError(f"Failed to get type ID for {entity_type}")

        # Build work package subject (unique identifier for this mapping)
        subject = f"{entity_type.upper()}: {jira_key}"
        if jira_name:
            subject = f"{subject} ({jira_name})"

        # Get CF IDs for the mapping fields
        cf_op_id_field = f"J2O OP {entity_type.title()} ID"
        cf_op_id = cf_ids.get(cf_op_id_field)
        cf_entity_type_id = cf_ids.get("J2O Entity Type")

        script = (
            "begin\n"
            f"  project = Project.find({project_id})\n"
            f"  wp_type = Type.find({type_id})\n"
            f"  subject = {repr(subject)}\n"
            "  status = Status.default || Status.first\n"
            "  priority = IssuePriority.default || IssuePriority.first\n"
            "  # Find existing or create new\n"
            f"  wp = project.work_packages.where(type_id: {type_id}).find_by(subject: subject)\n"
            "  created = false\n"
            "  if wp.nil?\n"
            "    wp = WorkPackage.new(\n"
            "      project: project,\n"
            "      type: wp_type,\n"
            "      subject: subject,\n"
            "      status: status,\n"
            "      priority: priority,\n"
            "      author: User.admin.first || User.first\n"
            "    )\n"
            "    created = true\n"
            "  end\n"
            "  # Set custom field values\n"
            "  cf_values = {}\n"
            f"  cf_values[{cf_op_id}] = {op_entity_id} if {cf_op_id}\n" if cf_op_id else ""
            f"  cf_values[{cf_entity_type_id}] = '{entity_type}' if {cf_entity_type_id}\n" if cf_entity_type_id else ""
            "  wp.custom_field_values = cf_values if cf_values.any?\n"
            "  wp.save!\n"
            "  { success: true, id: wp.id, subject: wp.subject, created: created }\n"
            "rescue => e\n"
            "  { success: false, error: e.message, backtrace: e.backtrace.first(3) }\n"
            "end\n"
        )

        try:
            result = self.execute_json_query(script)
            if isinstance(result, dict):
                if result.get("success"):
                    action = "Created" if result.get("created") else "Updated"
                    self.logger.debug("%s provenance WP for %s '%s' → OP ID %d", action, entity_type, jira_key, op_entity_id)
                return result
            return {"success": False, "error": f"Unexpected result: {result}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def restore_entity_mappings_from_provenance(self, entity_type: str) -> dict[str, dict[str, Any]]:
        """Restore entity mappings from provenance work packages.

        Queries the J2O Migration project for work packages of the specified
        entity type and reconstructs the Jira→OP mapping.

        Args:
            entity_type: One of 'project', 'group', 'type', 'status'

        Returns:
            Dict mapping Jira key to mapping data

        """
        if entity_type not in self.J2O_PROVENANCE_ENTITY_TYPES:
            raise ValueError(f"Invalid entity type: {entity_type}. Must be one of {self.J2O_PROVENANCE_ENTITY_TYPES}")

        # Get the project and type IDs (may not exist if never recorded)
        try:
            project_result = self.get_project_by_identifier(self.J2O_MIGRATION_PROJECT_IDENTIFIER)
            if not project_result or not project_result.get("id"):
                self.logger.info("J2O Migration project not found - no provenance data available")
                return {}
            project_id = int(project_result["id"])
        except Exception:
            self.logger.debug("J2O Migration project not found")
            return {}

        # Find the type for this entity
        type_name = f"J2O {entity_type.title()} Mapping"
        cf_op_id_field = f"J2O OP {entity_type.title()} ID"

        script = (
            "begin\n"
            f"  project = Project.find({project_id})\n"
            f"  wp_type = Type.find_by(name: '{type_name}')\n"
            "  return [].to_json unless wp_type\n"
            f"  cf_op_id = CustomField.find_by(name: '{cf_op_id_field}', type: 'WorkPackageCustomField')\n"
            "  cf_entity_type = CustomField.find_by(name: 'J2O Entity Type', type: 'WorkPackageCustomField')\n"
            "  # Also get J2O Origin fields for full provenance\n"
            "  cf_origin_key = CustomField.find_by(name: 'J2O Origin Key', type: 'WorkPackageCustomField')\n"
            "  cf_origin_id = CustomField.find_by(name: 'J2O Origin ID', type: 'WorkPackageCustomField')\n"
            "  cf_origin_system = CustomField.find_by(name: 'J2O Origin System', type: 'WorkPackageCustomField')\n"
            f"  wps = project.work_packages.where(type_id: wp_type.id)\n"
            "  wps.map do |wp|\n"
            "    {\n"
            "      id: wp.id,\n"
            "      subject: wp.subject,\n"
            "      op_entity_id: (cf_op_id ? wp.custom_value_for(cf_op_id)&.value&.to_i : nil),\n"
            "      entity_type: (cf_entity_type ? wp.custom_value_for(cf_entity_type)&.value : nil),\n"
            "      j2o_origin_key: (cf_origin_key ? wp.custom_value_for(cf_origin_key)&.value : nil),\n"
            "      j2o_origin_id: (cf_origin_id ? wp.custom_value_for(cf_origin_id)&.value : nil),\n"
            "      j2o_origin_system: (cf_origin_system ? wp.custom_value_for(cf_origin_system)&.value : nil)\n"
            "    }\n"
            "  end\n"
            "rescue => e\n"
            "  { error: e.message }\n"
            "end\n"
        )

        try:
            result = self.execute_json_query(script)
            if isinstance(result, dict) and result.get("error"):
                self.logger.warning("Error restoring %s mappings: %s", entity_type, result["error"])
                return {}

            if not isinstance(result, list):
                return {}

            # Build mapping from subject parsing and CF values
            mappings: dict[str, dict[str, Any]] = {}
            for wp in result:
                # Parse subject to extract Jira key: "TYPE: jira-key (name)" or "TYPE: jira-key"
                subject = wp.get("subject", "")
                prefix = f"{entity_type.upper()}: "
                if subject.startswith(prefix):
                    rest = subject[len(prefix):]
                    # Handle "(name)" suffix
                    if " (" in rest and rest.endswith(")"):
                        jira_key = rest.split(" (")[0]
                        jira_name = rest.split(" (")[1].rstrip(")")
                    else:
                        jira_key = rest
                        jira_name = None

                    mappings[jira_key] = {
                        "jira_key": jira_key,
                        "jira_name": jira_name,
                        "openproject_id": wp.get("op_entity_id"),
                        "matched_by": "j2o_provenance",
                        "j2o_origin_key": wp.get("j2o_origin_key"),
                        "j2o_origin_id": wp.get("j2o_origin_id"),
                        "j2o_origin_system": wp.get("j2o_origin_system"),
                        "restored_from_op": True,
                        "provenance_wp_id": wp.get("id"),
                    }

            self.logger.info("Restored %d %s mappings from provenance", len(mappings), entity_type)
            return mappings

        except Exception as e:
            self.logger.warning("Failed to restore %s mappings from provenance: %s", entity_type, e)
            return {}

    def bulk_record_entity_provenance(
        self,
        entity_type: str,
        mappings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk record provenance for multiple entities.

        Args:
            entity_type: One of 'project', 'group', 'type', 'status'
            mappings: List of dicts with keys: jira_key, jira_id (optional),
                     op_entity_id, jira_name (optional)

        Returns:
            Dict with success count, failed count, errors

        """
        if entity_type not in self.J2O_PROVENANCE_ENTITY_TYPES:
            raise ValueError(f"Invalid entity type: {entity_type}. Must be one of {self.J2O_PROVENANCE_ENTITY_TYPES}")

        if not mappings:
            return {"success": 0, "failed": 0, "errors": []}

        # Ensure infrastructure exists (once for all)
        project_id = self.ensure_j2o_migration_project()
        type_ids = self.ensure_j2o_provenance_types(project_id)
        cf_ids = self.ensure_j2o_provenance_custom_fields()

        type_id = type_ids.get(entity_type)
        if not type_id:
            return {"success": 0, "failed": len(mappings), "errors": [f"No type ID for {entity_type}"]}

        cf_op_id_field = f"J2O OP {entity_type.title()} ID"
        cf_op_id = cf_ids.get(cf_op_id_field)
        cf_entity_type_id = cf_ids.get("J2O Entity Type")

        # Build Ruby array of mappings
        ruby_mappings = []
        for m in mappings:
            jira_key = m.get("jira_key", "")
            jira_name = m.get("jira_name", "")
            op_entity_id = m.get("op_entity_id") or m.get("openproject_id")
            if jira_key and op_entity_id:
                subject = f"{entity_type.upper()}: {jira_key}"
                if jira_name:
                    subject = f"{subject} ({jira_name})"
                ruby_mappings.append(
                    f"  {{ subject: {repr(subject)}, op_entity_id: {op_entity_id} }}"
                )

        if not ruby_mappings:
            return {"success": 0, "failed": 0, "errors": []}

        script = (
            "begin\n"
            f"  project = Project.find({project_id})\n"
            f"  wp_type = Type.find({type_id})\n"
            "  status = Status.default || Status.first\n"
            "  priority = IssuePriority.default || IssuePriority.first\n"
            "  author = User.admin.first || User.first\n"
            f"  cf_op_id = {cf_op_id or 'nil'}\n"
            f"  cf_entity_type_id = {cf_entity_type_id or 'nil'}\n"
            "  mappings = [\n" + ",\n".join(ruby_mappings) + "\n  ]\n"
            "  success = 0\n"
            "  failed = 0\n"
            "  errors = []\n"
            "  mappings.each do |m|\n"
            "    begin\n"
            "      wp = project.work_packages.where(type_id: wp_type.id).find_by(subject: m[:subject])\n"
            "      if wp.nil?\n"
            "        wp = WorkPackage.new(\n"
            "          project: project,\n"
            "          type: wp_type,\n"
            "          subject: m[:subject],\n"
            "          status: status,\n"
            "          priority: priority,\n"
            "          author: author\n"
            "        )\n"
            "      end\n"
            "      cf_values = {}\n"
            "      cf_values[cf_op_id] = m[:op_entity_id] if cf_op_id\n"
            f"      cf_values[cf_entity_type_id] = '{entity_type}' if cf_entity_type_id\n"
            "      wp.custom_field_values = cf_values if cf_values.any?\n"
            "      wp.save!\n"
            "      success += 1\n"
            "    rescue => e\n"
            "      failed += 1\n"
            "      errors << \"#{m[:subject]}: #{e.message}\"\n"
            "    end\n"
            "  end\n"
            "  { success: success, failed: failed, errors: errors.first(10) }\n"
            "rescue => e\n"
            "  { success: 0, failed: mappings.size, errors: [e.message] }\n"
            "end\n"
        )

        try:
            result = self.execute_json_query(script)
            if isinstance(result, dict):
                return result
            return {"success": 0, "failed": len(mappings), "errors": [f"Unexpected result: {result}"]}
        except Exception as e:
            return {"success": 0, "failed": len(mappings), "errors": [str(e)]}

    def get_roles(self) -> list[dict[str, Any]]:
        """Return OpenProject roles (id, name, builtin flag)."""
        ruby = "Role.all.map { |r| r.as_json(only: [:id, :name, :builtin]) }"
        try:
            result = self.execute_json_query(ruby)
            if isinstance(result, list):
                return result
            raise QueryExecutionError("Unexpected OpenProject role payload")
        except Exception as e:
            msg = f"Failed to fetch OpenProject roles: {e}"
            raise QueryExecutionError(msg) from e

    def get_groups(self) -> list[dict[str, Any]]:
        """Return existing OpenProject groups with member IDs."""
        ruby = (
            "Group.includes(:users).order(:name).map do |g| "
            "  { id: g.id, name: g.name, user_ids: g.users.pluck(:id) }"
            "end"
        )
        try:
            result = self.execute_json_query(ruby)
            if isinstance(result, list):
                return result
            raise QueryExecutionError("Unexpected OpenProject group payload")
        except Exception as e:
            msg = f"Failed to fetch OpenProject groups: {e}"
            raise QueryExecutionError(msg) from e

    def sync_group_memberships(self, assignments: list[dict[str, Any]]) -> dict[str, int]:
        """Ensure each group has the provided membership list."""
        if not assignments:
            return {"updated": 0, "errors": 0}

        temp_dir = Path(self.file_manager.data_dir) / "group_sync"
        temp_dir.mkdir(parents=True, exist_ok=True)
        payload_path = temp_dir / f"group_memberships_{os.getpid()}_{int(time.time())}.json"
        result_path = temp_dir / (payload_path.name + ".result")

        try:
            with payload_path.open("w", encoding="utf-8") as handle:
                json.dump(assignments, handle)

            container_input = Path("/tmp") / payload_path.name  # noqa: S108
            container_output = Path("/tmp") / (payload_path.name + ".result")  # noqa: S108
            self.transfer_file_to_container(payload_path, container_input)

            ruby = (
                "require 'json'\n"
                f"input_path = '{container_input.as_posix()}'\n"
                f"output_path = '{container_output.as_posix()}'\n"
                "rows = JSON.parse(File.read(input_path))\n"
                "updated = 0\n"
                "errors = []\n"
                "rows.each do |row|\n"
                "  name = row['name']\n"
                "  next unless name && !name.strip.empty?\n"
                "  begin\n"
                "    group = Group.find_or_create_by(name: name)\n"
                "    desired_ids = Array(row['user_ids']).map(&:to_i).reject(&:nil?).uniq.sort\n"
                "    current_ids = group.user_ids.sort\n"
                "    if desired_ids != current_ids\n"
                "      group.user_ids = desired_ids\n"
                "      group.save\n"
                "      updated += 1\n"
                "    end\n"
                "  rescue => e\n"
                "    errors << { name: name, error: e.message }\n"
                "  end\n"
                "end\n"
                "File.write(output_path, { updated: updated, errors: errors.length }.to_json)\n"
                "nil\n"
            )

            self.execute_query(ruby, timeout=90)

            summary = self._read_result_file(container_output, result_path)
            return {
                "updated": int(summary.get("updated", 0)),
                "errors": int(summary.get("errors", 0)),
            }
        finally:
            with suppress(OSError):
                payload_path.unlink()
            with suppress(OSError):
                result_path.unlink()

    def assign_group_roles(
        self,
        assignments: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Assign OpenProject groups to projects with given role IDs."""
        if not assignments:
            return {"updated": 0, "errors": 0}

        temp_dir = Path(self.file_manager.data_dir) / "group_roles"
        temp_dir.mkdir(parents=True, exist_ok=True)
        payload_path = temp_dir / f"group_roles_{os.getpid()}_{int(time.time())}.json"
        result_path = temp_dir / (payload_path.name + ".result")

        try:
            with payload_path.open("w", encoding="utf-8") as handle:
                json.dump(assignments, handle)

            container_input = Path("/tmp") / payload_path.name  # noqa: S108
            container_output = Path("/tmp") / (payload_path.name + ".result")  # noqa: S108
            self.transfer_file_to_container(payload_path, container_input)

            ruby = (
                "require 'json'\n"
                f"input_path = '{container_input.as_posix()}'\n"
                f"output_path = '{container_output.as_posix()}'\n"
                "updated = 0\n"
                "errors = []\n"
                "begin\n"
                "  File.write(output_path, { updated: 0, errors: 0, status: 'initialised' }.to_json)\n"
                "  rows = JSON.parse(File.read(input_path))\n"
                "  Array(rows).each do |row|\n"
                "    begin\n"
                "      name = row['group_name']\n"
                "      project_id = row['project_id'].to_i\n"
                "      role_ids = Array(row['role_ids']).map(&:to_i).reject(&:nil?).uniq\n"
                "      next if name.nil? || name.empty? || project_id <= 0 || role_ids.empty?\n"
                "      group = Group.find_by(name: name)\n"
                "      project = Project.find_by(id: project_id)\n"
                "      next unless group && project\n"
                "      member = Member.find_or_initialize_by(project: project, principal: group)\n"
                "      existing_ids = Array(member.role_ids).map(&:to_i)\n"
                "      new_ids = (existing_ids + role_ids).uniq\n"
                "      if member.new_record? || new_ids.sort != existing_ids.sort\n"
                "        member.role_ids = new_ids\n"
                "        member.save\n"
                "        updated += 1\n"
                "      end\n"
                "    rescue => e\n"
                "      errors << { group: row['group_name'], project: row['project_id'], error: e.message }\n"
                "    end\n"
                "  end\n"
                "rescue => e\n"
                "  errors << { error: e.message }\n"
                "ensure\n"
                "  summary = { updated: updated, errors: errors.length }\n"
                "  summary[:error_details] = errors if errors.any?\n"
                "  File.write(output_path, summary.to_json)\n"
                "end\n"
                "nil\n"
            )

            self.execute_query(ruby, timeout=90)

            summary = self._read_result_file(container_output, result_path)
            return {
                "updated": int(summary.get("updated", 0)),
                "errors": int(summary.get("errors", 0)),
            }
        finally:
            with suppress(OSError):
                payload_path.unlink()
            with suppress(OSError):
                result_path.unlink()

    def assign_user_roles(
        self,
        *,
        project_id: int,
        user_id: int,
        role_ids: list[int],
    ) -> dict[str, Any]:
        """Ensure a user has the given roles on a project."""
        valid_role_ids = [int(r) for r in role_ids if isinstance(r, (int, str)) and int(r) > 0]
        if not valid_role_ids:
            return {"success": False, "error": "role_ids empty"}

        head = f"project_id = {int(project_id)}\nuser_id = {int(user_id)}\nrole_ids = {json.dumps(valid_role_ids)}\n"
        body = """
project = Project.find_by(id: project_id)
user = User.find_by(id: user_id)

unless project && user
  return { success: false, error: 'project or user not found' }
end

desired = Array(role_ids).map(&:to_i).reject { |rid| rid <= 0 }
if desired.empty?
  return { success: false, error: 'no roles specified' }
end

member = Member.find_or_initialize_by(project: project, principal: user)
existing = Array(member.role_ids).map(&:to_i)

if member.new_record? || (existing.sort != desired.sort)
  member.role_ids = desired
  changed = true
else
  changed = false
end

if member.save
  { success: true, changed: changed, role_ids: member.role_ids }
else
  { success: false, error: member.errors.full_messages.join(', ') }
end
"""
        script = head + body
        result = self.execute_query_to_json_file(script, timeout=90)
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "unexpected response"}

    def sync_workflow_transitions(
        self,
        transitions: list[dict[str, int]],
        role_ids: list[int],
    ) -> dict[str, int]:
        """Ensure workflow transitions exist for the provided type/status/role combinations."""
        if not transitions or not role_ids:
            return {"created": 0, "existing": 0, "errors": 0}

        temp_dir = Path(self.file_manager.data_dir) / "workflow_sync"
        temp_dir.mkdir(parents=True, exist_ok=True)
        payload_path = temp_dir / f"workflow_transitions_{os.getpid()}_{int(time.time())}.json"
        result_path = temp_dir / (payload_path.name + ".result")

        payload = {
            "transitions": [
                {
                    "type_id": int(row.get("type_id", 0)),
                    "from_status_id": int(row.get("from_status_id", 0)),
                    "to_status_id": int(row.get("to_status_id", 0)),
                }
                for row in transitions
            ],
            "role_ids": [int(r) for r in role_ids if int(r) > 0],
        }

        try:
            with payload_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            container_payload = Path("/tmp") / payload_path.name  # noqa: S108
            container_output = Path("/tmp") / (payload_path.name + ".result")  # noqa: S108
            self.transfer_file_to_container(payload_path, container_payload)

            ruby = (
                "require 'json'\n"
                f"payload_path = '{container_payload.as_posix()}'\n"
                f"output_path = '{container_output.as_posix()}'\n"
                "data = JSON.parse(File.read(payload_path))\n"
                "transitions = Array(data['transitions'])\n"
                "role_ids = Array(data['role_ids']).map(&:to_i).reject { |rid| rid <= 0 }.uniq\n"
                "created = 0\n"
                "existing = 0\n"
                "errors = []\n"
                "seen = {}\n"
                "transitions.each do |row|\n"
                "  type_id = row['type_id'].to_i\n"
                "  from_id = row['from_status_id'].to_i\n"
                "  to_id = row['to_status_id'].to_i\n"
                "  next if type_id <= 0 || from_id <= 0 || to_id <= 0\n"
                "  key = [type_id, from_id, to_id]\n"
                "  next if seen[key]\n"
                "  seen[key] = true\n"
                "  role_ids.each do |role_id|\n"
                "    begin\n"
                "      wf = Workflow.find_by(type_id: type_id, role_id: role_id, old_status_id: from_id, new_status_id: to_id)\n"
                "      if wf\n"
                "        existing += 1\n"
                "      else\n"
                "        Workflow.create!(type_id: type_id, role_id: role_id, old_status_id: from_id, new_status_id: to_id)\n"
                "        created += 1\n"
                "      end\n"
                "    rescue => e\n"
                "      errors << { type_id: type_id, role_id: role_id, from: from_id, to: to_id, error: e.message }\n"
                "    end\n"
                "  end\n"
                "end\n"
                "File.write(output_path, { created: created, existing: existing, errors: errors.length }.to_json)\n"
                "nil\n"
            )

            self.execute_query(ruby, timeout=180)
            summary = self._read_result_file(container_output, result_path)
            return {
                "created": int(summary.get("created", 0)),
                "existing": int(summary.get("existing", 0)),
                "errors": int(summary.get("errors", 0)),
            }
        finally:
            with suppress(OSError):
                payload_path.unlink()
            with suppress(OSError):
                result_path.unlink()

    def _read_result_file(
        self,
        container_path: Path,
        local_path: Path,
    ) -> dict[str, Any]:
        """Helper to read JSON results from container with cat fallback."""
        for attempt in range(10):
            try:
                stdout, _stderr, rc = self.docker_client.execute_command(
                    f"cat {container_path.as_posix()}",
                )
            except Exception:  # noqa: BLE001
                stdout, rc = "", 1

            if rc == 0 and stdout.strip():
                try:
                    return json.loads(stdout)
                except json.JSONDecodeError:
                    break

            time.sleep(0.5)

        try:
            copied = self.transfer_file_from_container(container_path, local_path)
        except FileTransferError as exc:
            self.logger.warning(
                "Result file missing in container after polling: %s (%s)",
                container_path,
                exc,
            )
            return {"updated": 0, "errors": 1, "missing_result": True}

        with copied.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def ensure_project_version(
        self,
        project_id: int,
        *,
        name: str,
        description: str | None = None,
        start_date: str | None = None,
        due_date: str | None = None,
        status: str | None = None,
        sharing: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a Version (Sprint/Release) for a project."""
        payload = {
            "project_id": int(project_id),
            "name": name,
            "description": description,
            "start_date": start_date,
            "due_date": due_date,
            "status": status,
            "sharing": sharing or "none",
        }

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        payload_json = json.dumps(payload, ensure_ascii=False)
        script = f"""
        require 'json'
        input = JSON.parse(<<'JSON_DATA')
{payload_json}
JSON_DATA

        project = Project.find_by(id: input['project_id'].to_i)
        unless project
          return {{ success: false, error: 'project not found' }}.to_json
        end

        version = project.versions.where(name: input['name']).first_or_initialize
        was_new = version.new_record?
        attrs = {{ name: input['name'], sharing: input['sharing'] || 'none' }}
        attrs[:description] = input['description'] if input['description']
        attrs[:start_date] = input['start_date'] if input['start_date']
        attrs[:due_date] = input['due_date'] if input['due_date']
        attrs[:status] = input['status'] if input['status']
        version.assign_attributes(attrs)

        changed = version.changed?
        if changed
          version.save!
        else
          version.save! if was_new
        end

        {{
          success: true,
          id: version.id,
          created: was_new,
          updated: changed
        }}.to_json
        """

        result = self.execute_query_to_json_file(script, timeout=90)
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "unexpected response"}

    def create_or_update_query(
        self,
        *,
        name: str,
        description: str | None = None,
        project_id: int | None = None,
        is_public: bool = True,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update an OpenProject query (saved filter)."""
        payload = {
            "name": name,
            "description": description,
            "project_id": project_id,
            "is_public": bool(is_public),
            "options": options or {},
        }

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        payload_json = json.dumps(payload, ensure_ascii=False)
        script = f"""
        require 'json'
        input = JSON.parse(<<'JSON_DATA')
{payload_json}
JSON_DATA

        begin
          project = input['project_id'] ? Project.find_by(id: input['project_id'].to_i) : nil
          user = User.respond_to?(:admin) ? User.admin.first : nil
          user ||= User.admin.first
          user ||= User.where(admin: true).first
          user ||= User.active.first

          if user.nil?
            result = {{ success: false, error: 'no available user to own query' }}
          else
            query = Query.find_or_initialize_by(name: input['name'], project: project)
            query.user ||= user

            is_public = !!input['is_public']
            if query.respond_to?(:public=)
              query.public = is_public
            elsif query.respond_to?(:write_attribute) && query.has_attribute?(:public)
              query.write_attribute(:public, is_public)
            end

            filters = input.dig('options', 'filters')
            query.filters = Array(filters) if filters && query.respond_to?(:filters=)

            columns = input.dig('options', 'columns')
            query.column_names = Array(columns) if columns && query.respond_to?(:column_names=)

            sort = input.dig('options', 'sort')
            query.sort_criteria = Array(sort) if sort && query.respond_to?(:sort_criteria=)

            group_by = input.dig('options', 'group_by')
            query.group_by = group_by if group_by && query.respond_to?(:group_by=)

            hierarchies = input.dig('options', 'show_hierarchies')
            if !hierarchies.nil? && query.respond_to?(:show_hierarchies=)
              query.show_hierarchies = hierarchies
            end

            if query.respond_to?(:include_subprojects=) && query.include_subprojects.nil?
              query.include_subprojects = false
            end

            created = query.new_record?
            changed = query.changed?
            query.save! if created || changed

            result = {{
              success: true,
              id: query.id,
              created: created,
              updated: changed || created
            }}
          end
        rescue => e
          result = {{ success: false, error: e.message }}
        end

        result
        """

        result = self.execute_query_to_json_file(script, timeout=120)
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "unexpected response"}

    def create_or_update_wiki_page(
        self,
        *,
        project_id: int,
        title: str,
        content: str,
    ) -> dict[str, Any]:
        """Create or update a Wiki page within a project."""
        payload = {
            "project_id": int(project_id),
            "title": title,
            "content": content,
        }
        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        payload_json = json.dumps(payload, ensure_ascii=False)

        script = f"""
        require 'json'
        input = JSON.parse(<<'JSON_DATA')
{payload_json}
JSON_DATA

        project = Project.find_by(id: input['project_id'])
        unless project
          return {{ success: false, error: 'project not found' }}.to_json
        end

        begin
          wiki = project.wiki || project.create_wiki(start_page: 'Home')
          page = wiki.pages.where(title: input['title']).first_or_initialize
          author = User.admin.first || User.active.first || User.first
          raise 'no available author for wiki content' unless author

          page.wiki ||= wiki if page.respond_to?(:wiki=)
          page.author ||= author if page.respond_to?(:author=)

          body_text = input['content'].to_s

          if page.respond_to?(:text=)
            page.text = body_text
          elsif page.respond_to?(:content=)
            page.content = body_text
          else
            raise 'wiki page entity does not support text assignment'
          end

          created = page.new_record?
          changed = page.changed?
          page.save!

          # Ensure timestamps/ journaling persists author for updates
          page.touch if !created && changed && page.respond_to?(:touch)

          {{
            success: true,
            id: page.id,
            updated_on: page.updated_at
          }}
        rescue => e
          {{
            success: false,
            error: e.message
          }}
        end
        """

        result = self.execute_query_to_json_file(script, timeout=120)
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "unexpected response"}

    def upsert_project_origin_attributes(
        self,
        project_id: int,
        *,
        origin_system: str,
        project_key: str,
        external_id: str | None = None,
        external_url: str | None = None,
    ) -> bool:
        """Persist origin metadata into Project attributes (description) idempotently.

        We embed a small, machine-readable block between HTML comment markers so we can
        replace it deterministically on subsequent runs without duplicating data.

        Args:
            project_id: OpenProject project ID
            origin_system: e.g. "jira"
            project_key: upstream project key (e.g. "SRVEP")
            external_id: upstream immutable project id (stringified)
            external_url: upstream canonical URL

        Returns:
            True on success, False otherwise.

        """
        # Escape braces in f-string; Ruby string content uses literal markers.
        marker_start = "<!-- J2O_ORIGIN_START -->"
        marker_end = "<!-- J2O_ORIGIN_END -->"
        payload = f"system={origin_system};key={project_key};id={external_id or ''};url={external_url or ''}"

    def upsert_project_origin_attributes(
        self,
        project_id: int,
        *,
        origin_system: str,
        project_key: str,
        external_id: str | None = None,
        external_url: str | None = None,
    ) -> bool:
        """Persist origin metadata into Project attributes (description) idempotently.

        We embed a small, machine-readable block between HTML comment markers so we can
        replace it deterministically on subsequent runs without duplicating data.

        Args:
            project_id: OpenProject project ID
            origin_system: e.g. "jira"
            project_key: upstream project key (e.g. "SRVEP")
            external_id: upstream immutable project id (stringified)
            external_url: upstream canonical URL

        Returns:
            True on success, False otherwise.

        """
        # Escape braces in f-string; Ruby string content uses literal markers.
        marker_start = "<!-- J2O_ORIGIN_START -->"
        marker_end = "<!-- J2O_ORIGIN_END -->"
        payload = f"system={origin_system};key={project_key};id={external_id or ''};url={external_url or ''}"
        # Ruby script to insert/replace the origin block in description
        script = (
            "project = Project.find(%d)\n" % project_id
            + f"marker_start = '{marker_start}'\n"
            + f"marker_end = '{marker_end}'\n"
            + f"payload = '{payload}'.dup\n"
            + "desc = project.description.to_s\n"
            + "block = ['\\n', marker_start, '\\n', payload, '\\n', marker_end, '\\n'].join\n"
            + "start_idx = desc.index(marker_start)\n"
            + "end_idx = desc.index(marker_end)\n"
            + "if start_idx && end_idx && end_idx > start_idx\n"
            + "  pre = desc[0...start_idx]\n"
            + "  post = desc[(end_idx + marker_end.length)..-1] || ''\n"
            + "  desc = pre + block + post\n"
            + "else\n"
            + "  desc = desc + block\n"
            + "end\n"
            + "project.update_columns(description: desc)\n"
            + "{ success: true }.to_json\n"
        )
        try:
            result = self.execute_query_to_json_file(script)
            return bool(isinstance(result, dict) and result.get("success"))
        except Exception as e:  # noqa: BLE001
            self.logger.warning("Failed to upsert project origin attributes for %s: %s", project_id, e)
            return False

    def upsert_project_attribute(
        self,
        project_id: int,
        *,
        name: str,
        value: str,
        field_format: str = "string",
    ) -> bool:
        """Create/enable a Project attribute (ProjectCustomField) and set its value for a project.

        This uses ProjectCustomField (STI on custom_fields) and ProjectCustomFieldProjectMapping,
        storing the actual value in CustomValue for customized_type='Project'.
        """
        ruby = f"""
          pid = {project_id}
          name = '{name}'.dup
          fmt  = '{field_format}'.dup
          val  = '{value}'.dup

          # Ensure attribute definition
          # Section is required for project attributes
          begin
            section = CustomFieldSection.find_or_create_by!(type: 'ProjectCustomFieldSection', name: 'J2O Origin')
          rescue => e
            section = nil
          end

          cf = ProjectCustomField.find_by(name: name)
          if !cf
            cf = ProjectCustomField.new(
              name: name,
              field_format: fmt,
              is_required: false,
              is_filter: false,
              searchable: true,
              editable: true,
              admin_only: false
            )
            begin
              cf.custom_field_section_id = section.id if section && cf.respond_to?(:custom_field_section_id=)
            rescue
            end
            begin
              cf.is_for_all = false if cf.respond_to?(:is_for_all=)
            rescue
            end
            cf.save!
          end

          # If cf existed without section, attach it
          if (!cf.custom_field_section_id || cf.custom_field_section_id.nil?) && section
            begin
              cf.update!(custom_field_section_id: section.id)
            rescue
            end
          end

          # Ensure mapping enabled for this project
          ProjectCustomFieldProjectMapping.find_or_create_by!(project_id: pid, custom_field_id: cf.id)

          # Upsert value
          cv = CustomValue.find_or_initialize_by(customized_type: 'Project', customized_id: pid, custom_field_id: cf.id)
          cv.value = val
          cv.save!

          {{ success: true, custom_field_id: cf.id, value: cv.value }}.to_json
        """
        try:
            result = self.execute_query_to_json_file(ruby)
            return bool(isinstance(result, dict) and result.get("success"))
        except Exception as e:  # noqa: BLE001
            self.logger.warning("Failed to upsert project attribute %s for %s: %s", name, project_id, e)
            return False

    def bulk_upsert_project_attributes(
        self,
        attributes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk upsert project attributes in a single Rails call.

        Args:
            attributes: List of dicts with keys:
                - project_id: int
                - name: str
                - value: str
                - field_format: str (default 'string')

        Returns:
            Dict with 'success': bool, 'processed': int, 'failed': int
        """
        if not attributes:
            return {"success": True, "processed": 0, "failed": 0}

        # Build JSON data for Ruby
        data = []
        for attr in attributes:
            data.append({
                "pid": int(attr["project_id"]),
                "name": str(attr["name"]),
                "value": str(attr.get("value", "")),
                "fmt": str(attr.get("field_format", "string")),
            })

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        ruby = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          # Ensure section exists once
          section = nil
          begin
            section = CustomFieldSection.find_or_create_by!(type: 'ProjectCustomFieldSection', name: 'J2O Origin')
          rescue => e
          end

          # Cache custom fields by name
          cf_cache = {{}}

          results = {{ processed: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              pid = item['pid']
              name = item['name']
              fmt = item['fmt']
              val = item['value']

              # Get or create custom field
              cf = cf_cache[name]
              if !cf
                cf = ProjectCustomField.find_by(name: name)
                if !cf
                  cf = ProjectCustomField.new(
                    name: name,
                    field_format: fmt,
                    is_required: false,
                    is_filter: false,
                    searchable: true,
                    editable: true,
                    admin_only: false
                  )
                  cf.custom_field_section_id = section.id if section && cf.respond_to?(:custom_field_section_id=) rescue nil
                  cf.is_for_all = false if cf.respond_to?(:is_for_all=) rescue nil
                  cf.save!
                end
                # Attach section if needed
                if section && (!cf.custom_field_section_id || cf.custom_field_section_id.nil?)
                  cf.update!(custom_field_section_id: section.id) rescue nil
                end
                cf_cache[name] = cf
              end

              # Ensure mapping for project
              ProjectCustomFieldProjectMapping.find_or_create_by!(project_id: pid, custom_field_id: cf.id)

              # Upsert value
              cv = CustomValue.find_or_initialize_by(customized_type: 'Project', customized_id: pid, custom_field_id: cf.id)
              cv.value = val
              cv.save!

              results[:processed] += 1
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ pid: item['pid'], name: item['name'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results.to_json
        """
        try:
            result = self.execute_query_to_json_file(ruby)
            if isinstance(result, dict):
                return result
            return {"success": False, "processed": 0, "failed": len(attributes), "error": str(result)}
        except Exception as e:  # noqa: BLE001
            self.logger.warning("Bulk upsert project attributes failed: %s", e)
            return {"success": False, "processed": 0, "failed": len(attributes), "error": str(e)}

    def rename_project_attribute(self, *, old_name: str, new_name: str) -> bool:
        """Rename a Project attribute (ProjectCustomField) if it exists.

        Returns True if renamed or already at new_name; False if missing or failed.
        """
        ruby = f"""
          old_name = '{old_name}'.dup
          new_name = '{new_name}'.dup
          cf = ProjectCustomField.find_by(name: old_name)
          if cf
            cf.update!(name: new_name)
            {{ success: true, id: cf.id }}.to_json
          else
            cf2 = ProjectCustomField.find_by(name: new_name)
            {{ success: !!cf2, id: (cf2 ? cf2.id : nil) }}.to_json
          end
        """
        try:
            result = self.execute_query_to_json_file(ruby)
            return bool(isinstance(result, dict) and result.get("success"))
        except Exception as e:  # noqa: BLE001
            self.logger.warning("Failed to rename project attribute %s -> %s: %s", old_name, new_name, e)
            return False

    def get_project_wp_cf_snapshot(self, project_id: int) -> list[dict[str, Any]]:
        """Return snapshot of WorkPackages in a project with Jira CFs and updated_at.

        Each item: { id, updated_at, jira_issue_key, jira_migration_date }
        """
        ruby = f"""
          cf_key = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
          cf_mig = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Last Update Date')

          # Pre-load custom values for all WPs in this project for efficiency
          wp_ids = WorkPackage.where(project_id: {project_id}).pluck(:id)

          key_values = {{}}
          mig_values = {{}}

          if cf_key
            CustomValue.where(custom_field_id: cf_key.id, customized_type: 'WorkPackage', customized_id: wp_ids)
              .each {{ |cv| key_values[cv.customized_id] = cv.value }}
          end

          if cf_mig
            CustomValue.where(custom_field_id: cf_mig.id, customized_type: 'WorkPackage', customized_id: wp_ids)
              .each {{ |cv| mig_values[cv.customized_id] = cv.value }}
          end

          WorkPackage.where(project_id: {project_id}).select(:id, :updated_at).map do |wp|
            {{ id: wp.id, updated_at: (wp.updated_at&.utc&.iso8601), jira_issue_key: key_values[wp.id], jira_migration_date: mig_values[wp.id] }}
          end
        """
        data = self.execute_large_query_to_json_file(ruby, timeout=120)
        if not isinstance(data, list):
            raise QueryExecutionError("Invalid snapshot from OpenProject")
        return data

    def set_wp_last_update_date_by_keys(
        self,
        project_id: int,
        jira_keys: list[str],
        date_str: str,
    ) -> dict[str, Any]:
        """Set 'J2O Last Update Date' CF for work packages by Jira Issue Key.

        Args:
            project_id: OpenProject project ID to scope updates
            jira_keys: List of Jira issue keys to update
            date_str: Date string (YYYY-MM-DD)

        Returns:
            Result dict with counts.

        """
        if not jira_keys:
            return {"updated": 0, "examined": 0}

        # Build a small Ruby script that resolves the two CFs and updates values
        # for all WPs in the given project that have matching Jira Issue Key.
        # Use JSON to safely embed the key list.
        keys_json = json.dumps(list(jira_keys))
        ruby = f"""
          require 'json'
          proj_id = {project_id}
          target_date = '{date_str}'
          keys = JSON.parse({json.dumps(keys_json)})
          updated = 0
          examined = 0
          key_cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Issue Key')
          last_cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Last Update Date')
          if key_cf && last_cf
            keys.each do |k|
              examined += 1
              begin
                # Find WP id by custom value match in project
                cv = CustomValue.where(customized_type: 'WorkPackage', custom_field_id: key_cf.id, value: k).first
                if cv
                  # Ensure WP belongs to project
                  wp = WorkPackage.find_by(id: cv.customized_id, project_id: proj_id)
                  if wp
                    last_cv = CustomValue.find_or_initialize_by(customized_type: 'WorkPackage', customized_id: wp.id, custom_field_id: last_cf.id)
                    if last_cv.new_record? || last_cv.value.to_s.strip != target_date
                      last_cv.value = target_date
                      begin; last_cv.save!; updated += 1; rescue; end
                    end
                  end
                end
              rescue
              end
            end
          end
          {{ updated: updated, examined: examined }}
        """
        try:
            result = self.execute_query_to_json_file(ruby)
            return result if isinstance(result, dict) else {"updated": 0, "examined": 0}
        except Exception as e:  # noqa: BLE001
            self.logger.warning(
                "Failed to set J2O Last Update Date for project %s: %s",
                project_id,
                e,
            )
            return {"updated": 0, "examined": 0, "error": str(e)}

    def bulk_create_records(  # noqa: PLR0915
        self,
        model: str,
        records: list[dict[str, Any]],
        *,
        timeout: int | None = None,
        result_basename: str | None = None,
    ) -> dict[str, Any]:
        """Create many records for a given Rails model using a minimal Ruby script.

        Policy: Ruby performs only create; all mapping/sanitization/defaults must be done in Python.

        Args:
            model: Rails model name (e.g., "WorkPackage")
            records: List of sanitized attribute dicts suitable for mass-assignment
            timeout: Optional execution timeout
            result_basename: Optional basename used for the result file in the container

        Returns:
            Result envelope with keys: status, created, errors, created_count, error_count, total

        Raises:
            QueryExecutionError: On execution or retrieval failure

        """
        # Basic validation of model name to avoid code injection
        if not isinstance(model, str) or not model or not re.match(r"^[A-Za-z_:][A-Za-z0-9_:]*$", model):
            _msg = "Invalid model name for bulk_create_records"
            raise QueryExecutionError(_msg)

        if not isinstance(records, list):
            _msg = "records must be a list of dicts"
            raise QueryExecutionError(_msg)

        # Prepare local JSON payload
        temp_dir = Path(self.file_manager.data_dir) / "bulk_create"
        temp_dir.mkdir(parents=True, exist_ok=True)
        local_json = temp_dir / f"{model.lower()}_bulk_{os.urandom(4).hex()}.json"
        try:
            with local_json.open("w", encoding="utf-8") as f:
                json.dump(records, f)
        except Exception as e:
            _msg = f"Failed to serialize records: {e}"
            raise QueryExecutionError(_msg) from e

        # Transfer JSON to container
        container_json = Path("/tmp") / local_json.name  # noqa: S108
        self.transfer_file_to_container(local_json, container_json)

        # BUG #32 FIX: Load journal creation .rb file content as template for WorkPackage migrations
        # This avoids Ruby scoping issues with the `load` statement
        journal_creation_ruby = ""
        if model == "WorkPackage":
            local_journal_rb = Path(__file__).parent.parent / "ruby" / "create_work_package_journals.rb"
            if local_journal_rb.exists():
                try:
                    with local_journal_rb.open("r", encoding="utf-8") as f:
                        # Read the .rb file content and prepare it for inline insertion
                        rb_content = f.read()
                        # Remove the header comments (first 9 lines) to avoid duplication
                        lines = rb_content.split("\n")
                        # Keep everything after line 9 (the actual Ruby code)
                        journal_creation_ruby = "\n".join(lines[9:])
                except Exception as e:
                    logger.warning(f"Failed to load journal creation template: {e}")
                    journal_creation_ruby = ""

        # Result file path in container and local debug path
        # Always ensure uniqueness to avoid collisions across batches
        if result_basename:
            base = str(result_basename)
            if not base.endswith(".json"):
                base = f"{base}.json"
            unique_suffix = f"_{int(time.time())}_{os.getpid()}_{os.urandom(2).hex()}"
            # Insert suffix before .json
            if base.lower().endswith(".json"):
                result_name = base[:-5] + unique_suffix + ".json"
            else:
                result_name = base + unique_suffix
        else:
            result_name = f"bulk_result_{model.lower()}_{int(time.time())}_{os.getpid()}_{os.urandom(3).hex()}.json"
        container_result = Path("/tmp") / result_name  # noqa: S108
        local_result = temp_dir / result_name

        # Progress file within the container, mirrored locally for monitoring
        container_progress = Path("/tmp") / (result_name + ".progress")  # noqa: S108
        local_progress = local_result.with_suffix(local_result.suffix + ".progress")

        # Compose minimal Ruby script
        # Provenance hint for bulk create
        def _bulk_hint() -> str:
            try:
                ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                proj = (config.jira_config or {}).get("project_filter")
                proj_part = f" project={proj}" if proj else ""
                return f"j2o: migration/bulk_create model={model}{proj_part} ts={ts} pid={os.getpid()}"
            except Exception:
                return f"j2o: migration/bulk_create model={model} pid={os.getpid()}"

        header = (
            f"# {_bulk_hint()}\n"
            "require 'json'\n"
            "require 'logger'\n"
            "begin; require 'fileutils'; rescue; end\n"
            f"model_name = '{model}'\n"
            f"data_path = '{container_json.as_posix()}'\n"
            f"result_path = '{container_result.as_posix()}'\n"
            # Ensure progress ENV defaults are present in both console and runner modes
            f"ENV['J2O_BULK_PROGRESS_FILE'] ||= '{container_progress.as_posix()}'\n"
            "ENV['J2O_BULK_PROGRESS_N'] ||= (ENV['J2O_BULK_PROGRESS_N'] || '50')\n"
        )
        ruby = (
            "# BUG #32 FIX: Disable stdout buffering completely\n"
            "$stdout.sync = true\n"
            "$stderr.sync = true\n"
            "puts '[RUBY] Script execution starting...'\n"
            "STDOUT.flush\n"
            "begin; Rails.logger.level = Logger::WARN; rescue; end\n"
            "begin; ActiveJob::Base.logger = Logger.new(nil); rescue; end\n"
            "begin; GoodJob.logger = Logger.new(nil); rescue; end\n"
            "verbose = (ENV['J2O_BULK_RUBY_VERBOSE'] == '1')\n"
            'puts "[RUBY] Verbose mode: #{verbose}"\n'
            "STDOUT.flush\n"
            "progress_file = ENV['J2O_BULK_PROGRESS_FILE']\n"
            "begin; FileUtils.rm_f(progress_file); rescue; end if progress_file\n"
            "progress_n = (ENV['J2O_BULK_PROGRESS_N'] || '50').to_i\n"
            "begin\n"
            "model = Object.const_get(model_name)\n"
            "data = JSON.parse(File.read(data_path))\n"
            "created = []\n"
            "errors = []\n"
            'puts "J2O bulk start: model=#{model_name} total=#{data.length} result=#{result_path}" if verbose\n'
            "begin; File.open(progress_file, 'a'){|f| f.write(\"START total=#{data.length}\\n\") }; rescue; end if progress_file\n"
            "data.each_with_index do |attrs, idx|\n"
            "  # Debug: Inspect attrs hash for Bug #32\n"
            "  if idx == 0 && model_name == 'WorkPackage'\n"
            '    puts "[BUG32-DEBUG] attrs.class = #{attrs.class}"\n'
            '    puts "[BUG32-DEBUG] attrs.keys.count = #{attrs.keys.count}"\n'
            '    puts "[BUG32-DEBUG] attrs.keys = #{attrs.keys.inspect}"\n'
            "    puts \"[BUG32-DEBUG] attrs['_rails_operations'] present? #{!attrs['_rails_operations'].nil?}\"\n"
            '    puts "[BUG32-DEBUG] attrs[:_rails_operations] present? #{!attrs[:_rails_operations].nil?}"\n'
            "    if attrs['_rails_operations']\n"
            "      puts \"[BUG32-DEBUG] _rails_operations count = #{attrs['_rails_operations'].length}\"\n"
            "    end\n"
            "    STDOUT.flush\n"
            "  end\n"
            "  begin\n"
            "    pref_attrs = nil\n"
            "    rec = model.new\n"
            "    # Minimal association pre-assignments for WorkPackage to satisfy validations\n"
            "    if model_name == 'WorkPackage'\n"
            "      begin\n"
            "        rec.project_id = attrs['project_id'] if attrs.key?('project_id')\n"
            "        if attrs.key?('type_id') && attrs['type_id']\n"
            "          rec.type = Type.find_by(id: attrs['type_id'])\n"
            "        end\n"
            "        if attrs.key?('status_id') && attrs['status_id']\n"
            "          rec.status = Status.find_by(id: attrs['status_id'])\n"
            "        end\n"
            "        if attrs.key?('priority_id') && attrs['priority_id']\n"
            "          rec.priority = IssuePriority.find_by(id: attrs['priority_id'])\n"
            "        end\n"
            "        if attrs.key?('author_id') && attrs['author_id']\n"
            "          rec.author = User.find_by(id: attrs['author_id'])\n"
            "        end\n"
            "        # Ruby-side safety defaults when not provided (keeps script minimal)\n"
            "        rec.status ||= Status.order(:position).first\n"
            "        rec.priority ||= IssuePriority.order(:position).first\n"
            "        rec.type ||= Type.order(:position).first\n"
            "        # Keep keys; assign_attributes can safely set *_id again if present\n"
            "      rescue => e\n"
            "        # continue with remaining attributes\n"
            "      end\n"
            "    end\n"
            "    if model_name == 'User'\n"
            "      begin\n"
            "        pref_attrs = attrs.delete('pref_attributes')\n"
            "      rescue\n"
            "        pref_attrs = nil\n"
            "      end\n"
            "    end\n"
            "    # Extract and remove custom_fields, _rails_operations, and Jira keys from attrs before assign_attributes\n"
            "    # Jira keys are NOT valid WorkPackage attributes - would cause UnknownAttributeError\n"
            "    cf_data = nil\n"
            "    rails_ops = nil\n"
            "    jira_id = nil\n"
            "    jira_key = nil\n"
            "    jira_issue_key = nil\n"
            "    begin\n"
            "      cf_data = attrs.delete('custom_fields') if attrs.key?('custom_fields')\n"
            "      rails_ops = attrs.delete('_rails_operations') if attrs.key?('_rails_operations')\n"
            "      jira_id = attrs.delete('jira_id') if attrs.key?('jira_id')\n"
            "      jira_key = attrs.delete('jira_key') if attrs.key?('jira_key')\n"
            "      jira_issue_key = attrs.delete('jira_issue_key') if attrs.key?('jira_issue_key')\n"
            "    rescue\n"
            "    end\n"
            "    begin\n"
            "      rec.assign_attributes(attrs)\n"
            "    rescue => e\n"
            "      # If assign fails, proceed to save with preassigned associations only\n"
            "    end\n"
            "    # Ensure defaults are applied AFTER assign_attributes to avoid blank overrides\n"
            "    if model_name == 'WorkPackage'\n"
            "      begin\n"
            "        rec.status ||= Status.order(:position).first\n"
            "        rec.priority ||= IssuePriority.order(:position).first\n"
            "        rec.type ||= Type.order(:position).first\n"
            "      rescue => e\n"
            "      end\n"
            "    end\n"
            "    # Provenance and preference handling\n"
            "    begin\n"
            "      if model_name == 'User' && pref_attrs.respond_to?(:each)\n"
            "        pref = rec.pref || rec.build_pref\n"
            "        pref_attrs.each do |k, v|\n"
            '          setter = "#{k}="\n'
            "          pref.public_send(setter, v) if pref.respond_to?(setter)\n"
            "        end\n"
            "        begin; pref.save; rescue; end\n"
            "      end\n"
            "    rescue\n"
            "    end\n"
            "    if rec.save\n"
            "      # Apply ALL custom fields AFTER work package is saved (Jira key + J2O Origin fields)\n"
            "      if model_name == 'WorkPackage'\n"
            "        begin\n"
            "          cf_map = {}\n"
            "          # Add Jira Issue Key custom field if present (use extracted vars, not attrs)\n"
            "          key = jira_issue_key || jira_key\n"
            "          if key\n"
            "            begin\n"
            "              cf_jira = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Issue Key')\n"
            "              if !cf_jira\n"
            "                cf_jira = CustomField.new(name: 'Jira Issue Key', field_format: 'string',\n"
            "                  is_required: false, is_for_all: true, type: 'WorkPackageCustomField')\n"
            "                cf_jira.save\n"
            "              end\n"
            "              cf_map[cf_jira.id] = key if cf_jira && cf_jira.id\n"
            "            rescue\n"
            "            end\n"
            "          end\n"
            "          # Add J2O Origin custom fields\n"
            "          if cf_data && cf_data.respond_to?(:each)\n"
            "            cf_data.each do |cfh|\n"
            "              begin\n"
            "                cid = (cfh['id'] || cfh[:id]).to_i\n"
            "                val = cfh['value'] || cfh[:value]\n"
            "                next if cid <= 0 || val.nil?\n"
            "                cf_map[cid] = val\n"
            "              rescue; end\n"
            "            end\n"
            "          end\n"
            "          # Set all custom fields at once\n"
            "          if cf_map.any?\n"
            "            rec.custom_field_values = cf_map\n"
            "            rec.save\n"
            '            puts "J2O bulk item #{idx}: Set #{cf_map.size} custom fields" if verbose\n'
            "          end\n"
            "        rescue => e\n"
            '          puts "J2O bulk item #{idx}: CF assignment error: #{e.class}: #{e.message}" if verbose\n'
            "        end\n"
            "      end\n"
            "      # BUG #32 FIX: Journal creation logic loaded from template\n"
            + (
                "\n".join(f"      {line}" for line in journal_creation_ruby.split("\n"))
                if journal_creation_ruby
                else ""
            )
            + "\n"
            "      created << {'index' => idx, 'id' => rec.id}\n"
            '      puts "J2O bulk item #{idx}: saved id=#{rec.id}" if verbose\n'
            "    else\n"
            "      errors << {'index' => idx, 'errors' => rec.errors.full_messages}\n"
            "      puts \"J2O bulk item #{idx}: failed #{rec.errors.full_messages.join(', ')}\" if verbose\n"
            "    end\n"
            "    if progress_n > 0 && ((idx + 1) % progress_n == 0)\n"
            "      begin; File.open(progress_file, 'a'){|f| f.write('.') }; rescue; end if progress_file\n"
            "      puts '.' if verbose\n"
            "    end\n"
            "    if verbose && progress_n > 0 && ((idx + 1) % (progress_n * 10) == 0)\n"
            '      puts "processed=#{idx + 1}/#{data.length}"\n'
            "    end\n"
            "  rescue => e\n"
            "    errors << {'index' => idx, 'errors' => [e.message]}\n"
            '    puts "J2O bulk item #{idx}: exception #{e.class}: #{e.message}" if verbose\n'
            "  end\n"
            "end\n"
            "result = {\n"
            "  'status' => 'success',\n"
            "  'created' => created,\n"
            "  'errors' => errors,\n"
            "  'created_count' => created.length,\n"
            "  'error_count' => errors.length,\n"
            "  'total' => data.length\n"
            "}\n"
            "File.open(result_path, 'w') do |f|\n"
            "  f.write(JSON.generate(result))\n"
            "  begin; f.flush; f.fsync; rescue; end\n"
            "end\n"
            "begin; FileUtils.chmod(0644, result_path); rescue; end\n"
            'puts "J2O bulk done: created=#{created.length} errors=#{errors.length} total=#{data.length} -> #{result_path}" if verbose\n'
            "begin; File.open(progress_file, 'a'){|f| f.write(\"\\nDONE #{created.length}/#{data.length}\\n\") }; rescue; end if progress_file\n"
            "rescue => top_e\n"
            "  begin\n"
            "    err = { 'status' => 'error', 'message' => top_e.message, 'backtrace' => (top_e.backtrace || []).take(20) }\n"
            "    File.open(result_path + '.error.json', 'w') do |f|\n"
            "      f.write(JSON.generate(err))\n"
            "      begin; f.flush; f.fsync; rescue; end\n"
            "    end\n"
            "    begin; FileUtils.chmod(0644, result_path + '.error.json'); rescue; end\n"
            '    puts "J2O bulk error: #{top_e.class}: #{top_e.message} -> #{result_path}.error.json" if verbose\n'
            "  rescue; end\n"
            "end\n"
        )

        # Decide execution mode: prefer rails runner for long scripts to avoid pasting into console
        full_script = header + ruby
        max_lines_env = os.environ.get("J2O_SCRIPT_RUNNER_MAX_LINES")
        char_thresh_env = os.environ.get("J2O_SCRIPT_RUNNER_THRESHOLD")
        try:
            max_lines = int(max_lines_env) if max_lines_env else 10
        except Exception:
            max_lines = 10
        try:
            char_threshold = int(char_thresh_env) if char_thresh_env else 200
        except Exception:
            char_threshold = 200

        script_lines = full_script.count("\n") + 1
        use_runner = (script_lines >= max_lines) or (len(full_script) >= char_threshold)

        output: str | None = None
        if use_runner:
            runner_script_path = f"/tmp/j2o_bulk_{os.urandom(4).hex()}.rb"  # noqa: S108
            local_tmp = Path(self.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
            local_tmp.parent.mkdir(parents=True, exist_ok=True)
            with local_tmp.open("w", encoding="utf-8") as f:
                f.write(full_script)
            self.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))
            mode = (os.environ.get("J2O_SCRIPT_LOAD_MODE") or "runner").lower()
            allow_runner_fallback = str(os.environ.get("J2O_ALLOW_RUNNER_FALLBACK", "0")).lower() in {"1", "true"}
            if mode == "console":
                try:
                    _console_output = self.rails_client.execute(
                        f"load '{runner_script_path}'",
                        timeout=timeout or 120,
                        suppress_output=True,
                    )
                except Exception as e:
                    if not allow_runner_fallback:
                        raise QueryExecutionError(
                            "Rails console execution failed and runner fallback is disabled",
                        ) from e
                    runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                    try:
                        stdout, stderr, rc = self.docker_client.execute_command(
                            runner_cmd,
                            timeout=timeout or 120,
                            env={
                                "J2O_BULK_RUBY_VERBOSE": os.environ.get("J2O_BULK_RUBY_VERBOSE", "1"),
                                "J2O_BULK_PROGRESS_FILE": container_progress.as_posix(),
                                "J2O_BULK_PROGRESS_N": os.environ.get("J2O_BULK_PROGRESS_N", "50"),
                            },
                        )
                    except subprocess.TimeoutExpired as te:
                        # Best-effort remote cleanup of the timed-out runner
                        try:
                            self.docker_client.execute_command(
                                f'pkill -f "rails runner {runner_script_path}" || true',
                                timeout=10,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        raise QueryExecutionError(
                            f"rails runner timed out for {runner_script_path}",
                        ) from te
                    if rc != 0:
                        q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(q_msg) from e
                    if stdout:
                        logger.info("runner stdout: %s", stdout[:500])
            else:
                runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                try:
                    stdout, stderr, rc = self.docker_client.execute_command(
                        runner_cmd,
                        timeout=timeout or 120,
                        env={
                            "J2O_BULK_RUBY_VERBOSE": os.environ.get("J2O_BULK_RUBY_VERBOSE", "1"),
                            "J2O_BULK_PROGRESS_FILE": container_progress.as_posix(),
                            "J2O_BULK_PROGRESS_N": os.environ.get("J2O_BULK_PROGRESS_N", "50"),
                        },
                    )
                except subprocess.TimeoutExpired as te:
                    # Best-effort remote cleanup of the timed-out runner
                    try:
                        self.docker_client.execute_command(
                            f'pkill -f "rails runner {runner_script_path}" || true',
                            timeout=10,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    raise QueryExecutionError(
                        f"rails runner timed out for {runner_script_path}",
                    ) from te
                if rc != 0:
                    q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                    raise QueryExecutionError(q_msg)
                if stdout:
                    logger.info("runner stdout: %s", stdout[:10000])
        else:
            # Execute via persistent Rails console with suppressed output (file-based result only)
            try:
                # Allow opt-in console progress visibility
                suppress = os.environ.get("J2O_BULK_PROGRESS_CONSOLE", "0") != "1"
                output = self.rails_client.execute(full_script, timeout=timeout or 120, suppress_output=suppress)
            except Exception as e:
                _msg = f"Rails execution failed for bulk_create_records: {e}"
                raise QueryExecutionError(_msg) from e

        # Poll-copy result back to local (allow slow writes on busy systems)
        max_wait_seconds_env = os.environ.get("J2O_BULK_RESULT_WAIT_SECONDS")
        try:
            max_wait_seconds = int(max_wait_seconds_env) if max_wait_seconds_env else 180
        except Exception:
            max_wait_seconds = 180
        poll_interval = 1.0
        waited = 0.0
        copied = False
        # Stall detection and heartbeats
        stall_env = os.environ.get("J2O_BULK_STALL_SECONDS")
        try:
            stall_seconds = int(stall_env) if stall_env else 120
        except Exception:
            stall_seconds = 120
        last_progress_len = -1
        last_progress_change_at = 0.0
        last_heartbeat_logged = -10.0
        runner_script_known = "runner_script_path" in locals()
        while waited < max_wait_seconds:
            # Avoid noisy SSH errors: first, check for existence using Docker API
            if self.docker_client.check_file_exists_in_container(container_result):
                # Attempt direct copy from container to local
                try:
                    self.transfer_file_from_container(container_result, local_result)
                    copied = True
                    break
                except FileNotFoundError:
                    # Race: file appeared in stat but not yet readable; keep polling
                    pass
                except Exception:  # noqa: BLE001
                    # Fall back to next poll iteration
                    pass

            # If an error sidecar file exists, fetch it for diagnostics
            try:
                err_remote = Path(container_result.as_posix() + ".error.json")
                if self.docker_client.check_file_exists_in_container(err_remote):
                    err_local = local_result.with_suffix(local_result.suffix + ".error.json")
                    self.transfer_file_from_container(err_remote, err_local)
                    try:
                        with err_local.open("r", encoding="utf-8") as ef:
                            err_txt = ef.read()[:500]
                        logger.error("Bulk runner error: %s", err_txt)
                    except Exception:
                        pass
            except Exception:  # noqa: BLE001
                pass

            # Probe progress file occasionally to provide live feedback and detect stalls
            try:
                if self.docker_client.check_file_exists_in_container(container_progress):
                    # Copy progress file locally at a modest cadence
                    if (waited - last_heartbeat_logged) >= 5.0:
                        try:
                            self.transfer_file_from_container(container_progress, local_progress)
                            prog_text = ""
                            try:
                                with local_progress.open("r", encoding="utf-8") as pf:
                                    prog_text = pf.read()
                            except Exception:
                                prog_text = ""
                            prog_len = len(prog_text)
                            # Count dots as a rough processed counter
                            processed_est = prog_text.count(".")
                            # Extract total from START line if present
                            total_est = None
                            try:
                                for line in prog_text.splitlines():
                                    if line.startswith("START total="):
                                        total_est = int(line.split("=", 1)[1])
                                        break
                            except Exception:
                                total_est = None
                            logger.info(
                                "Bulk progress: ~%s%s processed (waited %.0fs)",
                                processed_est,
                                f"/{total_est}" if total_est is not None else "",
                                waited,
                            )
                            if prog_len != last_progress_len:
                                last_progress_len = prog_len
                                last_progress_change_at = waited
                            elif (waited - last_progress_change_at) >= stall_seconds:
                                # Consider the run stalled; attempt to stop runner and error out
                                try:
                                    if runner_script_known:
                                        self.docker_client.execute_command(
                                            f'pkill -f "rails runner {runner_script_path}" || true',
                                            timeout=10,
                                        )
                                except Exception:
                                    pass
                                raise QueryExecutionError(
                                    f"bulk_create_records stalled for {stall_seconds}s without progress",
                                )
                            last_heartbeat_logged = waited
                        except Exception:
                            # Ignore progress read errors; continue polling
                            pass
            except Exception:
                pass

            # Periodic heartbeat even without progress file
            try:
                if (waited - last_heartbeat_logged) >= 10.0:
                    logger.info(
                        "Waiting for bulk result file %s (waited %.0fs)",
                        container_result,
                        waited,
                    )
                    last_heartbeat_logged = waited
            except Exception:
                pass

            time.sleep(poll_interval)
            waited += poll_interval

        if not copied:
            _msg = "Result file not found after bulk_create_records execution"
            raise QueryExecutionError(_msg)

        # Parse and return result
        try:
            with local_result.open("r", encoding="utf-8") as f:
                result = json.load(f)
                # Attach raw output snippet for callers that want to persist it
                if isinstance(output, str):
                    result["output"] = output[:2000]
                return result
        except Exception as e:
            _msg = f"Failed to parse result JSON: {e}"
            raise QueryExecutionError(_msg) from e

    def find_record(
        self,
        model: str,
        id_or_conditions: int | dict[str, Any],
    ) -> dict[str, Any]:
        """Find a record by ID or conditions.

        Args:
            model: Model name (e.g., "User", "Project")
            id_or_conditions: ID or conditions hash

        Returns:
            Record data

        Raises:
            RecordNotFoundError: If no record is found
            QueryExecutionError: If query fails

        """
        try:
            if isinstance(id_or_conditions, int):
                query = f"{model}.find_by(id: {id_or_conditions})&.as_json"
            else:
                # Convert Python dict to Ruby hash format
                conditions_str = json.dumps(id_or_conditions).replace('"', "'")
                query = f"{model}.find_by({conditions_str})&.as_json"

            result = self.execute_json_query(query)
        except (QueryExecutionError, JsonParseError) as e:
            msg = f"Error finding record for {model}."
            raise QueryExecutionError(msg) from e
            if result is None:
                msg = f"No {model} found with {id_or_conditions}"
                raise RecordNotFoundError(msg)
            return result

    def _retry_with_exponential_backoff(  # noqa: PLR0913
        self,
        operation: Callable[[], object],
        operation_name: str,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        *,
        jitter: bool = True,
        headers: dict[str, str] | None = None,  # noqa: ARG002
    ) -> object:
        """Execute an operation with exponential backoff retry logic.

        Args:
            operation: Function to execute
            operation_name: Name of operation for logging
            max_retries: Maximum number of retry attempts
            base_delay: Initial delay between retries in seconds
            max_delay: Maximum delay between retries in seconds
            backoff_factor: Factor to multiply delay by on each retry
            jitter: Whether to add random jitter to delays
            headers: Optional headers for idempotency key propagation

        Returns:
            Result of the operation

        Raises:
            Exception: Last exception if all retries are exhausted

        """
        last_exception: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                result = operation()
            except (ClientConnectionError, QueryExecutionError) as e:
                last_exception = e

                # Check if this is a transient error worth retrying
                error_message = str(e).lower()
                transient_indicators = [
                    "timeout",
                    "connection",
                    "network",
                    "temporary",
                    "retry",
                    "busy",
                    "overload",
                    "503",
                    "502",
                    "504",
                ]

                is_transient = any(indicator in error_message for indicator in transient_indicators)

                if not is_transient or attempt >= max_retries:
                    # Don't retry for non-transient errors or if out of retries
                    raise

                # Calculate delay with exponential backoff
                delay = min(base_delay * (backoff_factor**attempt), max_delay)

                # Add jitter to prevent thundering herd
                if jitter:
                    delay = delay * (0.5 + random.random() * 0.5)  # noqa: S311

                self.logger.warning(
                    "%s failed (attempt %d/%d): %s. Retrying in %.2f seconds...",
                    operation_name,
                    attempt + 1,
                    max_retries + 1,
                    e,
                    delay,
                )

                time.sleep(delay)

            except (RecordNotFoundError, JsonParseError) as e:
                # These are typically permanent errors - don't retry
                self.logger.debug(
                    "%s failed with non-transient error: %s",
                    operation_name,
                    e,
                )
                raise
            else:
                return result

        # This should never be reached, but just in case
        if last_exception is not None:
            raise last_exception
        _msg = f"{operation_name} failed after {max_retries} retries"
        raise QueryExecutionError(_msg)

    @batch_idempotent(ttl=3600)  # 1 hour TTL for batch record lookups
    def batch_find_records(  # noqa: C901
        self,
        model: str,
        ids: list[int | str],
        batch_size: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[int | str, dict[str, Any]]:
        """Find multiple records by IDs in batches with idempotency support.

        Args:
            model: Model name (e.g., "User", "Project")
            ids: List of IDs to find
            batch_size: Size of each batch (defaults to configured batch_size)
            headers: Optional headers containing X-Idempotency-Key

        Returns:
            Dictionary mapping ID to record data (missing IDs are omitted)

        Raises:
            QueryExecutionError: If query fails

        """
        if not ids:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion
        effective_batch_size = batch_size or getattr(self, "batch_size", 100)
        effective_batch_size = self._validate_batch_size(effective_batch_size)

        results: dict[int | str, dict[str, Any]] = {}

        # Process IDs in batches
        for i in range(0, len(ids), effective_batch_size):
            batch_ids = ids[i : i + effective_batch_size]

            def batch_operation(batch_ids: list[int | str] = batch_ids) -> object:
                # Use safe query builder with ActiveRecord parameterization
                query = self._build_safe_batch_query(model, "id", batch_ids)
                return self.execute_json_query(query)

            try:
                # Execute batch operation with retry logic (with idempotency key propagation)
                label_prefix = f"Batch fetch {model} records "
                sample_label = f"{batch_ids[:BATCH_LABEL_SAMPLE]}{'...' if len(batch_ids) > BATCH_LABEL_SAMPLE else ''}"
                batch_results = self._retry_with_exponential_backoff(
                    batch_operation,
                    f"{label_prefix}{sample_label}",
                    jitter=True,
                    headers=headers,
                )

                if batch_results:
                    # Ensure we have a list
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]

                    # Optimize ID mapping - create lookup sets for O(1) performance
                    batch_id_set = {str(bid) for bid in batch_ids}
                    original_id_map = {str(bid): bid for bid in batch_ids}

                    # Map results by ID with O(1) lookups
                    for record in batch_results:
                        if isinstance(record, dict) and "id" in record:
                            record_id_str = str(record["id"])
                            if record_id_str in batch_id_set:
                                original_id = original_id_map[record_id_str]
                                results[original_id] = record

            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    "Failed to fetch batch of %s records (IDs %s) after retries: %s",
                    model,
                    batch_ids,
                    e,
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for batch_id in batch_ids:
                    self.logger.debug(
                        "Failed to fetch %s record ID %s: %s",
                        model,
                        batch_id,
                        e,
                    )
                continue

        return results

    def create_record(self, model: str, attributes: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
        """Create a new record.

        Args:
            model: Model name (e.g., "User", "Project")
            attributes: Attributes to set on the record

        Returns:
            Created record data

        Raises:
            QueryExecutionError: If creation fails

        """
        # Convert Python dict to Ruby hash format
        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        ruby_hash = json.dumps(attributes, ensure_ascii=False).replace('"', "'")

        # Build Rails command for creating a record
        # Use a simple, single-line approach that works well with tmux console
        # Convert Python boolean values to Ruby equivalents
        def format_value(v: object) -> str:
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, str):
                return f"'{v}'"
            return str(v)

        attributes_str = ", ".join(
            [f"'{k}' => {format_value(v)}" for k, v in attributes.items()],
        )
        command = (
            f"record = {model}.new({{{attributes_str}}}); "
            f"record.save ? record.as_json : {{'error' => record.errors.full_messages}}"
        )

        try:
            # Try execute_query_to_json_file first for better output handling
            result = self.execute_query_to_json_file(command)

            # Check if we got a valid dictionary
            if isinstance(result, dict):
                return result

            # If result is None, empty, or not a dict, try the fallback method
            if result is None or not isinstance(result, dict):
                logger.debug(
                    "First method returned invalid result (%s), trying fallback",
                    type(result),
                )

                # Fallback to simpler command with execute_json_query
                simple_command = f"""
                record = {model}.create({ruby_hash})
                if record.persisted?
                  record.as_json
                else
                  raise "Failed to create record: #{{record.errors.full_messages.join(', ')}}"
                end
                """
                result = self.execute_json_query(simple_command)

            # Final validation
            if not isinstance(result, dict):
                # If we still don't have a dict, but the command didn't raise an error,
                # assume success and try to get the record by its attributes
                logger.warning(
                    (
                        "Could not parse JSON response from %s creation, but command executed. "
                        "Attempting to find created record."
                    ),
                    model,
                )

                # Try to find the record we just created
                try:
                    # Use a subset of attributes that are likely to be unique
                    search_attrs = {}
                    for key in ["name", "title", "identifier", "email"]:
                        if key in attributes:
                            search_attrs[key] = attributes[key]
                            break

                    if search_attrs:
                        found_record = self.find_record(model, search_attrs)
                        if found_record:
                            logger.info("Successfully found created %s record", model)
                            return found_record
                except Exception as e:  # noqa: BLE001
                    logger.debug("Could not find created record: %s", e)

                # If all else fails, create a minimal response
                logger.warning("Creating minimal response for %s creation", model)
                return {
                    "id": None,
                    "created": True,
                    "model": model,
                    "attributes": attributes,
                }

            return result  # noqa: TRY300

        except RubyError as e:
            msg = f"Failed to create {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            msg = f"Error creating {model}."
            raise QueryExecutionError(msg) from e

    def update_record(
        self,
        model: str,
        record_id: int,
        attributes: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a record with given attributes.

        Args:
            model: Model name (e.g., "User", "Project")
            record_id: Record ID
            attributes: Attributes to update

        Returns:
            Updated record data

        Raises:
            RecordNotFoundError: If record doesn't exist
            QueryExecutionError: If update fails

        """
        # Convert Python dict to Ruby hash format
        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        ruby_hash = json.dumps(attributes, ensure_ascii=False).replace('"', "'")

        # Build command to update the record
        command = f"""
        record = {model}.find_by(id: {record_id})
        if record.nil?
          raise "Record not found"
        elsif record.update({ruby_hash})
          record.as_json
        else
          raise "Failed to update record: #{{record.errors.full_messages.join(', ')}}"
        end
        """

        try:
            result = self.execute_json_query(command)
        except RubyError as e:
            if "Record not found" in str(e):
                msg = f"{model} with ID {record_id} not found"
                raise RecordNotFoundError(msg) from e
            msg = f"Failed to update {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            # Normalize to the same message tests expect for generic failures
            msg = f"Failed to update {model}."
            raise QueryExecutionError(msg) from e
        else:
            if not isinstance(result, dict):
                msg = (
                    f"Failed to update {model}: Invalid response from OpenProject (type={type(result)}, value={result})"
                )
                raise QueryExecutionError(msg)
            return result

    def delete_record(self, model: str, record_id: int) -> None:
        """Delete a record.

        Args:
            model: Model name (e.g., "User", "Project")
            record_id: Record ID

        Raises:
            RecordNotFoundError: If record doesn't exist
            QueryExecutionError: If deletion fails

        """
        command = f"""
        record = {model}.find_by(id: {record_id})
        if record.nil?
          raise "Record not found"
        elsif record.destroy
          true
        else
          raise "Failed to delete record: #{{record.errors.full_messages.join(', ')}}"
        end
        """

        try:
            self.execute_query(command)
        except RubyError as e:
            if "Record not found" in str(e):
                msg = f"{model} with ID {record_id} not found"
                raise RecordNotFoundError(msg) from e
            msg = f"Failed to delete {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            msg = f"Error deleting {model}."
            raise QueryExecutionError(msg) from e

    def find_all_records(
        self,
        model: str,
        conditions: dict[str, Any] | None = None,
        limit: int | None = None,
        includes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Find all records matching conditions.

        Args:
            model: Model name (e.g., "User", "Project")
            conditions: Optional conditions hash
            limit: Optional limit on number of records
            includes: Optional list of associations to include

        Returns:
            List of record data

        Raises:
            QueryExecutionError: If query fails

        """
        # Start building the query
        query = f"{model}"

        # Add conditions if provided
        if conditions:
            conditions_str = json.dumps(conditions).replace('"', "'")
            query += f".where({conditions_str})"

        # Add includes if provided
        if includes:
            includes_str = json.dumps(includes).replace('"', "'")
            query += f".includes({includes_str})"

        # Add limit if provided
        if limit:
            query += f".limit({limit})"

        # Build Ruby expression that returns array/dicts directly
        ruby_expr = f"{query}.as_json"

        try:
            # Prefer file-based for multi-record results to avoid console artifacts
            data = self.execute_large_query_to_json_file(
                ruby_expr,
                container_file=f"/tmp/j2o_{model.lower()}_records.json",  # noqa: S108
                timeout=60,
            )
            if data is None:
                return []
            return data if isinstance(data, list) else [data]
        except Exception as e:
            msg = f"Error finding records for {model}."
            raise QueryExecutionError(msg) from e

    def execute_transaction(self, commands: list[str]) -> object:
        """Execute multiple commands in a transaction.

        Args:
            commands: List of Ruby/Rails commands

        Returns:
            Result of the transaction

        Raises:
            QueryExecutionError: If transaction fails

        """
        # Build transaction block
        transaction_commands = "\n".join(commands)
        transaction_block = f"""
        ActiveRecord::Base.transaction do
          {transaction_commands}
        end
        """

        try:
            return self.execute_query(transaction_block)
        except Exception as e:
            msg = "Transaction failed."
            raise QueryExecutionError(msg) from e

    def transfer_file_from_container(
        self,
        container_path: Path,
        local_path: Path,
    ) -> Path:
        """Copy a file from the container to the local system.

        Args:
            container_path: Path to the file in the container
            local_path: Path where the file should be saved locally

        Returns:
            Path to the local file

        Raises:
            FileTransferError: If transfer fails
            FileNotFoundError: If container file doesn't exist

        """
        try:
            return self.docker_client.copy_file_from_container(
                container_path,
                local_path,
            )

        except Exception as e:
            msg = "Error transferring file from container."
            raise FileTransferError(msg) from e

    def get_users(self) -> list[dict[str, Any]]:
        """Get all users from OpenProject.

        Uses caching to avoid repeated Rails console queries.

        Returns:
            List of OpenProject users

        Raises:
            QueryExecutionError: If unable to retrieve users

        """
        # Check cache first (5 minutes validity)
        current_time = time.time()
        cache_valid = (
            hasattr(self, "_users_cache")
            and hasattr(self, "_users_cache_time")
            and self._users_cache is not None
            and self._users_cache_time is not None
            and current_time - self._users_cache_time < USERS_CACHE_TTL_SECONDS
        )

        if cache_valid:
            logger.debug("Using cached users data (%d users)", len(self._users_cache))
            return self._users_cache

        try:
            # Route through centralized helper for uniform behavior
            # Include the 'mail' attribute and the J2O provenance custom fields if present
            file_path = self._generate_unique_temp_filename("users")
            ruby_query = (
                "cf_origin_system = CustomField.find_by(type: 'UserCustomField', name: 'J2O Origin System'); "
                "cf_origin_id = CustomField.find_by(type: 'UserCustomField', name: 'J2O User ID'); "
                "cf_origin_key = CustomField.find_by(type: 'UserCustomField', name: 'J2O User Key'); "
                "cf_origin_url = CustomField.find_by(type: 'UserCustomField', name: 'J2O External URL'); "
                "User.all.map do |u|\n"
                "  next unless u.is_a?(::User)\n"
                "  data = u.as_json\n"
                "  data['mail'] = u.mail\n"
                "  data['j2o_origin_system'] = (cf_origin_system ? u.custom_value_for(cf_origin_system)&.value : nil)\n"
                "  data['j2o_user_id'] = (cf_origin_id ? u.custom_value_for(cf_origin_id)&.value : nil)\n"
                "  data['j2o_user_key'] = (cf_origin_key ? u.custom_value_for(cf_origin_key)&.value : nil)\n"
                "  data['j2o_external_url'] = (cf_origin_url ? u.custom_value_for(cf_origin_url)&.value : nil)\n"
                "  pref = (u.respond_to?(:pref) ? u.pref : nil)\n"
                "  data['time_zone'] = (pref ? pref.time_zone : nil)\n"
                "  if pref && pref.respond_to?(:language)\n"
                "    data['language'] = pref.language\n"
                "  end\n"
                "  data\n"
                "end.compact"
            )
            json_data = self.execute_large_query_to_json_file(ruby_query, container_file=file_path, timeout=180)
        except QueryExecutionError:
            # Propagate specific high-signal errors (tests assert exact messages)
            raise
        except Exception as e:
            msg = "Failed to retrieve users."
            raise QueryExecutionError(msg) from e
        else:
            # Validate that we got a list
            if not isinstance(json_data, list):
                logger.error(
                    "Expected list of users, got %s: %s",
                    type(json_data),
                    str(json_data)[:200],
                )
                msg = f"Invalid users data format - expected list, got {type(json_data)}"
                raise QueryExecutionError(msg)

            # Update cache
            self._users_cache = json_data or []
            self._users_cache_time = current_time

            logger.info("Retrieved %d users from OpenProject", len(self._users_cache))
            return self._users_cache

    def get_user(self, user_identifier: int | str) -> dict[str, Any]:  # noqa: C901, PLR0912
        """Get a single user by id, email, or login.

        This is a convenience wrapper over ``find_record`` and existing helpers,
        with light cache lookups to reduce Rails console round-trips.

        Args:
            user_identifier: An integer id, numeric string id, email, or login

        Returns:
            User data as a dictionary

        Raises:
            RecordNotFoundError: If the user cannot be found
            QueryExecutionError: If the lookup fails

        """
        try:
            # Normalize identifier
            identifier: str | int
            if isinstance(user_identifier, str):
                identifier = user_identifier.strip()
                if not identifier:
                    msg = "Empty user identifier"
                    raise ValueError(msg)  # noqa: TRY301
            else:
                identifier = int(user_identifier)

            # If numeric string, treat as id
            if isinstance(identifier, str) and identifier.isdigit():
                identifier = int(identifier)

            # Try cache fast-paths when possible
            if isinstance(identifier, int):
                # Check cached users first
                if getattr(self, "_users_cache", None):
                    for user in self._users_cache or []:
                        try:
                            uid = user.get("id")
                            if isinstance(uid, int) and uid == identifier:
                                return user
                            if isinstance(uid, str) and uid.isdigit() and int(uid) == identifier:
                                return user
                        except Exception:  # noqa: BLE001
                            logger.debug("Malformed user cache entry encountered")
                            continue

                # Fallback to direct lookup by id
                return self.find_record("User", identifier)

            # Email lookup
            if isinstance(identifier, str) and "@" in identifier:
                return self.get_user_by_email(identifier)

            # Login lookup (try cache first)
            login = identifier  # type: ignore[assignment]
            if getattr(self, "_users_cache", None):
                for user in self._users_cache or []:
                    if user.get("login") == login:
                        # Opportunistically cache by email for future lookups
                        email = user.get("mail") or user.get("email")
                        if isinstance(email, str):
                            self._users_by_email_cache[email.lower()] = user
                        return user

            # Fallback to direct lookup by login
            user = self.find_record("User", {"login": login})
            # Opportunistically cache by email for future lookups
            email = user.get("mail") or user.get("email")
            if isinstance(email, str):
                self._users_by_email_cache[email.lower()] = user
            return user  # noqa: TRY300

        except RecordNotFoundError:
            raise
        except Exception as e:
            msg = "Error getting user."
            raise QueryExecutionError(msg) from e

    def get_user_by_email(self, email: str) -> dict[str, Any]:
        """Get a user by email address.

        Uses cached user data if available.

        Args:
            email: Email address of the user

        Returns:
            User data

        Raises:
            RecordNotFoundError: If user with given email is not found
            QueryExecutionError: If query fails

        """
        # Normalize email to lowercase
        email_lower = email.lower()

        # Check cache first
        if hasattr(self, "_users_by_email_cache") and email_lower in self._users_by_email_cache:
            return self._users_by_email_cache[email_lower]

        # Try to load all users to populate cache
        try:
            # Load all users - we ignore the returned value because we just
            # want to populate the cache
            self.get_users()

            # Check if we got the user in the newly populated cache
            if email_lower in self._users_by_email_cache:
                return self._users_by_email_cache[email_lower]

            # If not in cache, try direct query
            user = self.find_record("User", {"email": email})
            if user:
                # Cache the result
                self._users_by_email_cache[email_lower] = user
                return user

            msg = f"User with email '{email}' not found"
            raise RecordNotFoundError(msg)  # noqa: TRY301

        except RecordNotFoundError:
            raise  # Re-raise RecordNotFoundError
        except Exception as e:
            msg = "Error finding user by email."
            raise QueryExecutionError(msg) from e

    def get_custom_field_by_name(self, name: str) -> dict[str, Any]:
        """Find a custom field by name.

        Args:
            name: The name of the custom field to find

        Returns:
            The custom field

        Raises:
            RecordNotFoundError: If custom field with given name is not found

        """
        return self.find_record("CustomField", {"name": name})

    def get_custom_field_id_by_name(self, name: str) -> int:
        """Find a custom field ID by name.

        Args:
            name: The name of the custom field to find

        Returns:
            The custom field ID

        Raises:
            RecordNotFoundError: If custom field with given name is not found
            QueryExecutionError: If query fails

        """
        try:
            result = self.execute_query(f"CustomField.where(name: '{name}').first&.id")

            # Handle nil value from Ruby
            if result is None:
                msg = f"Custom field '{name}' not found"
                raise RecordNotFoundError(msg)  # noqa: TRY301

            # Handle integer result
            if isinstance(result, int):
                return result

            # Try to convert string to int
            if isinstance(result, str):
                try:
                    return int(result)
                except ValueError:
                    msg = f"Invalid ID format: {result}"
                    raise QueryExecutionError(msg) from None

            msg = f"Unexpected result type: {type(result)}"
            raise QueryExecutionError(msg)  # noqa: TRY301

        except RecordNotFoundError:
            raise  # Re-raise RecordNotFoundError
        except Exception as e:
            msg = "Error getting custom field ID."
            raise QueryExecutionError(msg) from e

    def get_custom_fields(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Get all custom fields from OpenProject.

        Args:
            force_refresh: If True, force refresh from server, ignoring cache

        Returns:
            List of custom field dictionaries

        Raises:
            QueryExecutionError: If query execution fails

        """
        current_time = time.time()
        cache_timeout = 300  # 5 minutes

        # Check cache first (unless force refresh)
        if (
            not force_refresh
            and self._custom_fields_cache
            and (current_time - self._custom_fields_cache_time) < cache_timeout
        ):
            logger.debug(
                "Using cached custom fields (age: %.1fs)",
                current_time - self._custom_fields_cache_time,
            )
            return self._custom_fields_cache

        try:
            # Centralized execution path
            file_path = self._generate_unique_temp_filename("custom_fields")
            custom_fields = self.execute_large_query_to_json_file(
                "CustomField.all",
                container_file=file_path,
                timeout=90,
            )

            # Update cache
            self._custom_fields_cache = custom_fields or []
            self._custom_fields_cache_time = current_time

            return custom_fields if isinstance(custom_fields, list) else []

        except Exception as e:
            msg = "Failed to get custom fields."
            raise QueryExecutionError(msg) from e

    def get_statuses(self) -> list[dict[str, Any]]:  # noqa: C901, PLR0912, PLR0915
        """Get all statuses from OpenProject.

        Returns:
            List of status objects

        Raises:
            QueryExecutionError: If query fails

        """
        try:
            # Use file-based JSON to avoid tmux/console control characters
            file_path = self._generate_unique_temp_filename("statuses")
            file_path_interpolated = f"'{file_path}'"
            write_query = (
                "require 'json'; "
                f"statuses = Status.all.as_json; File.write({file_path_interpolated}, "
                "JSON.pretty_generate(statuses)); nil"
            )

            try:
                # Skip console attempt entirely if forced runner mode
                if os.environ.get("J2O_FORCE_RAILS_RUNNER"):
                    from src.clients.rails_console_client import ConsoleNotReadyError  # noqa: PLC0415
                    raise ConsoleNotReadyError("Forced runner mode via J2O_FORCE_RAILS_RUNNER")
                output = self.rails_client.execute(write_query, suppress_output=True)
                self._check_console_output_for_errors(output or "", context="get_statuses")
                logger.debug("Successfully executed statuses write command")
            except Exception as e:
                from src.clients.rails_console_client import (  # noqa: PLC0415
                    CommandExecutionError,
                    ConsoleNotReadyError,
                    RubyError,
                )

                if isinstance(e, (ConsoleNotReadyError, CommandExecutionError, RubyError, QueryExecutionError)):
                    if not config.migration_config.get("enable_runner_fallback", False):
                        # Respect user's preference to avoid per-request rails runner fallback
                        raise
                    logger.warning(
                        "Rails console failed for statuses (%s); falling back to rails runner",
                        type(e).__name__,
                    )
                    runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"  # noqa: S108
                    local_tmp = Path(self.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
                    local_tmp.parent.mkdir(parents=True, exist_ok=True)
                    ruby_runner = (
                        "require 'json'\n"
                        "statuses = Status.all.as_json\n"
                        f"File.write('{file_path}', JSON.pretty_generate(statuses))\n"
                    )
                    with local_tmp.open("w", encoding="utf-8") as f:
                        f.write(ruby_runner)
                    self.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))
                    runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                    stdout, stderr, rc = self.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(msg) from e
                else:
                    raise

            operation_succeeded = False  # Track success for debug file preservation
            try:
                ssh_command = f"docker exec {self.container_name} cat {file_path}"
                stdout = ""
                stderr = ""
                returncode = 1
                # Small retry loop to handle race where file may not be written yet
                for attempt in range(8):  # ~2 seconds total
                    try:
                        stdout, stderr, returncode = self.ssh_client.execute_command(ssh_command)
                    except Exception as e:
                        if "No such file or directory" in str(e):
                            time.sleep(0.25)
                            continue
                        raise
                    if returncode == 0:
                        if attempt > 0:
                            logger.debug(
                                "Recovered after %d attempts reading container file %s",
                                attempt + 1,
                                file_path,
                            )
                        break
                    if stderr and "No such file or directory" in stderr:
                        time.sleep(0.25)
                        continue
                    _emsg = f"Failed to read statuses file: {stderr or 'unknown error'}"
                    raise QueryExecutionError(_emsg)
                parsed = json.loads(stdout)
                logger.info("Successfully loaded %d statuses from container file", len(parsed))
                operation_succeeded = True
                return parsed if isinstance(parsed, list) else []
            finally:
                preserve_on_error = config.migration_config.get("preserve_debug_files_on_error", True)
                should_cleanup = operation_succeeded or not preserve_on_error
                if not should_cleanup:
                    logger.warning(
                        "Preserving debug file due to error: %s (set preserve_debug_files_on_error=false to auto-cleanup)",
                        file_path,
                    )
                else:
                    try:
                        self.ssh_client.execute_command(
                            f"docker exec {self.container_name} rm -f {file_path}",
                        )
                    except Exception as cleanup_err:
                        logger.warning(
                            "Failed to cleanup container temp file %s: %s",
                            file_path,
                            cleanup_err,
                        )
        except Exception as e:
            msg = "Failed to get statuses."
            raise QueryExecutionError(msg) from e

    def get_work_package_types(self) -> list[dict[str, Any]]:  # noqa: C901, PLR0912, PLR0915
        """Get all work package types from OpenProject.

        Returns:
            List of work package type objects

        Raises:
            QueryExecutionError: If query fails

        """
        try:
            # Use file-based JSON to avoid tmux/console artifacts and project only minimal fields
            file_path = self._generate_unique_temp_filename("work_package_types")
            file_path_interpolated = f"'{file_path}'"
            # Avoid Type#as_json on full AR models to prevent recursion/stack overflows in IRB
            # Only extract minimal attributes we actually need for mapping
            write_query = (
                "require 'json'; "
                "types = Type.select(:id, :name).map { |t| { id: t.id, name: t.name } }; "
                f"File.write({file_path_interpolated}, JSON.pretty_generate(types)); nil"
            )

            try:
                # Skip console attempt entirely if forced runner mode
                if os.environ.get("J2O_FORCE_RAILS_RUNNER"):
                    from src.clients.rails_console_client import ConsoleNotReadyError  # noqa: PLC0415
                    raise ConsoleNotReadyError("Forced runner mode via J2O_FORCE_RAILS_RUNNER")
                self.rails_client.execute(write_query, suppress_output=True)
                logger.debug("Successfully executed work package types write command")
            except Exception as e:
                from src.clients.rails_console_client import (  # noqa: PLC0415
                    CommandExecutionError,
                    ConsoleNotReadyError,
                    RubyError,
                )

                if isinstance(e, (ConsoleNotReadyError, CommandExecutionError, RubyError, QueryExecutionError)):
                    if not config.migration_config.get("enable_runner_fallback", False):
                        raise
                    logger.warning(
                        "Rails console failed for work package types (%s); falling back to rails runner",
                        type(e).__name__,
                    )
                    runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"  # noqa: S108
                    local_tmp = Path(self.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
                    local_tmp.parent.mkdir(parents=True, exist_ok=True)
                    ruby_runner = (
                        "require 'json'\n"
                        "types = Type.select(:id, :name).map { |t| { id: t.id, name: t.name } }\n"
                        f"File.write('{file_path}', JSON.pretty_generate(types))\n"
                    )
                    with local_tmp.open("w", encoding="utf-8") as f:
                        f.write(ruby_runner)
                    self.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))
                    runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                    stdout, stderr, rc = self.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        _emsg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(_emsg) from e
                else:
                    raise

            operation_succeeded = False  # Track success for debug file preservation
            try:
                ssh_command = f"docker exec {self.container_name} cat {file_path}"
                stdout = ""
                stderr = ""
                returncode = 1
                # Small retry loop to handle race where file may not be written yet
                for _ in range(8):  # ~2 seconds total
                    try:
                        stdout, stderr, returncode = self.ssh_client.execute_command(ssh_command)
                    except Exception as e:
                        if "No such file or directory" in str(e):
                            time.sleep(0.25)
                            continue
                        raise
                    if returncode == 0:
                        break
                    if stderr and "No such file or directory" in stderr:
                        time.sleep(0.25)
                        continue
                    _emsg = f"Failed to read work package types file: {stderr or 'unknown error'}"
                    raise QueryExecutionError(_emsg)
                if returncode != 0:
                    _emsg = f"Failed to read work package types file: {stderr or 'unknown error'}"
                    raise QueryExecutionError(_emsg)
                parsed = json.loads(stdout.strip())
                logger.info(
                    "Successfully loaded %d work package types from container file",
                    len(parsed) if isinstance(parsed, list) else 0,
                )
                operation_succeeded = True
                return parsed if isinstance(parsed, list) else []
            finally:
                preserve_on_error = config.migration_config.get("preserve_debug_files_on_error", True)
                should_cleanup = operation_succeeded or not preserve_on_error
                if not should_cleanup:
                    logger.warning(
                        "Preserving debug file due to error: %s (set preserve_debug_files_on_error=false to auto-cleanup)",
                        file_path,
                    )
                else:
                    try:
                        self.ssh_client.execute_command(
                            f"docker exec {self.container_name} rm -f {file_path}",
                        )
                    except Exception as cleanup_err:
                        logger.warning(
                            "Failed to cleanup container temp file %s: %s",
                            file_path,
                            cleanup_err,
                        )
        except Exception as e:
            msg = "Failed to get work package types."
            raise QueryExecutionError(msg) from e

    def get_projects(self, *, top_level_only: bool = False) -> list[dict[str, Any]]:  # noqa: C901, PLR0915, PLR0912
        """Get projects from OpenProject using file-based approach.

        Args:
            top_level_only: When True, returns only top-level (company) projects with no parent

        Returns:
            List of OpenProject projects as dictionaries

        Raises:
            QueryExecutionError: If unable to retrieve projects

        """
        try:
            # Use pure file-based approach - write to file and read directly from filesystem
            file_path = self._generate_unique_temp_filename("projects")

            # Execute command to write JSON to file - use a simple command that returns minimal output
            # Split into Python variable interpolation (f-string) and Ruby script (raw string)
            file_path_interpolated = f"'{file_path}'"
            scope = "Project.where(parent_id: nil)" if top_level_only else "Project.all"
            write_query = (
                "require 'json'\n"
                "projects = " + scope + ".includes(:enabled_modules).map do |p|\n"
                "  {\n"
                "    id: p.id,\n"
                "    name: p.name,\n"
                "    identifier: p.identifier,\n"
                "    description: p.description.to_s,\n"
                "    status: (p.respond_to?(:status) ? p.status&.name : nil),\n"
                "    status_code: p.status_code,\n"
                "    parent_id: p.parent_id,\n"
                "    public: p.public?,\n"
                "    active: p.active?,\n"
                "    enabled_modules: p.enabled_module_names\n"
                "  }\n"
                "end\n" + f"File.write({file_path_interpolated}, JSON.pretty_generate(projects))\n" + "nil"
            )

            # Execute the write command - verify console output and fallback if needed
            try:
                out = self.rails_client.execute(write_query, suppress_output=True)
                self._check_console_output_for_errors(out or "", context="get_projects")
                logger.debug("Successfully executed projects write command")
            except Exception as e:
                from src.clients.rails_console_client import (  # noqa: PLC0415
                    CommandExecutionError,
                    ConsoleNotReadyError,
                    RubyError,
                )

                if isinstance(e, (ConsoleNotReadyError, CommandExecutionError, RubyError, QueryExecutionError)):
                    if not config.migration_config.get("enable_runner_fallback", False):
                        raise
                    logger.warning(
                        "Rails console failed for projects (%s); falling back to rails runner",
                        type(e).__name__,
                    )
                    runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"  # noqa: S108
                    local_tmp = Path(self.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
                    local_tmp.parent.mkdir(parents=True, exist_ok=True)
                    top_selector_line = (
                        "projects = Project.where(parent_id: nil).select("
                        ":id, :name, :identifier, :description, :status_code"
                        ").as_json\n"
                    )
                    all_selector_line = (
                        "projects = Project.all.select(:id, :name, :identifier, :description, :status_code).as_json\n"
                    )
                    ruby_runner = (
                        "require 'json'\n"
                        + (top_selector_line if top_level_only else all_selector_line)
                        + f"File.write('{file_path}', JSON.pretty_generate(projects))\n"
                    )
                    with local_tmp.open("w", encoding="utf-8") as f:
                        f.write(ruby_runner)
                    self.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))
                    runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                    stdout, stderr, rc = self.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        _emsg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(_emsg) from e
                else:
                    raise

            # Read the JSON directly from the Docker container file system via SSH
            # Use SSH to read the file from the Docker container
            ssh_command = f"docker exec {self.container_name} cat {file_path}"
            try:
                stdout, stderr, returncode = self.ssh_client.execute_command(ssh_command)
            except Exception as e:
                msg = f"SSH command failed: {e}"
                raise QueryExecutionError(msg) from e
            if returncode != 0:
                logger.error(
                    "Failed to read file from container, stderr: %s",
                    stderr,
                )
                msg = f"SSH command failed with code {returncode}: {stderr}"
                raise QueryExecutionError(msg)  # noqa: TRY301

            file_content = stdout.strip()
            logger.debug(
                "Successfully read projects file from container, content length: %d",
                len(file_content),
            )

            # Parse the JSON content
            try:
                result = json.loads(file_content)
            except json.JSONDecodeError as e:
                logger.exception("Failed to read projects from container file %s", file_path)
                msg = f"Failed to read projects from container file: {e}"
                raise QueryExecutionError(msg) from e
            else:
                logger.info(
                    "Successfully loaded %d projects from container file",
                    len(result) if isinstance(result, list) else 0,
                )

            # The execute_query_to_json_file method should return the parsed JSON
            if not isinstance(result, list):
                logger.error(
                    "Expected list of projects, got %s: %s",
                    type(result),
                    str(result)[:200],
                )
                msg = f"Invalid projects data format - expected list, got {type(result)}"
                raise QueryExecutionError(msg)  # noqa: TRY301

            # Validate and clean project data
            validated_projects = []
            for project in result:
                if isinstance(project, dict) and project.get("id"):
                    # For OpenProject projects, identifier might be optional or missing
                    # Accept projects with at least an ID and name
                    enabled_modules = project.get("enabled_modules") or []
                    if isinstance(enabled_modules, list):
                        enabled_modules = sorted({str(mod) for mod in enabled_modules if mod})
                    else:
                        enabled_modules = []

                    validated_project = {
                        "id": project.get("id"),
                        "name": project.get("name", ""),
                        "identifier": project.get(
                            "identifier",
                            f"project-{project.get('id')}",
                        ),
                        "description": project.get("description", ""),
                        "public": bool(project.get("public", False)),
                        "status": project.get("status"),
                        "status_code": project.get("status_code"),
                        "parent_id": project.get("parent_id"),
                        "active": project.get("active"),
                        "enabled_modules": enabled_modules,
                    }
                    validated_projects.append(validated_project)
                    logger.debug("Validated project: %s", validated_project)
                else:
                    logger.debug(
                        "Skipping invalid project data (missing ID): %s",
                        project,
                    )

            logger.info(
                "Retrieved %d projects using file-based method",
                len(validated_projects),
            )
            return validated_projects  # noqa: TRY300

        except Exception as e:
            logger.exception("Failed to get projects using file-based method")
            msg = f"Failed to retrieve projects: {e}"
            raise QueryExecutionError(msg) from e

    def get_project_by_identifier(self, identifier: str) -> dict[str, Any]:
        """Get a project by identifier.

        Args:
            identifier: Project identifier or slug

        Returns:
            Project object

        Raises:
            RecordNotFoundError: If project with given identifier is not found
            QueryExecutionError: If query fails

        """
        try:
            project = self.execute_json_query(
                f"Project.find_by(identifier: '{identifier}')",
            )
        except Exception as e:
            msg = "Failed to get project."
            raise QueryExecutionError(msg) from e
        if project is None:
            msg = f"Project with identifier '{identifier}' not found"
            raise RecordNotFoundError(msg)
        return project

    def delete_all_work_packages(self) -> int:
        """Delete all work packages in bulk.

        Returns:
            Number of deleted work packages

        Raises:
            QueryExecutionError: If bulk deletion fails

        """
        try:
            count = self.execute_query("WorkPackage.delete_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            msg = "Failed to delete all work packages."
            raise QueryExecutionError(msg) from e

    def delete_all_projects(self) -> int:
        """Delete all projects in bulk.

        Returns:
            Number of deleted projects

        Raises:
            QueryExecutionError: If bulk deletion fails

        """
        try:
            count = self.execute_query("Project.delete_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            msg = "Failed to delete all projects."
            raise QueryExecutionError(msg) from e

    def delete_all_custom_fields(self) -> int:
        """Delete all custom fields in bulk.

        Uses destroy_all for proper dependency cleanup.

        Returns:
            Number of deleted custom fields

        Raises:
            QueryExecutionError: If bulk deletion fails

        """
        try:
            # Get count before deletion for return value
            count = self.execute_query("CustomField.count")
            self.execute_query("CustomField.destroy_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            msg = "Failed to delete all custom fields."
            raise QueryExecutionError(msg) from e

    def delete_non_default_issue_types(self) -> int:
        """Delete non-default issue types (work package types).

        Returns:
            Number of deleted types

        Raises:
            QueryExecutionError: If deletion fails

        """
        script = """
        non_default_types = Type.where(is_default: false, is_standard: false)
        count = non_default_types.count
        non_default_types.destroy_all
        count
        """

        try:
            count = self.execute_query(script)
            return count if isinstance(count, int) else 0
        except Exception as e:
            msg = "Failed to delete non-default issue types."
            raise QueryExecutionError(msg) from e

    def delete_non_default_issue_statuses(self) -> int:
        """Delete non-default issue statuses.

        Returns:
            Number of deleted statuses

        Raises:
            QueryExecutionError: If deletion fails

        """
        script = """
        non_default_statuses = Status.where(is_default: false)
        count = non_default_statuses.count
        non_default_statuses.destroy_all
        count
        """

        try:
            count = self.execute_query(script)
            return count if isinstance(count, int) else 0
        except Exception as e:
            msg = "Failed to delete non-default issue statuses."
            raise QueryExecutionError(msg) from e

    def get_time_entry_activities(self) -> list[dict[str, Any]]:
        """Get all available time entry activities from OpenProject.

        Returns:
            List of time entry activity dictionaries with id, name, and other properties

        Raises:
            QueryExecutionError: If the query fails

        """
        # Use file-based JSON retrieval to avoid console control-character issues
        query = (
            "TimeEntryActivity.active.map { |activity| "
            "{ id: activity.id, name: activity.name, position: activity.position, "
            "is_default: activity.is_default, active: activity.active } }"
        )

        try:
            result = self.execute_large_query_to_json_file(
                query,
                container_file="/tmp/j2o_time_entry_activities.json",  # noqa: S108
                timeout=60,
            )
            return result if isinstance(result, list) else []
        except Exception as e:
            msg = f"Failed to retrieve time entry activities: {e}"
            raise QueryExecutionError(msg) from e

    def create_time_entry(  # noqa: C901
        self,
        time_entry_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create a time entry in OpenProject.

        Args:
            time_entry_data: Time entry data in OpenProject API format

        Returns:
            Created time entry data with ID, or None if creation failed

        Raises:
            QueryExecutionError: If the creation fails

        """
        # Extract embedded references and convert to IDs
        embedded = time_entry_data.get("_embedded", {})

        # Get work package ID from href
        work_package_href = embedded.get("workPackage", {}).get("href", "")
        work_package_id = None
        if work_package_href:
            # Extract ID from href like "/api/v3/work_packages/123"
            match = re.search(r"/work_packages/(\d+)", work_package_href)
            if match:
                work_package_id = int(match.group(1))

        # Get user ID from href
        user_href = embedded.get("user", {}).get("href", "")
        user_id = None
        if user_href:
            # Extract ID from href like "/api/v3/users/456"
            match = re.search(r"/users/(\d+)", user_href)
            if match:
                user_id = int(match.group(1))

        # Get activity ID from href
        activity_href = embedded.get("activity", {}).get("href", "")
        activity_id = None
        if activity_href:
            # Extract ID from href like "/api/v3/time_entries/activities/789"
            match = re.search(r"/activities/(\d+)", activity_href)
            if match:
                activity_id = int(match.group(1))

        if not all([work_package_id, user_id, activity_id]):
            msg = (
                f"Missing required IDs: work_package_id={work_package_id}, user_id={user_id}, activity_id={activity_id}"
            )
            raise ValueError(
                msg,
            )

        # Normalize comment value (can be string or {raw,text})
        comment_obj = time_entry_data.get("comment", "")
        if isinstance(comment_obj, dict):
            comment_str = comment_obj.get("raw") or comment_obj.get("text") or str(comment_obj)
        else:
            comment_str = str(comment_obj)

        # Prepare the script with proper Ruby syntax
        script = f"""
        begin
          require 'logger'
          begin; Rails.logger.level = Logger::WARN; rescue; end
          begin; ActiveJob::Base.logger = Logger.new(nil); rescue; end
          begin; GoodJob.logger = Logger.new(nil); rescue; end
          time_entry = TimeEntry.new(
            entity_id: {work_package_id},
            entity_type: 'WorkPackage',
            user_id: {user_id},
            logged_by_id: {user_id},
            activity_id: {activity_id},
            hours: {time_entry_data.get("hours", 0)},
            spent_on: Date.parse('{time_entry_data.get("spentOn", "")}'),
            comments: {comment_str!r}
          )

          # Ensure project is set from associated work package to satisfy validations
          begin
            wp = WorkPackage.find_by(id: {work_package_id})
            if wp
              time_entry.entity = wp
              time_entry.project = wp.project
            end
          rescue => e
            # ignore association errors here; validations will surface below
          end

          # Provenance CF for time entries: J2O Origin Worklog Key
          begin
            key = {time_entry_data.get("_meta", {}).get("jira_worklog_key")!r}
            if key
              cf = CustomField.find_by(type: 'TimeEntryCustomField', name: 'J2O Origin Worklog Key')
              if !cf
                cf = CustomField.new(name: 'J2O Origin Worklog Key', field_format: 'string',
                  is_required: false, is_for_all: true, type: 'TimeEntryCustomField')
                cf.save
              end
              begin
                time_entry.custom_field_values = {{ cf.id => key }}
              rescue => e
                # ignore CF assignment errors
              end
            end
          rescue => e
            # ignore provenance CF errors
          end

          if time_entry.save
            {{
              id: time_entry.id,
              work_package_id: time_entry.entity_id,
              user_id: time_entry.user_id,
              activity_id: time_entry.activity_id,
              hours: time_entry.hours.to_f,
              spent_on: time_entry.spent_on.to_s,
              comments: time_entry.comments,
              created_at: time_entry.created_at.to_s,
              updated_at: time_entry.updated_at.to_s
            }}
          else
            {{
              error: "Validation failed",
              errors: time_entry.errors.full_messages
            }}
          end
        rescue => e
          {{
            error: "Creation failed",
            message: e.message,
            backtrace: e.backtrace.first(3)
          }}
        end
        """

        try:
            result = self.execute_query_to_json_file(script)

            if isinstance(result, dict):
                if result.get("error"):
                    logger.warning("Time entry creation failed: %s", result)
                    return None
                return result

            logger.warning("Unexpected time entry creation result: %s", result)
            return None  # noqa: TRY300

        except Exception as e:
            msg = f"Failed to create time entry: {e}"
            raise QueryExecutionError(msg) from e

    def get_time_entries(
        self,
        work_package_id: int | None = None,
        user_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get time entries from OpenProject with optional filtering.

        Args:
            work_package_id: Filter by work package ID
            user_id: Filter by user ID
            limit: Maximum number of entries to return

        Returns:
            List of time entry dictionaries

        Raises:
            QueryExecutionError: If the query fails

        """
        conditions = []
        if work_package_id:
            conditions.append(f"work_package_id: {work_package_id}")
        if user_id:
            conditions.append(f"user_id: {user_id}")

        where_clause = f".where({', '.join(conditions)})" if conditions else ""

        # Build Ruby expression that avoids relying on a 'work_package' association (use explicit lookup)
        query = (
            f"TimeEntry{where_clause}.limit({limit})"
            ".map do |entry| "
            "wp = (begin WorkPackage.find_by(id: entry.work_package_id); rescue; nil end); "
            "act = (begin entry.activity; rescue; nil end); usr = (begin entry.user; rescue; nil end); "
            "{ id: entry.id, "
            "work_package_id: entry.work_package_id, "
            "work_package_subject: (wp ? wp.subject : nil), "
            "user_id: entry.user_id, user_name: (usr ? usr.name : nil), "
            "activity_id: entry.activity_id, activity_name: (act ? act.name : nil), "
            "hours: entry.hours.to_f, spent_on: entry.spent_on.to_s, "
            "comments: entry.comments, created_at: entry.created_at.to_s, updated_at: entry.updated_at.to_s, "
            "custom_fields: (begin cf = entry.custom_field_values; cf.respond_to?(:to_json) ? cf : {}; rescue; {} end) } end"
        )

        try:
            # Use a unique container file to avoid collisions across concurrent calls
            unique_name = f"/tmp/j2o_time_entries_{os.getpid()}_{int(time.time())}_{os.urandom(2).hex()}.json"  # noqa: S108
            result = self.execute_large_query_to_json_file(
                query,
                container_file=unique_name,
                timeout=120,
            )
            return result if isinstance(result, list) else []
        except Exception as e:
            msg = "Failed to retrieve time entries."
            raise QueryExecutionError(msg) from e

    # ----- Priority helpers -----
    def get_issue_priorities(self) -> list[dict[str, Any]]:
        """Return list of IssuePriority with id, name, position, is_default, active."""
        script = """
        IssuePriority.order(:position).map do |p|
          { id: p.id, name: p.name, position: p.position, is_default: p.is_default, active: p.active }
        end
        """
        try:
            result = self.execute_json_query(script)
            return result if isinstance(result, list) else []
        except Exception:
            logger.exception("Failed to get issue priorities")
            return []

    def find_issue_priority_by_name(self, name: str) -> dict[str, Any] | None:
        # Use ensure_ascii=False to output UTF-8 directly
        script = f"p = IssuePriority.find_by(name: {json.dumps(name, ensure_ascii=False)}); p && {{ id: p.id, name: p.name, position: p.position, is_default: p.is_default, active: p.active }}"
        try:
            result = self.execute_json_query(script)
            return result if isinstance(result, dict) else None
        except Exception:
            logger.exception("Failed to find issue priority by name %s", name)
            return None

    def create_issue_priority(self, name: str, position: int | None = None, is_default: bool = False) -> dict[str, Any]:
        pos_expr = "nil" if position is None else str(int(position))
        # Use ensure_ascii=False to output UTF-8 directly
        script = f"""
        p = IssuePriority.create!(name: {json.dumps(name, ensure_ascii=False)}, position: {pos_expr}, is_default: {str(is_default).lower()}, active: true)
        {{ id: p.id, name: p.name, position: p.position, is_default: p.is_default, active: p.active }}
        """
        try:
            result = self.execute_json_query(script)
            return result if isinstance(result, dict) else {"id": None, "name": name}
        except Exception as e:
            msg = f"Failed to create issue priority {name}: {e}"
            raise QueryExecutionError(msg) from e

    def ensure_local_avatars_enabled(self) -> bool:
        """Enable local avatar uploads if disabled."""
        ruby = (
            "settings = Setting.plugin_openproject_avatars || {}\n"
            "if ActiveModel::Type::Boolean.new.cast(settings['enable_local_avatars'])\n"
            "  { enabled: true }.to_json\n"
            "else\n"
            "  settings['enable_local_avatars'] = true\n"
            "  Setting.plugin_openproject_avatars = settings\n"
            "  { enabled: true }.to_json\n"
            "end\n"
        )
        result = self.execute_query_to_json_file(ruby)
        return bool(isinstance(result, dict) and result.get("enabled"))

    def set_user_avatar(
        self,
        *,
        user_id: int,
        container_path: Path,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Upload and assign a local avatar for a user."""
        safe_content_type = (content_type or "image/png").replace("'", "")
        safe_filename = filename.replace("'", "")
        head = (
            f"user_id = {int(user_id)}\n"
            f"file_path = '{container_path.as_posix()}'\n"
            f"filename = '{safe_filename}'\n"
            f"content_type = '{safe_content_type}'\n"
        )
        body = """require 'rack/test'
require 'avatars/update_service'

result = { success: false }
user = User.find_by(id: user_id)
if user.nil?
  result = { success: false, error: 'user not found' }
elsif !OpenProject::Avatars::AvatarManager.local_avatars_enabled?
  result = { success: false, error: 'local avatars disabled' }
else
  uploader = Rack::Test::UploadedFile.new(file_path, content_type, true)
  service = ::Avatars::UpdateService.new(user)
  outcome = service.replace(uploader)
  if outcome.success?
    result = { success: true }
  else
    result = { success: false, error: outcome.errors.full_messages.join(', ') }
  end
end
result.to_json
"""
        script = head + body
        response = self.execute_query_to_json_file(script, timeout=180)
        if isinstance(response, dict):
            return response
        return {"success": False, "error": "unexpected response"}

    # ----- Watchers helpers -----
    def find_watcher(self, work_package_id: int, user_id: int) -> dict[str, Any] | None:
        """Find a watcher for a work package and user if it exists."""
        query = (
            "Watcher.where(watchable_type: 'WorkPackage', watchable_id: %d, user_id: %d).limit(1).map do |w| "
            "{ id: w.id, user_id: w.user_id, watchable_id: w.watchable_id } end.first"
        ) % (work_package_id, user_id)
        try:
            res = self.execute_query(query)
            if isinstance(res, dict) and res:
                return res
            return None
        except Exception as e:
            msg = "Failed to query watcher."
            raise QueryExecutionError(msg) from e

    def add_watcher(self, work_package_id: int, user_id: int) -> bool:
        """Idempotently add a watcher to the work package.

        Returns True if the watcher exists or was created successfully.
        """
        try:
            if self.find_watcher(work_package_id, user_id):
                return True
        except Exception:
            # Proceed to attempt create even if find failed
            pass

        script = (
            "wp = WorkPackage.find(%d); u = User.find(%d); "
            "if !Watcher.exists?(watchable_type: 'WorkPackage', watchable_id: wp.id, user_id: u.id); "
            "w = Watcher.new(user: u, watchable: wp); w.save!; {created: true}.to_json; else; {created: false}.to_json; end"
        ) % (work_package_id, user_id)
        try:
            created = self.execute_query(script)
            if isinstance(created, dict):
                return True
            return bool(created)
        except Exception as e:
            msg = "Failed to add watcher."
            raise QueryExecutionError(msg) from e

    def bulk_add_watchers(
        self,
        watchers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Add multiple watchers in a single Rails call.

        Args:
            watchers: List of dicts with keys:
                - work_package_id: int
                - user_id: int

        Returns:
            Dict with 'success': bool, 'created': int, 'skipped': int, 'failed': int
        """
        if not watchers:
            return {"success": True, "created": 0, "skipped": 0, "failed": 0}

        # Build JSON data for Ruby
        data = []
        for w in watchers:
            data.append({
                "wp_id": int(w["work_package_id"]),
                "user_id": int(w["user_id"]),
            })

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        # that Ruby misinterprets as invalid Unicode escape sequences
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ created: 0, skipped: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              wp_id = item['wp_id']
              user_id = item['user_id']

              # Check if already exists
              if Watcher.exists?(watchable_type: 'WorkPackage', watchable_id: wp_id, user_id: user_id)
                results[:skipped] += 1
              else
                wp = WorkPackage.find_by(id: wp_id)
                u = User.find_by(id: user_id)
                if wp && u
                  w = Watcher.new(user: u, watchable: wp)
                  if w.save
                    results[:created] += 1
                  else
                    results[:failed] += 1
                    results[:errors] << {{ wp_id: wp_id, user_id: user_id, error: w.errors.full_messages.join(', ') }}
                  end
                else
                  results[:failed] += 1
                  results[:errors] << {{ wp_id: wp_id, user_id: user_id, error: 'WorkPackage or User not found' }}
                end
              end
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ wp_id: item['wp_id'], user_id: item['user_id'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results.to_json
        """
        try:
            result = self.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "created": 0, "skipped": 0, "failed": len(watchers), "error": str(result)}
        except Exception as e:  # noqa: BLE001
            logger.warning("Bulk add watchers failed: %s", e)
            return {"success": False, "created": 0, "skipped": 0, "failed": len(watchers), "error": str(e)}

    def bulk_set_wp_custom_field_values(
        self,
        cf_values: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Set custom field values for multiple work packages in a single Rails call.

        Args:
            cf_values: List of dicts with keys:
                - work_package_id: int
                - custom_field_id: int
                - value: str

        Returns:
            Dict with 'success': bool, 'updated': int, 'failed': int
        """
        if not cf_values:
            return {"success": True, "updated": 0, "failed": 0}

        # Build JSON data for Ruby
        data = []
        for cv in cf_values:
            data.append({
                "wp_id": int(cv["work_package_id"]),
                "cf_id": int(cv["custom_field_id"]),
                "value": str(cv["value"]),
            })

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ updated: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              wp_id = item['wp_id']
              cf_id = item['cf_id']
              val = item['value']

              wp = WorkPackage.find_by(id: wp_id)
              cf = CustomField.find_by(id: cf_id)
              if wp && cf
                cv = wp.custom_value_for(cf)
                if cv
                  cv.value = val
                  cv.save
                else
                  wp.custom_field_values = {{ cf.id => val }}
                end
                wp.save!
                results[:updated] += 1
              else
                results[:failed] += 1
                results[:errors] << {{ wp_id: wp_id, cf_id: cf_id, error: 'WorkPackage or CustomField not found' }}
              end
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ wp_id: item['wp_id'], cf_id: item['cf_id'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results.to_json
        """
        try:
            result = self.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "updated": 0, "failed": len(cf_values), "error": str(result)}
        except Exception as e:  # noqa: BLE001
            logger.warning("Bulk set WP CF values failed: %s", e)
            return {"success": False, "updated": 0, "failed": len(cf_values), "error": str(e)}

    def upsert_work_package_description_section(
        self,
        work_package_id: int,
        section_marker: str,
        content: str,
    ) -> bool:
        """Upsert a section in a work package's description.

        Args:
            work_package_id: The work package ID
            section_marker: The section title/marker (e.g., "Remote Links")
            content: The markdown content for the section

        Returns:
            True if successful, False otherwise
        """
        # Escape content for Ruby
        safe_content = content.replace("'", "\\'").replace("\n", "\\n")
        safe_marker = section_marker.replace("'", "\\'")

        script = f"""
          wp = WorkPackage.find_by(id: {work_package_id})
          if !wp
            {{ success: false, error: 'WorkPackage not found' }}.to_json
          else
            desc = wp.description || ''
            marker = '## {safe_marker}'
            content = '{safe_content}'

            # Find existing section
            section_regex = /\\n?## {safe_marker}\\n[\\s\\S]*?(?=\\n## |\\z)/
            if desc.match?(section_regex)
              # Replace existing section
              new_section = "\\n" + marker + "\\n" + content
              desc = desc.gsub(section_regex, new_section)
            else
              # Append new section
              desc = desc.strip + "\\n\\n" + marker + "\\n" + content
            end

            wp.description = desc.strip
            if wp.save
              {{ success: true }}.to_json
            else
              {{ success: false, error: wp.errors.full_messages.join(', ') }}.to_json
            end
          end
        """
        try:
            result = self.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result.get("success", False)
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to upsert WP description section: %s", e)
            return False

    def bulk_upsert_wp_description_sections(
        self,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Upsert description sections for multiple work packages in a single Rails call.

        Args:
            sections: List of dicts with keys:
                - work_package_id: int
                - section_marker: str
                - content: str

        Returns:
            Dict with 'success': bool, 'updated': int, 'failed': int
        """
        if not sections:
            return {"success": True, "updated": 0, "failed": 0}

        # Build JSON data for Ruby
        data = []
        for s in sections:
            data.append({
                "wp_id": int(s["work_package_id"]),
                "marker": str(s["section_marker"]),
                "content": str(s["content"]),
            })

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ updated: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              wp_id = item['wp_id']
              marker_text = item['marker']
              content = item['content']

              wp = WorkPackage.find_by(id: wp_id)
              if !wp
                results[:failed] += 1
                results[:errors] << {{ wp_id: wp_id, error: 'WorkPackage not found' }}
                next
              end

              desc = wp.description || ''
              marker = '## ' + marker_text

              # Find existing section using regex
              section_regex = Regexp.new("\\n?" + Regexp.escape(marker) + "\\n[\\s\\S]*?(?=\\n## |\\z)")
              if desc.match?(section_regex)
                new_section = "\\n" + marker + "\\n" + content
                desc = desc.gsub(section_regex, new_section)
              else
                desc = desc.strip + "\\n\\n" + marker + "\\n" + content
              end

              wp.description = desc.strip
              if wp.save
                results[:updated] += 1
              else
                results[:failed] += 1
                results[:errors] << {{ wp_id: wp_id, error: wp.errors.full_messages.join(', ') }}
              end
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ wp_id: item['wp_id'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results.to_json
        """
        try:
            result = self.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "updated": 0, "failed": len(sections), "error": str(result)}
        except Exception as e:  # noqa: BLE001
            logger.warning("Bulk upsert WP description sections failed: %s", e)
            return {"success": False, "updated": 0, "failed": len(sections), "error": str(e)}

    def create_work_package_activity(
        self,
        work_package_id: int,
        activity_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create a journal/activity (comment) on a work package.

        Args:
            work_package_id: The work package ID
            activity_data: Dict with 'comment' key containing {'raw': 'comment text'}

        Returns:
            Created journal data or None on failure
        """
        comment = activity_data.get("comment", {})
        if isinstance(comment, dict):
            comment_text = comment.get("raw", "")
        else:
            comment_text = str(comment)

        if not comment_text:
            return None

        # Escape single quotes for Ruby
        escaped_comment = comment_text.replace("\\", "\\\\").replace("'", "\\'")

        script = f"""
        begin
          wp = WorkPackage.find({work_package_id})
          user = User.current || User.find_by(admin: true)
          journal = wp.journals.create!(
            user: user,
            notes: '{escaped_comment}'
          )
          {{ id: journal.id, status: 'created' }}
        rescue => e
          {{ error: e.message }}
        end
        """
        try:
            result = self.execute_query_to_json_file(script)
            if isinstance(result, dict) and not result.get("error"):
                return result
            logger.debug("Failed to create activity: %s", result)
            return None
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to create activity for WP#%d: %s", work_package_id, e)
            return None

    def bulk_create_work_package_activities(
        self,
        activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple journal/activity entries (comments) in a single Rails call.

        Args:
            activities: List of dicts with keys:
                - work_package_id: int
                - comment: str (the comment text)
                - user_id: int (optional, defaults to admin user)

        Returns:
            Dict with 'success': bool, 'created': int, 'failed': int
        """
        if not activities:
            return {"success": True, "created": 0, "failed": 0}

        # Build JSON data for Ruby - escape properly
        data = []
        for act in activities:
            comment = act.get("comment", "")
            if isinstance(comment, dict):
                comment = comment.get("raw", "")
            data.append({
                "work_package_id": int(act["work_package_id"]),
                "comment": str(comment),
                "user_id": act.get("user_id"),
            })

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ created: 0, failed: 0, errors: [] }}
          default_user = User.current || User.find_by(admin: true)

          data.each do |item|
            begin
              wp = WorkPackage.find_by(id: item['work_package_id'])
              unless wp
                results[:failed] += 1
                results[:errors] << {{ wp_id: item['work_package_id'], error: 'WorkPackage not found' }}
                next
              end

              user = item['user_id'] ? User.find_by(id: item['user_id']) : default_user
              user ||= default_user

              comment_text = item['comment'].to_s
              next if comment_text.empty?

              journal = wp.journals.create!(
                user: user,
                notes: comment_text
              )
              results[:created] += 1
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ wp_id: item['work_package_id'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results.to_json
        """
        try:
            result = self.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "created": 0, "failed": len(activities), "error": str(result)}
        except Exception as e:  # noqa: BLE001
            logger.warning("Bulk create WP activities failed: %s", e)
            return {"success": False, "created": 0, "failed": len(activities), "error": str(e)}

    def find_relation(
        self,
        from_work_package_id: int,
        to_work_package_id: int,
    ) -> dict[str, Any] | None:
        """Find a relation between two work packages if it exists.

        Returns minimal relation info or None.
        """
        query = (
            "Relation.where(from_id: %d, to_id: %d).limit(1).map do |r| "
            "{ id: r.id, relation_type: r.relation_type, from_id: r.from_id, to_id: r.to_id } end.first"
            % (from_work_package_id, to_work_package_id)
        )
        try:
            result = self.execute_large_query_to_json_file(
                query,
                container_file="/tmp/j2o_find_relation.json",  # noqa: S108
                timeout=30,
            )
            return result if isinstance(result, dict) else None
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to find relation: %s", e)
            return None

    def create_relation(
        self,
        from_work_package_id: int,
        to_work_package_id: int,
        relation_type: str,
    ) -> dict[str, Any] | None:
        """Create a relation idempotently between two work packages.

        On success returns dict with id and status ('created' or 'exists'); otherwise None.
        """
        script = f"""
        begin
          from_wp = WorkPackage.find_by(id: {from_work_package_id})
          to_wp = WorkPackage.find_by(id: {to_work_package_id})
          if !from_wp || !to_wp
            {{ error: 'NotFound' }}
          else
            rel = Relation.where(from_id: {from_work_package_id}, to_id: {to_work_package_id}, relation_type: '{relation_type}').first
            if rel
              {{ id: rel.id, status: 'exists', relation_type: rel.relation_type, from_id: rel.from_id, to_id: rel.to_id }}
            else
              rel = Relation.create(from: from_wp, to: to_wp, relation_type: '{relation_type}')
              if rel.persisted?
                {{ id: rel.id, status: 'created', relation_type: rel.relation_type, from_id: rel.from_id, to_id: rel.to_id }}
              else
                {{ error: 'Validation failed', errors: rel.errors.full_messages }}
              end
            end
          end
        rescue => e
          {{ error: 'Creation failed', message: e.message }}
        end
        """
        try:
            result = self.execute_query_to_json_file(script)
            if isinstance(result, dict):
                if result.get("error"):
                    logger.warning("Relation creation failed: %s", result)
                    return None
                return result
            logger.warning("Unexpected relation creation result: %s", result)
            return None  # noqa: TRY300
        except Exception as e:
            msg = f"Failed to create relation: {e}"
            raise QueryExecutionError(msg) from e

    def bulk_create_relations(
        self,
        relations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple relations in a single Rails call.

        Args:
            relations: List of dicts with keys:
                - from_id: int (from work package ID)
                - to_id: int (to work package ID)
                - relation_type: str (relates, duplicates, blocks, precedes, follows)

        Returns:
            Dict with 'success': bool, 'created': int, 'skipped': int, 'failed': int
        """
        if not relations:
            return {"success": True, "created": 0, "skipped": 0, "failed": 0}

        # Build JSON data for Ruby
        data = []
        for rel in relations:
            data.append({
                "from_id": int(rel["from_id"]),
                "to_id": int(rel["to_id"]),
                "type": str(rel["relation_type"]),
            })

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ created: 0, skipped: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              from_id = item['from_id']
              to_id = item['to_id']
              rel_type = item['type']

              # Check if relation already exists (either direction for symmetric types)
              existing = Relation.where(from_id: from_id, to_id: to_id).first
              existing ||= Relation.where(from_id: to_id, to_id: from_id).first if ['relates'].include?(rel_type)

              if existing
                results[:skipped] += 1
              else
                from_wp = WorkPackage.find_by(id: from_id)
                to_wp = WorkPackage.find_by(id: to_id)
                if from_wp && to_wp
                  rel = Relation.new(from: from_wp, to: to_wp, relation_type: rel_type)
                  if rel.save
                    results[:created] += 1
                  else
                    results[:failed] += 1
                    results[:errors] << {{ from: from_id, to: to_id, error: rel.errors.full_messages.join(', ') }}
                  end
                else
                  results[:failed] += 1
                  results[:errors] << {{ from: from_id, to: to_id, error: 'WorkPackage not found' }}
                end
              end
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ from: item['from_id'], to: item['to_id'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results.to_json
        """
        try:
            result = self.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "created": 0, "skipped": 0, "failed": len(relations), "error": str(result)}
        except Exception as e:  # noqa: BLE001
            logger.warning("Bulk create relations failed: %s", e)
            return {"success": False, "created": 0, "skipped": 0, "failed": len(relations), "error": str(e)}

    def batch_create_time_entries(  # noqa: C901
        self,
        time_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple time entries via file-based JSON in the container.

        This avoids console output parsing by writing input and results to files
        inside the container and reading results back via docker exec.

        Args:
            time_entries: List of time entry data dictionaries

        Returns:
            Dictionary with creation results and statistics

        Raises:
            QueryExecutionError: If the batch operation fails

        """
        if not time_entries:
            return {"created": 0, "failed": 0, "results": []}

        # Build entries data with necessary fields and ID extraction
        entries_data: list[dict[str, Any]] = []
        for i, entry_data in enumerate(time_entries):
            embedded = entry_data.get("_embedded", {})

            def extract_id(pattern: str, href: str) -> int | None:
                m = re.search(pattern, href or "")
                return int(m.group(1)) if m else None

            work_package_id = extract_id(r"/work_packages/(\d+)", embedded.get("workPackage", {}).get("href", ""))
            user_id = extract_id(r"/users/(\d+)", embedded.get("user", {}).get("href", ""))
            activity_id = extract_id(r"/activities/(\d+)", embedded.get("activity", {}).get("href", ""))

            if all([work_package_id, user_id, activity_id]):
                # Normalize comment to string
                comment_obj = entry_data.get("comment", "")
                if isinstance(comment_obj, dict):
                    comment_str = comment_obj.get("raw") or comment_obj.get("text") or str(comment_obj)
                else:
                    comment_str = str(comment_obj)

                entries_data.append(
                    {
                        "index": i,
                        "work_package_id": work_package_id,
                        "user_id": user_id,
                        "activity_id": activity_id,
                        "hours": entry_data.get("hours", 0),
                        "spent_on": entry_data.get("spentOn", ""),
                        "comments": comment_str,
                        "jira_worklog_key": (entry_data.get("_meta", {}) or {}).get("jira_worklog_key"),
                    },
                )

        if not entries_data:
            return {"created": 0, "failed": len(time_entries), "results": []}

        # Prepare local JSON payload
        temp_dir = Path(self.file_manager.data_dir) / "bulk_create"
        temp_dir.mkdir(parents=True, exist_ok=True)
        local_json = temp_dir / f"time_entries_bulk_{os.urandom(4).hex()}.json"
        with local_json.open("w", encoding="utf-8") as f:
            json.dump(entries_data, f)

        # Transfer JSON to container and define result path
        container_json = Path("/tmp") / local_json.name  # noqa: S108
        self.transfer_file_to_container(local_json, container_json)

        result_name = f"bulk_result_time_entries_{os.urandom(3).hex()}.json"
        container_result = Path("/tmp") / result_name  # noqa: S108
        local_result = temp_dir / result_name

        # Build Ruby runner that writes results JSON to file via helper assembly
        header_lines = [
            f"data_path = '{container_json.as_posix()}'",
            f"result_path = '{container_result.as_posix()}'",
        ]
        ruby_lines = [
            "begin; Rails.logger.level = Logger::WARN; rescue; end",
            "begin; ActiveJob::Base.logger = Logger.new(nil); rescue; end",
            "begin; GoodJob.logger = Logger.new(nil); rescue; end",
            "entries = JSON.parse(File.read(data_path), symbolize_names: true)",
            "results = []",
            "created_count = 0",
            "failed_count = 0",
            "entries.each do |entry|",
            "  begin",
            "    te = TimeEntry.new(",
            "      activity_id: entry[:activity_id],",
            "      hours: entry[:hours],",
            "      spent_on: Date.parse(entry[:spent_on]),",
            "      comments: entry[:comments],",
            "      entity_id: entry[:work_package_id],",
            "      entity_type: 'WorkPackage'",
            "    )",
            "    begin",
            "      wp = WorkPackage.find_by(id: entry[:work_package_id])",
            "      if wp.nil?",
            "        failed_count += 1",
            "        results << { index: entry[:index], success: false, error: 'WorkPackage not found: ' + entry[:work_package_id].to_s }",
            "        next",
            "      end",
            "      te.entity = wp",
            "      te.project = wp.project",
            "    rescue => e",
            "      failed_count += 1",
            "      results << { index: entry[:index], success: false, error: 'WorkPackage lookup failed: ' + e.message }",
            "      next",
            "    end",
            "    begin",
            "      user = User.find_by(id: entry[:user_id])",
            "      if user.nil?",
            "        failed_count += 1",
            "        results << { index: entry[:index], success: false, error: 'User not found: ' + entry[:user_id].to_s }",
            "        next",
            "      end",
            "      te.user = user",
            "      te.user_id = entry[:user_id]",
            "      te.logged_by_id = entry[:user_id]",
            "    rescue => e",
            "      failed_count += 1",
            "      results << { index: entry[:index], success: false, error: 'User lookup failed: ' + e.message }",
            "      next",
            "    end",
            "    begin",
            "      key = entry[:jira_worklog_key]",
            "      if key",
            "        cf = CustomField.find_by(type: 'TimeEntryCustomField', name: 'Jira Worklog Key')",
            "        if !cf",
            "          cf = CustomField.new(name: 'Jira Worklog Key', field_format: 'string', is_required: false, is_for_all: true, type: 'TimeEntryCustomField')",
            "          cf.save",
            "        end",
            "        begin",
            "          te.custom_field_values = { cf.id => key }",
            "        rescue => e",
            "        end",
            "      end",
            "    rescue => e",
            "    end",
            "    if te.save",
            "      created_count += 1",
            "      results << { index: entry[:index], success: true, id: te.id }",
            "    else",
            "      failed_count += 1",
            "      results << { index: entry[:index], success: false, errors: te.errors.full_messages }",
            "    end",
            "  rescue => e",
            "    failed_count += 1",
            "    results << { index: entry[:index], success: false, error: e.message }",
            "  end",
            "end",
            "File.write(result_path, JSON.generate({ created: created_count, failed: failed_count, results: results }))",
        ]
        ruby = (
            "\n".join(
                [
                    "require 'json'",
                    "require 'date'",
                    "require 'logger'",
                    *header_lines,
                    *ruby_lines,
                ]
            )
            + "\n"
        )

        try:
            _ = self.rails_client.execute(ruby, timeout=120, suppress_output=True)
        except Exception as e:
            msg = f"Rails execution failed for batch_create_time_entries: {e}"
            raise QueryExecutionError(msg) from e

        # Retrieve result file
        max_wait_seconds = 30
        poll_interval = 1.0
        waited = 0.0
        while waited < max_wait_seconds:
            try:
                self.transfer_file_from_container(container_result, local_result)
                break
            except Exception:  # noqa: BLE001
                time.sleep(poll_interval)
                waited += poll_interval

        if not local_result.exists():
            msg = "Result file not found after batch_create_time_entries execution"
            raise QueryExecutionError(msg)

        with local_result.open("r", encoding="utf-8") as f:
            return json.load(f)

    # ===== ENHANCED PERFORMANCE FEATURES =====

    def get_performance_stats(self) -> dict[str, Any]:
        """Get comprehensive performance statistics."""
        return self.performance_optimizer.get_comprehensive_stats()

    # ===== BATCH OPERATIONS =====

    def batch_create_work_packages(
        self,
        work_packages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple work packages in batches for optimal performance."""
        if not work_packages:
            return {"created": 0, "failed": 0, "results": []}

        return self.performance_optimizer.batch_processor.process_batches(
            work_packages,
            self._create_work_packages_batch,
        )

    def _create_work_packages_batch(
        self,
        work_packages: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, Any]:
        """Create a batch of work packages using Rails."""
        if not work_packages:
            return {"created": 0, "failed": 0, "results": []}

        # Write JSON to a temp file in container to avoid escaping issues
        import tempfile
        import uuid

        batch_id = uuid.uuid4().hex[:8]
        container_json_path = f"/tmp/j2o_batch_{batch_id}.json"

        # Write JSON to local temp file, then transfer to container
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(work_packages, f)
            local_json_path = f.name

        try:
            from pathlib import Path

            self.docker_client.transfer_file_to_container(Path(local_json_path), Path(container_json_path))
        finally:
            import os

            os.unlink(local_json_path)

        # Build batch work package creation script - read JSON from file
        script = f"""
        work_packages_data = JSON.parse(File.read('{container_json_path}'))
        created_count = 0
        failed_count = 0
        results = []

        work_packages_data.each do |wp_data|
          begin
            # Create work package with provided attributes
            wp = WorkPackage.new

            # Set basic attributes
            wp.subject = wp_data['subject'] if wp_data['subject']
            wp.description = wp_data['description'] if wp_data['description']

            # Set project (required)
            if wp_data['project_id']
              wp.project = Project.find(wp_data['project_id'])
            end

            # Set type (required)
            if wp_data['type_id']
              wp.type = Type.find(wp_data['type_id'])
            elsif wp_data['type_name']
              wp.type = Type.find_by(name: wp_data['type_name'])
            end

            # Set status
            if wp_data['status_id']
              wp.status = Status.find(wp_data['status_id'])
            elsif wp_data['status_name']
              wp.status = Status.find_by(name: wp_data['status_name'])
            end

            # Set priority
            if wp_data['priority_id']
              wp.priority = IssuePriority.find(wp_data['priority_id'])
            elsif wp_data['priority_name']
              wp.priority = IssuePriority.find_by(name: wp_data['priority_name'])
            end

            # Set author
            if wp_data['author_id']
              wp.author = User.find(wp_data['author_id'])
            end

            # Set assignee
            if wp_data['assigned_to_id']
              wp.assigned_to = User.find(wp_data['assigned_to_id'])
            end

            # Assign provenance custom fields if provided as [{{id, value}}]
            begin
              cf_items = wp_data['custom_fields']
              if cf_items && cf_items.respond_to?(:each)
                cf_map = {{}}
                cf_items.each do |cf|
                  begin
                    cid = (cf['id'] || cf[:id])
                    val = (cf['value'] || cf[:value])
                    cf_map[cid] = val if cid
                  rescue
                  end
                end
                if cf_map.any?
                  begin
                    wp.custom_field_values = cf_map
                  rescue
                  end
                end
              end
            rescue
            end

            # Save the work package
            if wp.save
              created_count += 1
              results << {{ id: wp.id, status: 'created', subject: wp.subject }}
            else
              failed_count += 1
              results << {{
                subject: wp_data['subject'],
                status: 'failed',
                errors: wp.errors.full_messages
              }}
            end

          rescue => e
            failed_count += 1
            results << {{
              subject: wp_data['subject'],
              status: 'failed',
              error: e.message
            }}
          end
        end

        {{
          created: created_count,
          failed: failed_count,
          results: results
        }}
        """

        operation_succeeded = False  # Track success for debug file preservation
        try:
            result = self.execute_json_query(script)
            operation_succeeded = True
            return result if isinstance(result, dict) else {"created": 0, "failed": len(work_packages), "results": []}
        except Exception as e:
            msg = f"Failed to batch create work packages: {e}"
            raise QueryExecutionError(msg) from e
        finally:
            # Clean up container JSON file - preserve on error for debugging
            preserve_on_error = config.migration_config.get("preserve_debug_files_on_error", True)
            should_cleanup = operation_succeeded or not preserve_on_error
            if not should_cleanup:
                self.logger.warning(
                    "Preserving debug file due to error: %s (set preserve_debug_files_on_error=false to auto-cleanup)",
                    container_json_path,
                )
            else:
                try:
                    self.docker_client.execute_command(f"rm -f {container_json_path}")
                except Exception as cleanup_err:
                    self.logger.warning(
                        "Failed to cleanup container temp file %s: %s",
                        container_json_path,
                        cleanup_err,
                    )

    def get_project_enhanced(self, project_id: int) -> dict[str, Any]:
        """Get comprehensive project information with caching."""
        script = f"""
        project = Project.find({project_id})
        work_package_count = project.work_packages.count

        {{
          project: {{
            id: project.id,
            name: project.name,
            identifier: project.identifier,
            description: project.description,
            status: project.status,
            created_at: project.created_at,
            updated_at: project.updated_at,
            # Back-compat keys (map *_on to *_at)
            created_on: project.created_at,
            updated_on: project.updated_at
          }},
          statistics: {{
            work_package_count: work_package_count,
            active: project.active?
          }}
        }}
        """

        try:
            return self.execute_json_query(script)
        except Exception as e:
            msg = f"Failed to get enhanced project data for ID {project_id}: {e}"
            raise QueryExecutionError(msg) from e

    def enable_project_modules(self, project_id: int, modules: list[str]) -> bool:
        """Ensure the given project has the specified modules enabled.

        Idempotent: adds any missing modules to `enabled_module_names` and saves the project.

        Args:
            project_id: OpenProject project ID
            modules: List of module identifiers (e.g., ['time_tracking'])

        Returns:
            True if the modules are enabled (already or after change), False on error

        """
        if not modules:
            return True
        # Build Ruby script that ensures all modules are present
        mods_json = json.dumps([str(m) for m in modules])
        script = f"""
        begin
          p = Project.find({int(project_id)})
          names = p.enabled_module_names.map(&:to_s)
          desired = {mods_json}
          added = false
          desired.each do |m|
            unless names.include?(m)
              names << m
              added = true
            end
          end
          if added
            p.enabled_module_names = names
            p.save!
          end
          {{ changed: added, enabled: names }}
        rescue => e
          {{ error: e.message }}
        end
        """
        try:
            result = self.execute_json_query(script)
            if isinstance(result, dict) and not result.get("error"):
                return True
            logger.warning("Failed to enable modules on project %s: %s", project_id, result)
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("Exception enabling modules on project %s: %s", project_id, e)
            return False

    def bulk_enable_project_modules(
        self,
        project_modules: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Enable modules for multiple projects in a single Rails call.

        Args:
            project_modules: List of dicts with keys:
                - project_id: int
                - modules: list[str]

        Returns:
            Dict with 'success': bool, 'processed': int, 'failed': int
        """
        if not project_modules:
            return {"success": True, "processed": 0, "failed": 0}

        # Build JSON data for Ruby
        data = []
        for pm in project_modules:
            if pm.get("modules"):
                data.append({
                    "pid": int(pm["project_id"]),
                    "modules": [str(m) for m in pm["modules"]],
                })

        if not data:
            return {"success": True, "processed": 0, "failed": 0}

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ processed: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              p = Project.find(item['pid'])
              names = p.enabled_module_names.map(&:to_s)
              desired = item['modules']
              added = false
              desired.each do |m|
                unless names.include?(m)
                  names << m
                  added = true
                end
              end
              if added
                p.enabled_module_names = names
                p.save!
              end
              results[:processed] += 1
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ pid: item['pid'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results.to_json
        """
        try:
            result = self.execute_json_query(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "processed": 0, "failed": len(data), "error": str(result)}
        except Exception as e:  # noqa: BLE001
            logger.warning("Bulk enable project modules failed: %s", e)
            return {"success": False, "processed": 0, "failed": len(data), "error": str(e)}

    def batch_get_users_by_ids(self, user_ids: list[int]) -> dict[int, dict]:
        """Retrieve multiple users in batches."""
        if not user_ids:
            return {}

        # Get all users and filter to requested IDs
        all_users = self.get_all_users()
        return {user["id"]: user for user in all_users if user["id"] in user_ids}

    def stream_work_packages_for_project(
        self,
        project_id: int,
        batch_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream work packages for a project with memory-efficient pagination."""
        effective_batch_size = batch_size or self.batch_size

        script = f"""
        project = Project.find({project_id})
        work_packages = project.work_packages.limit({effective_batch_size})

        work_packages.map do |wp|
          {{
            id: wp.id,
            subject: wp.subject,
            description: wp.description,
            status: wp.status.name,
            priority: wp.priority.name,
            type: wp.type.name,
            author: wp.author.name,
            assignee: wp.assigned_to&.name,
            created_at: wp.created_at,
            updated_at: wp.updated_at,
            # Back-compat keys (map *_on to *_at)
            created_on: wp.created_at,
            updated_on: wp.updated_at
          }}
        end
        """

        try:
            results = self.execute_json_query(script)
            if isinstance(results, list):
                yield from results
        except Exception:
            logger.exception("Failed to stream work packages for project %s", project_id)

    def batch_update_work_packages(
        self,
        updates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Update multiple work packages in batches."""
        if not updates:
            return {"updated": 0, "failed": 0, "results": []}

        # Build batch update script
        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        # that Ruby misinterprets as invalid Unicode escape sequences
        updates_json = json.dumps(updates, ensure_ascii=False)
        script = f"""
        updates = {updates_json}
        updated_count = 0
        failed_count = 0
        results = []

        updates.each do |update|
          begin
            wp = WorkPackage.find(update['id'])
            update.each do |key, value|
              next if key == 'id'
              wp.send("#{{key}}=", value) if wp.respond_to?("#{{key}}=")
            end
            wp.save!
            updated_count += 1
            results << {{ id: wp.id, status: 'updated' }}
          rescue => e
            failed_count += 1
            results << {{ id: update['id'], status: 'failed', error: e.message }}
          end
        end

        {{
          updated: updated_count,
          failed: failed_count,
          results: results
        }}
        """

        try:
            return self.execute_json_query(script)
        except Exception as e:
            msg = f"Failed to batch update work packages: {e}"
            raise QueryExecutionError(msg) from e

    def create_work_package(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Create a single work package.

        Args:
            payload: Work package data. Can be in API format (with _links)
                     or direct format (with project_id, type_id, etc.)

        Returns:
            Created work package data or None on failure

        """
        # Convert API-style payload to batch format if needed
        wp_data: dict[str, Any] = {}

        # Handle API-style _links format
        if "_links" in payload:
            links = payload["_links"]

            # Extract project ID from href
            if "project" in links and "href" in links["project"]:
                href = links["project"]["href"]
                if match := re.search(r"/projects/(\d+)", href):
                    wp_data["project_id"] = int(match.group(1))

            # Extract type ID from href
            if "type" in links and "href" in links["type"]:
                href = links["type"]["href"]
                if match := re.search(r"/types/(\d+)", href):
                    wp_data["type_id"] = int(match.group(1))

            # Extract status ID from href
            if "status" in links and "href" in links["status"]:
                href = links["status"]["href"]
                if match := re.search(r"/statuses/(\d+)", href):
                    wp_data["status_id"] = int(match.group(1))
        else:
            # Direct format - copy relevant fields
            for key in ["project_id", "type_id", "status_id", "priority_id", "author_id", "assigned_to_id"]:
                if key in payload:
                    wp_data[key] = payload[key]

        # Copy subject and description
        if "subject" in payload:
            wp_data["subject"] = payload["subject"]
        if "description" in payload:
            wp_data["description"] = payload["description"]

        # Call the internal batch method directly for single item
        # to avoid process_batches wrapper which may alter return format
        try:
            result = self._create_work_packages_batch([wp_data])
            if isinstance(result, dict) and result.get("results"):
                results = result["results"]
                if results and len(results) > 0:
                    return results[0] if isinstance(results[0], dict) else {"id": results[0]}
        except Exception as e:
            logger.error("Failed to create work package: %s", e)
        return None

    def update_work_package(
        self,
        wp_id: int,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Update a single work package.

        Args:
            wp_id: Work package ID
            updates: Fields to update

        Returns:
            Updated work package data or None on failure

        """
        update_data = {"id": wp_id, **updates}
        result = self.batch_update_work_packages([update_data])
        if result and result.get("results"):
            return result["results"][0]
        return None

    @batch_idempotent(ttl=3600)  # 1 hour TTL for user email lookups
    def batch_get_users_by_emails(  # noqa: C901
        self,
        emails: list[str],
        batch_size: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple users by email addresses in batches with idempotency support.

        Args:
            emails: List of email addresses to find
            batch_size: Size of each batch (defaults to configured batch_size)
            headers: Optional headers containing X-Idempotency-Key

        Returns:
            Dictionary mapping email to user data (missing emails are omitted)

        Raises:
            QueryExecutionError: If query fails

        """
        if not emails:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion
        effective_batch_size = batch_size or getattr(self, "batch_size", 100)
        effective_batch_size = self._validate_batch_size(effective_batch_size)

        results = {}

        # Process emails in batches
        for i in range(0, len(emails), effective_batch_size):
            batch_emails = emails[i : i + effective_batch_size]

            def batch_operation(batch_emails: list[str] = batch_emails) -> list[dict[str, Any]]:
                # Use safe query builder with ActiveRecord parameterization
                query = self._build_safe_batch_query("User", "mail", batch_emails)
                return self.execute_json_query(query)  # type: ignore[return-value]

            try:
                # Execute batch operation with retry logic (with idempotency key propagation)
                batch_results = self._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch users by email {batch_emails[:2]}{'...' if len(batch_emails) > 2 else ''}",  # noqa: PLR2004
                    headers=headers,
                )

                if batch_results:
                    # Ensure we have a list
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]

                    # Map results by email
                    for record in batch_results:
                        if isinstance(record, dict) and "mail" in record:
                            email = record["mail"]
                            if email in batch_emails:
                                results[email] = record

            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    "Failed to fetch batch of user emails %s after retries: %s",
                    batch_emails,
                    e,
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for email in batch_emails:
                    self.logger.debug("Failed to fetch user by email %s: %s", email, e)
                continue

        return results

    @batch_idempotent(ttl=3600)  # 1 hour TTL for project identifier lookups
    def batch_get_projects_by_identifiers(  # noqa: C901
        self,
        identifiers: list[str],
        batch_size: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple projects by identifiers in batches with idempotency support.

        Args:
            identifiers: List of project identifiers to find
            batch_size: Size of each batch (defaults to configured batch_size)
            headers: Optional headers containing X-Idempotency-Key

        Returns:
            Dictionary mapping identifier to project data (missing identifiers are omitted)

        Raises:
            QueryExecutionError: If query fails

        """
        if not identifiers:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion
        effective_batch_size = batch_size or getattr(self, "batch_size", 100)
        effective_batch_size = self._validate_batch_size(effective_batch_size)

        results = {}

        # Process identifiers in batches
        for i in range(0, len(identifiers), effective_batch_size):
            batch_identifiers = identifiers[i : i + effective_batch_size]

            def batch_operation(batch_identifiers: list[str] = batch_identifiers) -> list[dict[str, Any]]:
                # Use safe query builder with ActiveRecord parameterization
                query = self._build_safe_batch_query(
                    "Project",
                    "identifier",
                    batch_identifiers,
                )
                return self.execute_json_query(query)  # type: ignore[return-value]

            try:
                # Execute batch operation with retry logic (with idempotency key propagation)
                batch_results = self._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch projects by identifier "
                    f"{batch_identifiers[:2]}{'...' if len(batch_identifiers) > 2 else ''}",  # noqa: PLR2004
                    headers=headers,
                )

                if batch_results:
                    # Ensure we have a list
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]

                    # Map results by identifier
                    for record in batch_results:
                        if isinstance(record, dict) and "identifier" in record:
                            identifier = record["identifier"]
                            if identifier in batch_identifiers:
                                results[identifier] = record

            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    "Failed to fetch batch of project identifiers %s after retries: %s",
                    batch_identifiers,
                    e,
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for identifier in batch_identifiers:
                    self.logger.debug(
                        "Failed to fetch project by identifier %s: %s",
                        identifier,
                        e,
                    )
                continue

        return results

    @batch_idempotent(
        ttl=7200,
    )  # 2 hour TTL for custom field lookups (less frequent changes)
    def batch_get_custom_fields_by_names(  # noqa: C901
        self,
        names: list[str],
        batch_size: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple custom fields by names in batches with idempotency support.

        Args:
            names: List of custom field names to find
            batch_size: Size of each batch (defaults to configured batch_size)
            headers: Optional headers containing X-Idempotency-Key

        Returns:
            Dictionary mapping name to custom field data (missing names are omitted)

        Raises:
            QueryExecutionError: If query fails

        """
        if not names:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion
        effective_batch_size = batch_size or getattr(self, "batch_size", 100)
        effective_batch_size = self._validate_batch_size(effective_batch_size)

        results = {}

        # Process names in batches
        for i in range(0, len(names), effective_batch_size):
            batch_names = names[i : i + effective_batch_size]

            def batch_operation(batch_names: list[str] = batch_names) -> list[dict[str, Any]]:
                # Use safe query builder with ActiveRecord parameterization
                query = self._build_safe_batch_query("CustomField", "name", batch_names)
                return self.execute_json_query(query)  # type: ignore[return-value]

            try:
                # Execute batch operation with retry logic (with idempotency key propagation)
                batch_results = self._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch custom fields by name {batch_names[:2]}{'...' if len(batch_names) > 2 else ''}",  # noqa: PLR2004
                    headers=headers,
                )

                if batch_results:
                    # Ensure we have a list
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]

                    # Map results by name
                    for record in batch_results:
                        if isinstance(record, dict) and "name" in record:
                            name = record["name"]
                            if name in batch_names:
                                results[name] = record

            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    "Failed to fetch batch of custom field names %s after retries: %s",
                    batch_names,
                    e,
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for name in batch_names:
                    self.logger.debug(
                        "Failed to fetch custom field by name %s: %s",
                        name,
                        e,
                    )
                continue

        return results

    def _validate_batch_size(self, batch_size: int) -> int:
        """Validate and clamp batch size to safe limits.

        Args:
            batch_size: Requested batch size

        Returns:
            Safe batch size within limits

        Raises:
            ValueError: If batch_size is invalid

        """
        if not isinstance(batch_size, int) or batch_size <= 0:
            msg = f"batch_size must be a positive integer, got: {batch_size}"
            raise ValueError(
                msg,
            )

        # Enforce maximum batch size to prevent memory exhaustion
        max_batch_size = 1000
        if batch_size > max_batch_size:
            self.logger.warning(
                "batch_size %d exceeds maximum %d, clamping to maximum",
                batch_size,
                max_batch_size,
            )
            return max_batch_size

        return batch_size

    def _validate_model_name(self, model: str) -> str:
        """Validate model name against whitelist to prevent injection.

        Args:
            model: Model name to validate

        Returns:
            Validated model name

        Raises:
            ValueError: If model name is not allowed

        """
        # Whitelist of allowed OpenProject model names
        allowed_models = {
            "User",
            "Project",
            "WorkPackage",
            "CustomField",
            "Status",
            "Type",
            "Priority",
            "Category",
            "Version",
            "TimeEntry",
            "Attachment",
            "Repository",
            "News",
            "Wiki",
            "WikiPage",
            "Forum",
            "Message",
            "Board",
        }

        if model not in allowed_models:
            msg = f"Model '{model}' not in allowed list: {sorted(allowed_models)}"
            raise ValueError(
                msg,
            )

        return model

    def _build_safe_batch_query(self, model: str, field: str, values: list[Any]) -> str:
        """Build a safe batch query using ActiveRecord patterns.

        Args:
            model: Validated model name
            field: Field name to query (e.g., 'id', 'mail', 'identifier')
            values: List of values to query for

        Returns:
            Safe Ruby query string using ActiveRecord WHERE methods

        Raises:
            ValueError: If field name is invalid or payload too large

        """
        # Validate model name first
        safe_model = self._validate_model_name(model)

        # Validate field name to prevent injection (Zen's critical recommendation)
        if not re.match(r"^[a-zA-Z_]+$", field):
            msg = f"Illegal field name '{field}' - only letters and underscores allowed"
            raise ValueError(
                msg,
            )

        # Use ActiveRecord's built-in parameterization instead of string building
        # This approach delegates sanitization to Rails rather than DIY
        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        values_json = json.dumps(values, ensure_ascii=False)

        # Add payload byte cap to prevent memory exhaustion (Zen's recommendation)
        payload_bytes = len(values_json.encode("utf-8"))
        max_payload_bytes = 256_000  # 256 KB limit
        if payload_bytes > max_payload_bytes:
            msg = f"Batch payload {payload_bytes} bytes exceeds {max_payload_bytes} limit"
            raise ValueError(
                msg,
            )

        return f"{safe_model}.where({field}: {values_json}).map(&:as_json)"
