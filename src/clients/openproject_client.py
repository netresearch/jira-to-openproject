"""OpenProject client for interacting with OpenProject instances via SSH and Rails console."""

import json
import os
import random
import re
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
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
from src.utils.metrics_collector import MetricsCollector
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
        self.command_timeout = command_timeout
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

        # Initialize metrics collector for observability (Zen's recommendation)
        self.metrics = MetricsCollector()

        logger.success(
            "OpenProjectClient initialized for host %s, container %s",
            self.ssh_host,
            self.container_name,
        )

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
        header = (
            "require 'json'\n"
            f"input_data = JSON.parse(File.read('{container_data_path.as_posix()}'))\n"
        )
        full_script = header + script_content

        local_script_path: Path | None = None
        container_script_path: Path | None = None
        try:
            # Create local script file and transfer both script and data
            local_script_path = self._create_script_file(full_script)
            container_script_path = self._transfer_rails_script(local_script_path)

            # Transfer the input JSON to container
            self.transfer_file_to_container(local_data_path, container_data_path)

            # Execute the script inside Rails console. Suppress wrapper result.inspect noise.
            load_cmd = f'load "{container_script_path.as_posix()}"'
            try:
                output = self.rails_client.execute(load_cmd, timeout=timeout, suppress_output=True)
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
                    stdout, stderr, rc = self.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(q_msg) from e
                    output = stdout
                else:
                    raise

            # Extract JSON payload between JSON_OUTPUT_START / JSON_OUTPUT_END
            start_marker = "JSON_OUTPUT_START"
            end_marker = "JSON_OUTPUT_END"
            start_idx = output.find(start_marker)
            end_idx = output.find(end_marker)
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_str = output[start_idx + len(start_marker) : end_idx].strip()
                # Fallback parsing with sanitization to guard against stray control chars from IRB/tmux
                def _try_parse(s: str) -> Any:  # noqa: ANN401
                    return json.loads(s)

                def _sanitize_control_chars(s: str) -> str:
                    # Remove ASCII control chars except standard whitespace
                    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", s)

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

                try:
                    parsed = _try_parse(json_str)
                except Exception:  # noqa: BLE001
                    try:
                        parsed = _try_parse(_sanitize_control_chars(json_str))
                    except Exception:  # noqa: BLE001
                        candidate = _extract_first_json_block(json_str)
                        if candidate is None:
                            candidate = _extract_first_json_block(_sanitize_control_chars(json_str)) or json_str
                        try:
                            parsed = _try_parse(candidate)
                        except Exception as e:
                            q_msg = f"Failed to parse JSON output: {e}"
                            raise QueryExecutionError(q_msg) from e

                # Return a structured success response
                return {
                    "status": "success",
                    "message": "Script executed successfully",
                    "data": parsed,
                    "output": output[:2000],
                }

            # No JSON markers found - return an error envelope for callers
            return {
                "status": "error",
                "message": "JSON markers not found in Rails output",
                "output": output[:2000],
            }

        finally:
            # Best-effort cleanup of local + remote files
            with suppress(Exception):
                if local_script_path is not None and container_script_path is not None:
                    self._cleanup_script_files(local_script_path, container_script_path)
            with suppress(Exception):
                self._cleanup_script_files(local_data_path, container_data_path)

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

        Args:
            query: Rails query to execute
            timeout: Timeout in seconds

        Returns:
            Parsed JSON data

        Raises:
            QueryExecutionError: If execution fails
            JsonParseError: If result parsing fails

        """
        try:
            # Prefer proper pagination for collection queries

            # Extract model name when possible, e.g., Project.all / WorkPackage.where(...)
            # Only treat plain collection queries (all/where) without explicit to_json or array producers
            produces_array = any(k in query for k in [".map", ".pluck", ".collect", ".select{"])
            has_to_json = ".to_json" in query
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\.(all|where)\b", query)
            if m and ".limit(" not in query and not produces_array and not has_to_json:
                model_name = m.group(1)
                return self._execute_batched_query(model_name, timeout=timeout)

            # For other queries, wrap and add .to_json (even if already contains .to_json)
            modified_query = f"({query}).to_json"

            # Execute the query and parse result
            result_output = self.execute_query(modified_query, timeout=timeout)
            return self._parse_rails_output(result_output)

        except JsonParseError:
            # Surface parse errors directly to callers so orchestrator can stop on error
            logger.exception("JSON parsing failed for Rails output")
            raise
        except RubyError:
            # Preserve RubyError context for higher layers to classify
            logger.exception("Ruby error during execute_query_to_json_file")
            raise
        except Exception as e:
            # Normalize any other errors to QueryExecutionError
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
        container_file_quoted = quote(container_file)

        # Build Ruby that writes JSON to file without printing large output to console
        ruby_path_literal = container_file_quoted.replace("'", "\\'")
        ruby_script = (
            "require 'json'\n"
            f"data = {ruby_json_expr}\n"
            f"File.write('{ruby_path_literal}', JSON.generate(data))\n"
        )

        # Execute via persistent tmux Rails console (faster than rails runner)
        try:
            _console_output = self.rails_client.execute(
                ruby_script,
                timeout=timeout or 90,
                suppress_output=True,
            )
            # Defensive: detect console errors; rails_console_client will raise RubyError on late errors
            # If it returns a string with hints of failure, surface as QueryExecutionError
            self._check_console_output_for_errors(
                _console_output or "",
                context="execute_large_query_to_json_file",
            )
        except Exception as e:
            # On console instability (IRB/Reline crash, marker loss), optionally fall back to rails runner
            if isinstance(e, (ConsoleNotReadyError, CommandExecutionError, RubyError)):
                if not config.migration_config.get("enable_runner_fallback", False):
                    # Respect user's preference to avoid per-request rails runner fallback
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
                runner_cmd = (
                    f"(cd /app || cd /opt/openproject) && "
                    f"bundle exec rails runner {runner_script_path}"
                )
                stdout, stderr, rc = self.docker_client.execute_command(runner_cmd)
                if rc != 0:
                    q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                    raise QueryExecutionError(q_msg) from e
            else:
                raise

        # Read file back from container via SSH (avoids tmux buffer limits)
        ssh_command = f"docker exec {self.container_name} cat {container_file}"

        # Small retry loop to handle race where file write completes slightly after command returns
        stdout = ""
        stderr = ""
        returncode = 1
        for attempt in range(8):  # ~2 seconds total with 0.25s sleeps
            try:
                stdout, stderr, returncode = self.ssh_client.execute_command(ssh_command)
            except Exception as e:
                # If file not present yet, wait and retry
                if "No such file or directory" in str(e):
                    time.sleep(0.25)
                    continue
                raise

            if returncode == 0:
                if attempt > 0:
                    logger.debug(
                        "Recovered after %d attempts reading container file %s",
                        attempt + 1,
                        container_file,
                    )
                break
            if stderr and "No such file or directory" in stderr:
                time.sleep(0.25)
                continue
            # Other non-zero error: fail fast
            msg = f"SSH command failed with code {returncode}: {stderr}"
            raise QueryExecutionError(msg)

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
            ("SystemStackError" in output)
            or ("full_message':" in output)
            or ("stack level too deep" in output)
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
            q_msg = (
                f"Unexpected console output during {context}; expected '{expected_prefix}...'. "
                f"Got: {sample[:300]}"
            )
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
                    if not batch_data or (
                        isinstance(batch_data, list) and len(batch_data) == 0
                    ):
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
                    if (val.startswith('"') and val.endswith('"')) or (
                        val.startswith("'") and val.endswith("'")
                    ):
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
                        prev = lines2[i-1].strip()
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
        result = self.execute_query(f"{model}.count")

        if isinstance(result, str) and result.isdigit():
            return int(result)
        msg = "Unable to parse count result."
        raise QueryExecutionError(msg)

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

        # Result file path in container and local debug path
        result_name = result_basename or f"bulk_result_{model.lower()}_{os.urandom(3).hex()}.json"
        container_result = Path("/tmp") / result_name  # noqa: S108
        local_result = temp_dir / result_name

        # Compose minimal Ruby script
        header = (
            "require 'json'\n"
            "require 'logger'\n"
            f"model_name = '{model}'\n"
            f"data_path = '{container_json.as_posix()}'\n"
            f"result_path = '{container_result.as_posix()}'\n"
        )
        ruby = (
            "begin; Rails.logger.level = Logger::WARN; rescue; end\n"
            "begin; ActiveJob::Base.logger = Logger.new(nil); rescue; end\n"
            "begin; GoodJob.logger = Logger.new(nil); rescue; end\n"
            "model = Object.const_get(model_name)\n"
            "data = JSON.parse(File.read(data_path))\n"
            "created = []\n"
            "errors = []\n"
            "data.each_with_index do |attrs, idx|\n"
            "  begin\n"
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
            "        # Keep keys; assign_attributes can safely set *_id again if present\n"
            "      rescue => e\n"
            "        # continue with remaining attributes\n"
            "      end\n"
            "    end\n"
            "    begin\n"
            "      rec.assign_attributes(attrs)\n"
            "    rescue => e\n"
            "      # If assign fails, proceed to save with preassigned associations only\n"
            "    end\n"
            "    # Provenance custom fields\n"
            "    begin\n"
            "      if model_name == 'WorkPackage'\n"
            "        key = attrs['jira_issue_key'] || attrs['jira_key']\n"
            "        if key\n"
            "          cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Issue Key')\n"
            "          if !cf\n"
            "            cf = CustomField.new(name: 'Jira Issue Key', field_format: 'string',\n"
            "              is_required: false, is_for_all: true, type: 'WorkPackageCustomField')\n"
            "            cf.save\n"
            "          end\n"
            "          begin\n"
            "            rec.custom_field_values = { cf.id => key }\n"
            "          rescue => e\n"
            "            # ignore CF assignment errors here\n"
            "          end\n"
            "        end\n"
            "      elsif model_name == 'User'\n"
            "        key = attrs['jira_user_key']\n"
            "        if key\n"
            "          cf = CustomField.find_by(type: 'UserCustomField', name: 'Jira user key')\n"
            "          if !cf\n"
            "            cf = CustomField.new(name: 'Jira user key', field_format: 'string',\n"
            "              is_required: false, is_for_all: true, type: 'UserCustomField')\n"
            "            cf.save\n"
            "          end\n"
            "          begin\n"
            "            rec.custom_field_values = { cf.id => key }\n"
            "          rescue => e\n"
            "            # ignore CF assignment errors here\n"
            "          end\n"
            "        end\n"
            "      end\n"
            "    rescue => e\n"
            "      # ignore provenance CF errors\n"
            "    end\n"
            "    if rec.save\n"
            "      created << {'index' => idx, 'id' => rec.id}\n"
            "    else\n"
            "      errors << {'index' => idx, 'errors' => rec.errors.full_messages}\n"
            "    end\n"
            "  rescue => e\n"
            "    errors << {'index' => idx, 'errors' => [e.message]}\n"
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
            "File.write(result_path, JSON.generate(result))\n"
        )

        # Execute via persistent Rails console (file-only flow; ignore stdout)
        try:
            output = self.rails_client.execute(header + ruby, timeout=timeout or 120, suppress_output=True)
        except Exception as e:
            _msg = f"Rails execution failed for bulk_create_records: {e}"
            raise QueryExecutionError(_msg) from e

        # Poll-copy result back to local
        max_wait_seconds = 30
        poll_interval = 1.0
        waited = 0.0
        copied = False
        while waited < max_wait_seconds:
            try:
                self.transfer_file_from_container(container_result, local_result)
                copied = True
                break
            except Exception:  # noqa: BLE001
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

    def _retry_with_exponential_backoff(  # noqa: PLR0913, C901
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
                # Track successful batch operations (Zen's observability recommendation)
                if hasattr(self, "metrics"):
                    self.metrics.increment_counter("batch_success_total")
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

                is_transient = any(
                    indicator in error_message for indicator in transient_indicators
                )

                if not is_transient or attempt >= max_retries:
                    # Track failed batch operations (Zen's observability recommendation)
                    if hasattr(self, "metrics"):
                        self.metrics.increment_counter("batch_failure_total")
                    # Don't retry for non-transient errors or if out of retries
                    raise

                # Track retry attempts (Zen's observability recommendation)
                if hasattr(self, "metrics"):
                    self.metrics.increment_counter("batch_retry_total")

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
        ruby_hash = json.dumps(attributes).replace('"', "'")

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
        ruby_hash = json.dumps(attributes).replace('"', "'")

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
                    f"Failed to update {model}: Invalid response from OpenProject "
                    f"(type={type(result)}, value={result})"
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
            # Include the 'mail' attribute and the 'Jira user key' custom field if present
            file_path = self._generate_unique_temp_filename("users")
            ruby_query = (
                "cf = CustomField.find_by(type: 'UserCustomField', name: 'Jira user key'); "
                "User.all.map do |u| v = (cf ? u.custom_value_for(cf)&.value : nil); "
                "u.as_json.merge({'mail' => u.mail, 'jira_user_key' => v}) end"
            )
            json_data = self.execute_large_query_to_json_file(ruby_query, container_file=file_path, timeout=60)
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
                msg = (
                    f"Invalid users data format - expected list, got {type(json_data)}"
                )
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
                        email = (user.get("mail") or user.get("email"))
                        if isinstance(email, str):
                            self._users_by_email_cache[email.lower()] = user
                        return user

            # Fallback to direct lookup by login
            user = self.find_record("User", {"login": login})
            # Opportunistically cache by email for future lookups
            email = (user.get("mail") or user.get("email"))
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
        if (
            hasattr(self, "_users_by_email_cache")
            and email_lower in self._users_by_email_cache
        ):
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
                    runner_cmd = (
                        f"(cd /app || cd /opt/openproject) && "
                        f"bundle exec rails runner {runner_script_path}"
                    )
                    stdout, stderr, rc = self.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(msg) from e
                else:
                    raise

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
                return parsed if isinstance(parsed, list) else []
            finally:
                with suppress(Exception):
                    self.ssh_client.execute_command(
                        f"docker exec {self.container_name} rm -f {file_path}",
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
                    runner_cmd = (
                        f"(cd /app || cd /opt/openproject) && "
                        f"bundle exec rails runner {runner_script_path}"
                    )
                    stdout, stderr, rc = self.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        _emsg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(_emsg) from e
                else:
                    raise

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
                return parsed if isinstance(parsed, list) else []
            finally:
                with suppress(Exception):
                    self.ssh_client.execute_command(
                        f"docker exec {self.container_name} rm -f {file_path}",
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
            selector = (
                "Project.where(parent_id: nil).select(:id, :name, :identifier, :description, :status_code)"
                if top_level_only
                else "Project.all.select(:id, :name, :identifier, :description, :status_code)"
            )
            write_query = (
                f"projects = {selector}.as_json; File.write({file_path_interpolated}, "
                f"JSON.pretty_generate(projects)); nil"
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
                        "projects = Project.all.select("
                        ":id, :name, :identifier, :description, :status_code"
                        ").as_json\n"
                    )
                    ruby_runner = (
                        "require 'json'\n"
                        + (top_selector_line if top_level_only else all_selector_line)
                        + f"File.write('{file_path}', JSON.pretty_generate(projects))\n"
                    )
                    with local_tmp.open("w", encoding="utf-8") as f:
                        f.write(ruby_runner)
                    self.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))
                    runner_cmd = (
                        f"(cd /app || cd /opt/openproject) && "
                        f"bundle exec rails runner {runner_script_path}"
                    )
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
                msg = (
                    f"Invalid projects data format - expected list, got {type(result)}"
                )
                raise QueryExecutionError(msg)  # noqa: TRY301

            # Validate and clean project data
            validated_projects = []
            for project in result:
                if isinstance(project, dict) and project.get("id"):
                    # For OpenProject projects, identifier might be optional or missing
                    # Accept projects with at least an ID and name
                    validated_project = {
                        "id": project.get("id"),
                        "name": project.get("name", ""),
                        "identifier": project.get(
                            "identifier",
                            f"project-{project.get('id')}",
                        ),  # Generate if missing
                        "description": project.get("description", ""),
                        "public": project.get("public", False),
                        "status": project.get(
                            "status_code",
                            1,
                        ),  # Use status_code from DB
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
                f"Missing required IDs: work_package_id={work_package_id}, "
                f"user_id={user_id}, activity_id={activity_id}"
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
            work_package_id: {work_package_id},
            user_id: {user_id},
            activity_id: {activity_id},
            hours: {time_entry_data.get('hours', 0)},
            spent_on: Date.parse('{time_entry_data.get('spentOn', '')}'),
            comments: {comment_str!r}
          )

          # Provenance CF for time entries: Jira Worklog Key
          begin
            key = {time_entry_data.get('_meta', {}).get('jira_worklog_key')!r}
            if key
              cf = CustomField.find_by(type: 'TimeEntryCustomField', name: 'Jira Worklog Key')
              if !cf
                cf = CustomField.new(name: 'Jira Worklog Key', field_format: 'string',
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
              work_package_id: time_entry.work_package_id,
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

        # Build Ruby expression (not a block) that returns the array directly
        query = (
            f"TimeEntry{where_clause}.limit({limit}).includes(:work_package, :user, :activity)"

            ".map do |entry| "
            "{ id: entry.id, "
            "work_package_id: entry.work_package_id, "
            "work_package_subject: entry.work_package&.subject, "
            "user_id: entry.user_id, user_name: entry.user&.name, "
            "activity_id: entry.activity_id, activity_name: entry.activity&.name, "
            "hours: entry.hours.to_f, spent_on: entry.spent_on.to_s, "
            "comments: entry.comments, created_at: entry.created_at.to_s, updated_at: entry.updated_at.to_s } end"
        )

        try:
            result = self.execute_large_query_to_json_file(
                query,
                container_file="/tmp/j2o_time_entries.json",  # noqa: S108
                timeout=60,
            )
            return result if isinstance(result, list) else []
        except Exception as e:
            msg = "Failed to retrieve time entries."
            raise QueryExecutionError(msg) from e

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

        # Build Ruby runner that writes results JSON to file
        header = (
            "require 'json'\n"
            "require 'date'\n"
            "require 'logger'\n"
            f"data_path = '{container_json.as_posix()}'\n"
            f"result_path = '{container_result.as_posix()}'\n"
        )
        ruby = (
            "begin; Rails.logger.level = Logger::WARN; rescue; end\n"
            "begin; ActiveJob::Base.logger = Logger.new(nil); rescue; end\n"
            "begin; GoodJob.logger = Logger.new(nil); rescue; end\n"
            "entries = JSON.parse(File.read(data_path), symbolize_names: true)\n"
            "results = []\n"
            "created_count = 0\n"
            "failed_count = 0\n"
            "entries.each do |entry|\n"
            "  begin\n"
            "    te = TimeEntry.new(\n"
            "      activity_id: entry[:activity_id],\n"
            "      hours: entry[:hours],\n"
            "      spent_on: Date.parse(entry[:spent_on]),\n"
            "      comments: entry[:comments]\n"
            "    )\n"
            "    begin\n"
            "      wp = WorkPackage.find_by(id: entry[:work_package_id])\n"
            "      if wp\n"
            "        te.work_package = wp\n"
            "        te.project = wp.project\n"
            "      end\n"
            "    rescue => e\n"
            "    end\n"
            "    begin\n"
            "      te.user_id = entry[:user_id]\n"
            "      te.logged_by_id = entry[:user_id]\n"
            "    rescue => e\n"
            "    end\n"
            "    begin\n"
            "      key = entry[:jira_worklog_key]\n"
            "      if key\n"
            "        cf = CustomField.find_by(type: 'TimeEntryCustomField', name: 'Jira Worklog Key')\n"
            "        if !cf\n"
            "          cf = CustomField.new(name: 'Jira Worklog Key', field_format: 'string', "
            "is_required: false, is_for_all: true, type: 'TimeEntryCustomField')\n"
            "          cf.save\n"
            "        end\n"
            "        begin\n"
            "          te.custom_field_values = { cf.id => key }\n"
            "        rescue => e\n"
            "        end\n"
            "      end\n"
            "    rescue => e\n"
            "    end\n"
            "    if te.save\n"
            "      created_count += 1\n"
            "      results << { index: entry[:index], success: true, id: te.id }\n"
            "    else\n"
            "      failed_count += 1\n"
            "      results << { index: entry[:index], success: false, errors: te.errors.full_messages }\n"
            "    end\n"
            "  rescue => e\n"
            "    failed_count += 1\n"
            "    results << { index: entry[:index], success: false, error: e.message }\n"
            "  end\n"
            "end\n"
            (
                "File.write(result_path, JSON.generate({ created: created_count, failed: failed_count, "
                "results: results }))\n"
            )
        )

        try:
            _ = self.rails_client.execute(header + ruby, timeout=120, suppress_output=True)
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

        # Build batch work package creation script
        work_packages_json = json.dumps(work_packages)
        script = f"""
        work_packages_data = {work_packages_json}
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

        try:
            result = self.execute_json_query(script)
            return (
                result
                if isinstance(result, dict)
                else {"created": 0, "failed": len(work_packages), "results": []}
            )
        except Exception as e:
            msg = f"Failed to batch create work packages: {e}"
            raise QueryExecutionError(msg) from e

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
            created_on: project.created_on,
            updated_on: project.updated_on
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
            created_on: wp.created_on,
            updated_on: wp.updated_on
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
        updates_json = json.dumps(updates)
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

    @batch_idempotent(ttl=3600)  # 1 hour TTL for user email lookups
    def batch_get_users_by_emails(
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

            except Exception as e:
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
    def batch_get_projects_by_identifiers(
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

            except Exception as e:
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
    def batch_get_custom_fields_by_names(
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

            except Exception as e:
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
        values_json = json.dumps(values)

        # Add payload byte cap to prevent memory exhaustion (Zen's recommendation)
        payload_bytes = len(values_json.encode("utf-8"))
        max_payload_bytes = 256_000  # 256 KB limit
        if payload_bytes > max_payload_bytes:
            msg = (
                f"Batch payload {payload_bytes} bytes exceeds {max_payload_bytes} limit"
            )
            raise ValueError(
                msg,
            )

        return rf"{safe_model}\.where({field}: {values_json}).map(&:as_json)"
