#!/usr/bin/env python3
"""OpenProject API client."""

import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from shlex import quote
from typing import Any, Dict, List, Optional, Union, Callable, Iterator

from src import config
from src.clients.docker_client import DockerClient
from src.clients.rails_console_client import RailsConsoleClient, RubyError
from src.clients.ssh_client import SSHClient
from src.utils.file_manager import FileManager
from src.utils.rate_limiter import create_openproject_rate_limiter
from src.utils.performance_optimizer import (
    PerformanceOptimizer,
    StreamingPaginator,
    cached,
    rate_limited,
    batched
)
from src.utils.config_validation import SecurityValidator, ConfigurationValidationError
from src.utils.metrics_collector import MetricsCollector

logger = config.logger


class OpenProjectError(Exception):
    """Base exception for all OpenProject client errors."""


class ConnectionError(OpenProjectError):
    """Error when connection to OpenProject fails."""


class FileTransferError(OpenProjectError):
    """Error when transferring files to/from OpenProject container."""


class QueryExecutionError(OpenProjectError):
    """Error when executing a query in OpenProject."""


class RecordNotFoundError(OpenProjectError):
    """Error when a record is not found in OpenProject."""


class JsonParseError(OpenProjectError):
    """Error when parsing JSON output from OpenProject."""


class OpenProjectClient:
    """Client for OpenProject operations.

    This is the top-level coordinator that orchestrates the client architecture:
    - SSHClient handles all SSH interactions
    - DockerClient (using SSHClient) handles container interactions
    - RailsConsoleClient handles Rails console interactions.

    All error handling uses exceptions rather than status dictionaries.
    """

    def __init__(
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
        **kwargs
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

        Raises:
            ValueError: If required configuration values are missing

        """
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
            "tmux_session_name", "rails_console"
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
            f"{'Using provided' if ssh_client else 'Initialized'} SSHClient for host {self.ssh_host}",
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
            f"{'Using provided' if docker_client else 'Initialized'} DockerClient for container {self.container_name}",
        )

        # 3. Finally, create or use the Rails console client for executing commands
        self.rails_client = rails_client or RailsConsoleClient(
            tmux_session_name=self.tmux_session_name,
            command_timeout=self.command_timeout,
        )
        logger.debug(
            f"{'Using provided' if rails_client else 'Initialized'} "
            f"RailsConsoleClient with tmux session {self.tmux_session_name}",
        )

        # ===== PERFORMANCE OPTIMIZER SETUP =====
        # Performance configuration from kwargs (passed from migration.py)
        
        # Validate performance configuration parameters using SecurityValidator  
        try:
            cache_size = SecurityValidator.validate_numeric_parameter('cache_size', kwargs.get('cache_size', 1500))
            cache_ttl = SecurityValidator.validate_numeric_parameter('cache_ttl', kwargs.get('cache_ttl', 2400))
            batch_size = SecurityValidator.validate_numeric_parameter('batch_size', kwargs.get('batch_size', 50))
            max_workers = SecurityValidator.validate_numeric_parameter('max_workers', kwargs.get('max_workers', 12))
            rate_limit = SecurityValidator.validate_numeric_parameter('rate_limit_per_sec', kwargs.get('rate_limit', 12.0))
            
            # Validate resource allocation to prevent system overload
            SecurityValidator.validate_resource_allocation(batch_size, max_workers, 2048)  # 2GB memory limit
            
        except ConfigurationValidationError as e:
            logger.error(f"OpenProjectClient configuration validation failed: {e}")
            raise
        
        # Initialize performance optimizer with validated parameters
        self.performance_optimizer = PerformanceOptimizer(
            cache_size=cache_size,
            cache_ttl=cache_ttl,
            batch_size=batch_size,
            max_workers=max_workers,
            rate_limit=rate_limit
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
        """Generate a unique temporary filename to prevent race conditions.
        
        Combines timestamp, process ID, and random component to ensure uniqueness
        across concurrent migration processes.
        
        Args:
            base_name: Base name for the file (e.g., 'users', 'projects')
            
        Returns:
            Unique temporary file path (e.g., '/tmp/users_1703123456_12345_abc123.json')
        """
        timestamp = int(time.time())
        pid = os.getpid()
        random_suffix = format(random.randint(0, 0xffffff), '06x')
        return f"/tmp/{base_name}_{timestamp}_{pid}_{random_suffix}.json"

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
            return file_path
        except OSError:
            error_msg = f"Failed to create script file: {file_path!s}"
            logger.exception(error_msg)
            raise OSError(error_msg) from None
        except Exception:
            error_msg = f"Failed to create script file: {file_path!s}"
            logger.exception(error_msg)
            raise OSError(error_msg) from None

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
            container_path = Path("/tmp") / local_path.name

            self.docker_client.transfer_file_to_container(abs_path, container_path)

            logger.debug(
                "Successfully transferred file to container at %s", container_path
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

    def _cleanup_script_files(self, local_path: Path, remote_path: Path) -> None:
        """Clean up script files after execution.

        Args:
            local_path: Path to the local script file
            remote_path: Path to the remote script file

        """
        # Clean up local file
        try:
            if local_path.exists():
                local_path.unlink()
                logger.debug("Cleaned up local script file: %s", local_path)
        except Exception as e:
            logger.warning("Non-critical error cleaning up local file: %s", e)

        # Clean up remote file
        try:
            command = [
                "rm",
                "-f",
                quote(remote_path.resolve(strict=True).as_posix()),
            ]
            self.ssh_client.execute_command(" ".join(command))
            logger.debug("Cleaned up remote script file: %s", remote_path)
        except Exception as e:
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

    def transfer_file_to_container(
        self, local_path: Path, container_path: Path
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
            unique_id = str(random.randint(10000, 99999))

            # Simple command to echo the ID back
            command = f'puts "OPENPROJECT_CONNECTION_TEST_{unique_id}"'

            # Execute the command
            result = self.rails_client.execute(command)

            # Check if the unique ID is in the response
            return f"OPENPROJECT_CONNECTION_TEST_{unique_id}" in result
        except Exception:
            logger.exception("Connection test failed.")
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
        return self.rails_client._send_command_to_tmux(f"puts ({query})", effective_timeout)

    def execute_query_to_json_file(self, query: str, timeout: int | None = None) -> Any:
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
            # Get the configured batch size from migration config
            # This respects user configuration instead of hardcoded limits
            default_batch_size = config.migration_config.get("batch_size", 100)
            
            # For other queries, execute directly but be careful about automatic modifications
            # Only add .limit() for queries that are clearly meant to return collections
            collection_indicators = [
                "all",
                "where",
                "offset",
                "order",
                "includes",
                "joins",
            ]
            single_record_indicators = [
                "find_by",
                "find(",
                "first",
                "last",
                "count",
                "sum",
                "exists?",
            ]

            # Check if this is a single-record query that shouldn't have .limit() added
            is_single_record = any(
                indicator in query.lower() for indicator in single_record_indicators
            )
            is_collection = any(
                indicator in query.lower() for indicator in collection_indicators
            )

            # Check if query already produces arrays (e.g., .map, .pluck results)
            produces_array = any(
                method in query.lower()
                for method in [".map", ".pluck", ".collect", ".select {"]
            )

            # Check for simple expressions that shouldn't have limits
            is_simple_expression = (
                any(
                    operator in query
                    for operator in [
                        "+",
                        "-",
                        "*",
                        "/",
                        "%",  # Arithmetic operators
                        "==",
                        "!=",
                        "<",
                        ">",
                        "<=",
                        ">=",  # Comparison operators
                        "&&",
                        "||",
                        "!",  # Logical operators
                        "&",
                        "|",
                        "^",
                        "~",
                        "<<",
                        ">>",  # Bitwise operators
                        "**",  # Math operators
                        "puts ",
                        "p ",
                        "pp ",  # Output methods
                    ]
                )
                or query.strip().replace(" ", "").isalnum()
            )  # Simple alphanumeric

            # Ensure the query produces JSON output
            if ".to_json" not in query and not query.strip().endswith(".to_json"):
                if is_simple_expression or (is_single_record and not is_collection):
                    # For simple expressions and single record queries, just add .to_json
                    query = f"({query}).to_json"
                elif is_collection and not produces_array:
                    # Only add .limit() for explicit collection queries that don't already produce arrays
                    if ".limit(" not in query:
                        # Apply configurable limit before any JSON conversion
                        # Use the configured batch size instead of hardcoded value
                        if ".as_json" in query:
                            # If query already has .as_json, we need to restructure it
                            base_query = query.replace(".as_json", "")
                            query = f"({base_query}).limit({default_batch_size}).to_json"
                        else:
                            query = f"({query}).limit({default_batch_size}).to_json"
                    else:
                        query = f"({query}).to_json"
                else:
                    # For other queries (like .map results), just add .to_json
                    query = f"({query}).to_json"

            # Execute the query and parse result
            result_output = self.execute_query(query, timeout=timeout)
            return self._parse_rails_output(result_output)

        except Exception as e:
            logger.error(f"Error in execute_query_to_json_file: {e}")
            raise

    def _execute_batched_query(
        self, model_name: str, timeout: int | None = None
    ) -> list[dict[str, Any]]:
        """Execute a query in batches to avoid any truncation issues."""
        try:
            # First, try a simple non-batched approach for smaller datasets
            # This handles the common case where batching isn't needed
            simple_query = f"{model_name}.limit(50).to_json"
            result_output = self.execute_query(simple_query, timeout=timeout)

            try:
                simple_data = self._parse_rails_output(result_output)

                # If we get valid data and it's less than 50 items, we're done
                if isinstance(simple_data, list) and len(simple_data) < 50:
                    logger.debug(
                        "Retrieved %d total records using simple query",
                        len(simple_data),
                    )
                    return simple_data
                if isinstance(simple_data, list) and len(simple_data) == 50:
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
                        "Unexpected data type from simple query: %s", type(simple_data)
                    )
                    return []
                else:
                    return []

            except Exception as e:
                logger.debug(
                    "Simple query failed, falling back to batched approach: %s", e
                )

            # Fall back to batched approach for larger datasets
            all_results = []
            batch_size = 50  # Increased batch size for better performance
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
                    if offset > 5000:
                        logger.warning("Reached safety limit of 5000 records, stopping")
                        break

                except Exception as e:
                    logger.error("Failed to parse batch at offset %d: %s", offset, e)
                    # Record error for rate limiting adaptation
                    self.rate_limiter.record_response(operation_time, 500)
                    break

            logger.debug(
                "Retrieved %d total records using batched approach", len(all_results)
            )
            return all_results

        except Exception as e:
            logger.error("Batched query failed: %s", e)
            # Return empty list instead of failing completely
            return []

    def _parse_rails_output(self, result_output: str) -> Any:
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
            # Debug: Log the raw output
            logger.debug("Raw result_output: %s", repr(result_output[:500]))

            # Clean up the output by removing tmux markers and Rails prompts
            clean_output = result_output.strip()

            # Split output into lines for processing
            lines = clean_output.split("\n")

            # Look for TMUX command markers to extract content between them
            tmux_start_idx = -1
            tmux_end_idx = -1

            for i, line in enumerate(lines):
                # Look for various start patterns
                if (
                    "TMUX_CMD_START" in line
                    or line.strip().startswith("[{")  # Start of JSON array
                    or (line.strip().startswith("[") and '"id":' in line)
                ):  # JSON array with id field
                    tmux_start_idx = i
                    logger.debug(
                        "Found start marker at line %d: %s", i, repr(line[:100])
                    )
                    break
                elif (
                    line.strip().startswith("open-project(")
                    and i < len(lines) - 1
                    and (
                        lines[i + 1].strip().startswith("[")
                        or lines[i + 1].strip().startswith('{"')
                    )
                ):
                    # Rails prompt followed by JSON
                    tmux_start_idx = i
                    logger.debug(
                        "Found Rails prompt + JSON start at line %d: %s",
                        i,
                        repr(line[:100]),
                    )
                    break

            # Look for end markers
            for i, line in enumerate(lines):
                if "TMUX_CMD_END" in line or line.strip().endswith("}]"):
                    tmux_end_idx = i
                    logger.debug("Found end marker at line %d: %s", i, repr(line[:100]))
                    break

            logger.debug(
                "TMUX marker indices: start=%d, end=%d", tmux_start_idx, tmux_end_idx
            )

            # Extract between markers
            content_lines = []
            if tmux_start_idx >= 0:
                start_line = tmux_start_idx + 1
                if tmux_end_idx >= 0:
                    end_line = tmux_end_idx
                else:
                    end_line = len(lines)

                # Extract all content lines between markers
                content_lines = lines[start_line:end_line]
            else:
                # No markers found, use all lines
                content_lines = lines

            if not content_lines:
                logger.debug("No content found between markers")
                return None

            # Join all content lines to form complete JSON/output
            clean_output = " ".join(
                line.strip() for line in content_lines if line.strip()
            )

            # Log the cleaned output for debugging
            logger.debug("Clean output length: %d characters", len(clean_output))
            logger.debug("Clean output preview: %s", clean_output[:200])

            # Handle Rails console responses with => prefix first (before other parsing)
            for line in clean_output.split("\n"):
                line = line.strip()
                # Handle Rails console output patterns like "=> nil", "=> true", etc.
                if line.startswith("=> "):
                    value_part = line[3:].strip()
                    if value_part == "nil":
                        logger.debug("Found Rails console nil response")
                        return None
                    elif value_part == "true":
                        logger.debug("Found Rails console true response")
                        return True
                    elif value_part == "false":
                        logger.debug("Found Rails console false response")
                        return False
                    elif value_part.isdigit():
                        logger.debug(
                            "Found Rails console integer response: %s", value_part
                        )
                        return int(value_part)
                    elif (value_part.startswith('"') and value_part.endswith('"')) or (
                        value_part.startswith("'") and value_part.endswith("'")
                    ):
                        logger.debug(
                            "Found Rails console string response: %s", value_part
                        )
                        return value_part[1:-1]  # Remove quotes
                    else:
                        logger.debug("Found Rails console response: %s", value_part)
                        return value_part

            # Try parsing as JSON first (arrays, objects)
            try:
                logger.debug("Attempting to parse as JSON")

                # For mixed console output, look for JSON patterns within the text
                json_patterns = [
                    r"\{[^{}]*\}",  # Simple objects like {"error":["..."]}
                    r"\[[^\[\]]*\]",  # Simple arrays
                ]

                for pattern in json_patterns:
                    import re

                    matches = re.findall(pattern, clean_output)
                    for match in matches:
                        try:
                            parsed = json.loads(match)
                            logger.debug(
                                "Successfully parsed embedded JSON: %s", match[:100]
                            )
                            return parsed
                        except json.JSONDecodeError:
                            continue

                # Try parsing the entire clean output as JSON
                parsed_data = json.loads(clean_output)
                logger.debug("Successfully parsed as JSON")
                return parsed_data

            except json.JSONDecodeError as e:
                logger.debug("Could not parse clean output as JSON: %s", e)
                logger.debug("Failed to parse extracted JSON: %s", e)

            # Look for JSON content in individual lines
            json_lines = []
            for line in clean_output.split("\n"):
                line = line.strip()
                # Skip Ruby script commands
                if any(
                    cmd in line
                    for cmd in [
                        "eval(<<",
                        "SCRIPT_END",
                        "puts(",
                        "rescue =>",
                        "begin",
                        "end",
                    ]
                ):
                    continue

                # Look for lines that start with [ or { (JSON indicators)
                if line.startswith("[") or line.startswith("{"):
                    json_lines.append(line)
                elif json_lines and (
                    line.endswith("]")
                    or line.endswith("}")
                    or "," in line
                    or '"' in line
                ):
                    # Continue collecting lines that seem to be part of JSON
                    json_lines.append(line)

            if json_lines:
                json_text = "".join(json_lines)
                try:
                    data = json.loads(json_text)
                    logger.debug("Successfully parsed JSON from extracted lines")
                    return data
                except json.JSONDecodeError as e:
                    logger.debug("Failed to parse extracted JSON: %s", str(e))

            # Look for simple scalar values in individual lines
            for line in clean_output.split("\n"):
                line = line.strip()
                # Skip lines that look like commands or prompts
                if (
                    line.startswith("puts")
                    or line.startswith("p ")
                    or line.startswith("pp ")
                    or ">" in line
                ):
                    continue

                # Extract value from Rails console format like "=> value"
                if line.startswith("=> "):
                    value_part = line[3:].strip()

                    # Handle nil
                    if value_part == "nil":
                        logger.debug("Found scalar nil value from Rails output")
                        return None
                    # Handle booleans
                    elif value_part in ["true", "false"]:
                        logger.debug(
                            "Found scalar boolean value from Rails output: %s",
                            value_part,
                        )
                        return value_part == "true"
                    # Handle numbers
                    elif value_part.isdigit():
                        logger.debug(
                            "Found scalar integer value from Rails output: %s",
                            value_part,
                        )
                        return int(value_part)
                    # Handle quoted strings
                    elif (value_part.startswith('"') and value_part.endswith('"')) or (
                        value_part.startswith("'") and value_part.endswith("'")
                    ):
                        logger.debug(
                            "Found scalar string value from Rails output: %s",
                            value_part,
                        )
                        return value_part[1:-1]  # Remove quotes
                    # For other values, return as string
                    elif value_part:
                        logger.debug(
                            "Found scalar value from Rails output: %s", value_part
                        )
                        return value_part

                # Check if this line is just a number (fallback)
                if line.isdigit():
                    logger.debug("Found scalar integer value: %s", line)
                    return int(line)
                # Check if this line is a boolean (fallback)
                if line in ["true", "false"]:
                    logger.debug("Found scalar boolean value: %s", line)
                    return line == "true"
                # Check if this line is nil (fallback)
                if line == "nil":
                    logger.debug("Found scalar nil value: %s", line)
                    return None
                # Check if this line is a quoted string (fallback)
                if (line.startswith('"') and line.endswith('"')) or (
                    line.startswith("'") and line.endswith("'")
                ):
                    logger.debug("Found scalar string value: %s", line)
                    return line[1:-1]  # Remove quotes

            # Look for simple scalar values in the entire clean output
            if clean_output.isdigit():
                logger.debug("Found scalar integer value: %s", clean_output)
                return int(clean_output)
            if clean_output in ["true", "false"]:
                logger.debug("Found scalar boolean value: %s", clean_output)
                return clean_output == "true"
            if clean_output == "nil":
                logger.debug("Found scalar nil value: %s", clean_output)
                return None
            if (clean_output.startswith('"') and clean_output.endswith('"')) or (
                clean_output.startswith("'") and clean_output.endswith("'")
            ):
                logger.debug("Found scalar string value: %s", clean_output)
                return clean_output[1:-1]  # Remove quotes

            # Final fallback - instead of returning unparsed strings, log error and return None
            logger.error("Could not parse Rails console output as JSON or scalar value")
            logger.error("Full clean output: %s", clean_output[:500])

            # Raise an exception instead of returning unparsed strings to force proper error handling
            raise JsonParseError(
                f"Failed to parse Rails console output: {clean_output[:100]}..."
            )

        except JsonParseError:
            # Re-raise JsonParseError
            raise
        except Exception as e:
            logger.error("Failed to process query result: %s", repr(e))
            logger.error("Raw output: %s", result_output[:200])
            # Raise an exception instead of returning None to ensure proper error handling
            raise QueryExecutionError(
                f"Failed to parse Rails console output: {e}"
            ) from e

    def execute_json_query(self, query: str, timeout: int | None = None) -> Any:
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
            if query.strip().endswith(")"):
                # If query ends with a closing parenthesis, add .as_json after it
                json_query = f"{query}.as_json"
            else:
                # Otherwise just append .as_json
                json_query = f"({query}).as_json"
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

            if result is None:
                msg = f"No {model} found with {id_or_conditions}"
                raise RecordNotFoundError(msg)

            return result

        except (QueryExecutionError, JsonParseError) as e:
            msg = f"Error finding record for {model}."
            raise QueryExecutionError(msg) from e

    def _retry_with_exponential_backoff(
        self,
        operation: Callable[[], Any],
        operation_name: str,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        jitter: bool = True,
    ) -> Any:
        """Execute an operation with exponential backoff retry logic.

        Args:
            operation: Function to execute
            operation_name: Name of operation for logging
            max_retries: Maximum number of retry attempts
            base_delay: Initial delay between retries in seconds
            max_delay: Maximum delay between retries in seconds
            backoff_factor: Factor to multiply delay by on each retry
            jitter: Whether to add random jitter to delays

        Returns:
            Result of the operation

        Raises:
            Exception: Last exception if all retries are exhausted
        """
        last_exception = None
        
        for attempt in range(max_retries + 1):
            try:
                result = operation()
                # Track successful batch operations (Zen's observability recommendation)
                if hasattr(self, 'metrics'):
                    self.metrics.increment_counter('batch_success_total')
                return result
            except (ConnectionError, QueryExecutionError) as e:
                last_exception = e
                
                # Check if this is a transient error worth retrying
                error_message = str(e).lower()
                transient_indicators = [
                    'timeout', 'connection', 'network', 'temporary', 
                    'retry', 'busy', 'overload', '503', '502', '504'
                ]
                
                is_transient = any(indicator in error_message for indicator in transient_indicators)
                
                if not is_transient or attempt >= max_retries:
                    # Track failed batch operations (Zen's observability recommendation)
                    if hasattr(self, 'metrics'):
                        self.metrics.increment_counter('batch_failure_total')
                    # Don't retry for non-transient errors or if out of retries
                    raise e
                
                # Track retry attempts (Zen's observability recommendation)
                if hasattr(self, 'metrics'):
                    self.metrics.increment_counter('batch_retry_total')
                
                # Calculate delay with exponential backoff
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                
                # Add jitter to prevent thundering herd
                if jitter:
                    delay = delay * (0.5 + random.random() * 0.5)
                
                self.logger.warning(
                    "%s failed (attempt %d/%d): %s. Retrying in %.2f seconds...",
                    operation_name, attempt + 1, max_retries + 1, e, delay
                )
                
                time.sleep(delay)
            
            except (RecordNotFoundError, JsonParseError) as e:
                # These are typically permanent errors - don't retry
                self.logger.debug(
                    "%s failed with non-transient error: %s", 
                    operation_name, e
                )
                raise e
        
        # This should never be reached, but just in case
        raise last_exception or Exception(f"{operation_name} failed after {max_retries} retries")

    @rate_limited()
    def batch_find_records(
        self,
        model: str,
        ids: list[int | str],
        batch_size: int | None = None,
    ) -> dict[int | str, dict[str, Any]]:
        """Find multiple records by IDs in batches.

        Args:
            model: Model name (e.g., "User", "Project")
            ids: List of IDs to find
            batch_size: Size of each batch (defaults to configured batch_size)

        Returns:
            Dictionary mapping ID to record data (missing IDs are omitted)

        Raises:
            QueryExecutionError: If query fails
        """
        if not ids:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion
        effective_batch_size = batch_size or getattr(self, 'batch_size', 100)
        effective_batch_size = self._validate_batch_size(effective_batch_size)
        
        results = {}
        
        # Process IDs in batches
        for i in range(0, len(ids), effective_batch_size):
            batch_ids = ids[i:i + effective_batch_size]
            
            def batch_operation():
                # Use safe query builder with ActiveRecord parameterization
                query = self._build_safe_batch_query(model, 'id', batch_ids)
                return self.execute_json_query(query)
            
            try:
                # Execute batch operation with retry logic
                batch_results = self._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch {model} records {batch_ids[:3]}{'...' if len(batch_ids) > 3 else ''}"
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
                        if isinstance(record, dict) and 'id' in record:
                            record_id_str = str(record['id'])
                            if record_id_str in batch_id_set:
                                original_id = original_id_map[record_id_str]
                                results[original_id] = record

            except Exception as e:
                self.logger.warning(
                    "Failed to fetch batch of %s records (IDs %s) after retries: %s",
                    model,
                    batch_ids,
                    e
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for batch_id in batch_ids:
                    self.logger.debug(
                        "Failed to fetch %s record ID %s: %s", 
                        model, batch_id, e
                    )
                continue

        return results

    def create_record(self, model: str, attributes: dict[str, Any]) -> dict[str, Any]:
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
        def format_value(v):
            if isinstance(v, bool):
                return "true" if v else "false"
            elif isinstance(v, str):
                return f"'{v}'"
            else:
                return str(v)

        attributes_str = ", ".join(
            [f"'{k}' => {format_value(v)}" for k, v in attributes.items()]
        )
        command = f"record = {model}.new({{{attributes_str}}}); record.save ? record.as_json : {{'error' => record.errors.full_messages}}"

        try:
            # Try execute_query_to_json_file first for better output handling
            result = self.execute_query_to_json_file(command)

            # Check if we got a valid dictionary
            if isinstance(result, dict):
                return result

            # If result is None, empty, or not a dict, try the fallback method
            if result is None or not isinstance(result, dict):
                logger.debug(
                    f"First method returned invalid result ({type(result)}), trying fallback"
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
                    f"Could not parse JSON response from {model} creation, but command executed. Attempting to find created record."
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
                            logger.info(f"Successfully found created {model} record")
                            return found_record
                except Exception as e:
                    logger.debug(f"Could not find created record: {e}")

                # If all else fails, create a minimal response
                logger.warning(f"Creating minimal response for {model} creation")
                return {
                    "id": None,
                    "created": True,
                    "model": model,
                    "attributes": attributes,
                }

            return result

        except RubyError as e:
            msg = f"Failed to create {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            msg = f"Error creating {model}."
            raise QueryExecutionError(msg) from e

    def update_record(
        self, model: str, id: int, attributes: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a record with given attributes.

        Args:
            model: Model name (e.g., "User", "Project")
            id: Record ID
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
        record = {model}.find_by(id: {id})
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
            if not isinstance(result, dict):
                msg = f"Failed to update {model}: Invalid response from OpenProject (type={type(result)}, value={result})"
                raise QueryExecutionError(msg)
            return result
        except RubyError as e:
            if "Record not found" in str(e):
                msg = f"{model} with ID {id} not found"
                raise RecordNotFoundError(msg) from e
            msg = f"Failed to update {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            msg = f"Error updating {model}."
            raise QueryExecutionError(msg) from e

    def delete_record(self, model: str, id: int) -> None:
        """Delete a record.

        Args:
            model: Model name (e.g., "User", "Project")
            id: Record ID

        Raises:
            RecordNotFoundError: If record doesn't exist
            QueryExecutionError: If deletion fails

        """
        command = f"""
        record = {model}.find_by(id: {id})
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
                msg = f"{model} with ID {id} not found"
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

        # Add to_json to get the result as JSON
        query += ".as_json"

        try:
            # Use the JSON-specific method
            result = self.execute_json_query(query)
            # Make sure we always return a list, even for empty results
            if result is None:
                return []
            return result
        except Exception as e:
            msg = f"Error finding records for {model}."
            raise QueryExecutionError(msg) from e

    def execute_transaction(self, commands: list[str]) -> Any:
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
            and current_time - self._users_cache_time < 300
        )

        if cache_valid:
            logger.debug("Using cached users data (%d users)", len(self._users_cache))
            return self._users_cache

        try:
            # Use pure file-based approach - write to file and read directly from filesystem
            file_path = self._generate_unique_temp_filename("users")

            # Execute command to write JSON to file - use a simple command that returns minimal output
            # Split into Python variable interpolation (f-string) and Ruby script (raw string)
            file_path_interpolated = f"'{file_path}'"
            write_query = f'users = User.all.as_json; File.write({file_path_interpolated}, JSON.pretty_generate(users)); puts "Users data written to {file_path} (#{{users.count}} users)"; nil'

            # Execute the write command with extended timeout for large user sets
            self.rails_client.execute(write_query, timeout=60, suppress_output=True)
            logger.debug("Successfully executed users write command")

            # Read the JSON directly from the Docker container file system via SSH
            try:
                # Use SSH to read the file from the Docker container
                ssh_command = f"docker exec {self.container_name} cat {file_path}"
                stdout, stderr, returncode = self.ssh_client.execute_command(
                    ssh_command
                )

                if returncode != 0:
                    logger.error(
                        "Failed to read file from container, stderr: %s", stderr
                    )
                    raise QueryExecutionError(
                        f"SSH command failed with code {returncode}: {stderr}"
                    )

                file_content = stdout.strip()
                logger.debug(
                    "Successfully read users file from container, content length: %d",
                    len(file_content),
                )

                # Parse the JSON content
                json_data = json.loads(file_content)
                logger.info(
                    "Successfully loaded %d users from container file",
                    len(json_data) if isinstance(json_data, list) else 0,
                )
            except (json.JSONDecodeError, Exception) as e:
                logger.error(
                    "Failed to read users from container file %s: %s", file_path, e
                )
                raise QueryExecutionError(
                    f"Failed to read users from container file: {e}"
                )

            # Validate that we got a list
            if not isinstance(json_data, list):
                logger.error(
                    "Expected list of users, got %s: %s",
                    type(json_data),
                    str(json_data)[:200],
                )
                raise QueryExecutionError(
                    f"Invalid users data format - expected list, got {type(json_data)}"
                )

            # Update cache
            self._users_cache = json_data or []
            self._users_cache_time = current_time

            logger.info("Retrieved %d users from OpenProject", len(self._users_cache))
            return self._users_cache

        except Exception as e:
            msg = "Failed to retrieve users."
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
            raise RecordNotFoundError(msg)

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
                raise RecordNotFoundError(msg)

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
            raise QueryExecutionError(msg)

        except RecordNotFoundError:
            raise  # Re-raise RecordNotFoundError
        except Exception as e:
            msg = "Error getting custom field ID."
            raise QueryExecutionError(msg) from e

    def get_custom_fields(self, force_refresh: bool = False) -> list[dict[str, Any]]:
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
            # Use pure file-based approach - write to file and read directly from filesystem
            file_path = self._generate_unique_temp_filename("custom_fields")

            # Execute command to write JSON to file - use a simple command that returns minimal output
            # Split into Python variable interpolation (f-string) and Ruby script (raw string)
            file_path_interpolated = f"'{file_path}'"
            write_query = f'custom_fields = CustomField.all.as_json; File.write({file_path_interpolated}, JSON.pretty_generate(custom_fields)); puts "Custom fields data written to {file_path} (#{{custom_fields.count}} fields)"; nil'

            # Execute the write command - fail immediately if Rails console fails
            self.rails_client.execute(write_query, suppress_output=True)
            logger.debug("Successfully executed custom fields write command")

            # Read the JSON directly from the Docker container file system via SSH
            try:
                # Use SSH to read the file from the Docker container
                ssh_command = f"docker exec {self.container_name} cat {file_path}"
                stdout, stderr, returncode = self.ssh_client.execute_command(
                    ssh_command
                )

                if returncode != 0:
                    logger.error(
                        "Failed to read file from container, stderr: %s", stderr
                    )
                    raise QueryExecutionError(
                        f"SSH command failed with code {returncode}: {stderr}"
                    )

                file_content = stdout.strip()
                logger.debug(
                    "Successfully read custom fields file from container, content length: %d",
                    len(file_content),
                )

                # Parse the JSON content
                custom_fields = json.loads(file_content)
                logger.info(
                    "Successfully loaded %d custom fields from container file",
                    len(custom_fields) if isinstance(custom_fields, list) else 0,
                )

                # Update cache
                self._custom_fields_cache = custom_fields or []
                self._custom_fields_cache_time = current_time

                return custom_fields

            except (json.JSONDecodeError, Exception) as e:
                logger.error("Failed to read custom fields from container file: %s", e)
                raise QueryExecutionError(
                    f"Failed to read/parse custom fields from file: {e}"
                )

        except Exception as e:
            msg = "Failed to get custom fields."
            raise QueryExecutionError(msg) from e

    def get_statuses(self) -> list[dict[str, Any]]:
        """Get all statuses from OpenProject.

        Returns:
            List of status objects

        Raises:
            QueryExecutionError: If query fails

        """
        try:
            return self.execute_json_query("Status.all") or []
        except Exception as e:
            msg = "Failed to get statuses."
            raise QueryExecutionError(msg) from e

    def get_work_package_types(self) -> list[dict[str, Any]]:
        """Get all work package types from OpenProject.

        Returns:
            List of work package type objects

        Raises:
            QueryExecutionError: If query fails

        """
        try:
            return self.execute_json_query("Type.all") or []
        except Exception as e:
            msg = "Failed to get work package types."
            raise QueryExecutionError(msg) from e

    def get_projects(self) -> list[dict[str, Any]]:
        """Get all projects from OpenProject using direct tmux approach.

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
            write_query = f'projects = Project.all.select(:id, :name, :identifier, :description, :status_code).as_json; File.write({file_path_interpolated}, JSON.pretty_generate(projects)); puts "Projects data written to {file_path} (#{{projects.count}} projects)"; nil'

            # Execute the write command - fail immediately if Rails console fails
            self.rails_client.execute(write_query, suppress_output=True)
            logger.debug("Successfully executed projects write command")

            # Read the JSON directly from the Docker container file system via SSH
            try:
                # Use SSH to read the file from the Docker container
                ssh_command = f"docker exec {self.container_name} cat {file_path}"
                stdout, stderr, returncode = self.ssh_client.execute_command(
                    ssh_command
                )

                if returncode != 0:
                    logger.error(
                        "Failed to read file from container, stderr: %s", stderr
                    )
                    raise QueryExecutionError(
                        f"SSH command failed with code {returncode}: {stderr}"
                    )

                file_content = stdout.strip()
                logger.debug(
                    "Successfully read projects file from container, content length: %d",
                    len(file_content),
                )

                # Parse the JSON content
                result = json.loads(file_content)
                logger.info(
                    "Successfully loaded %d projects from container file",
                    len(result) if isinstance(result, list) else 0,
                )
            except (json.JSONDecodeError, Exception) as e:
                logger.error(
                    "Failed to read projects from container file %s: %s", file_path, e
                )
                raise QueryExecutionError(
                    f"Failed to read projects from container file: {e}"
                )

            # The execute_query_to_json_file method should return the parsed JSON
            if not isinstance(result, list):
                logger.error(
                    "Expected list of projects, got %s: %s",
                    type(result),
                    str(result)[:200],
                )
                raise QueryExecutionError(
                    f"Invalid projects data format - expected list, got {type(result)}"
                )

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
                            "identifier", f"project-{project.get('id')}"
                        ),  # Generate if missing
                        "description": project.get("description", ""),
                        "public": project.get("public", False),
                        "status": project.get(
                            "status_code", 1
                        ),  # Use status_code from DB
                    }
                    validated_projects.append(validated_project)
                    logger.debug("Validated project: %s", validated_project)
                else:
                    logger.debug(
                        "Skipping invalid project data (missing ID): %s", project
                    )

            logger.info(
                "Retrieved %d projects using file-based method", len(validated_projects)
            )
            return validated_projects

        except Exception as e:
            logger.error("Failed to get projects using file-based method: %s", e)
            raise QueryExecutionError(f"Failed to retrieve projects: {e}") from e

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
            if project is None:
                msg = f"Project with identifier '{identifier}' not found"
                raise RecordNotFoundError(msg)
            return project
        except RecordNotFoundError:
            raise  # Re-raise RecordNotFoundError
        except Exception as e:
            msg = "Failed to get project."
            raise QueryExecutionError(msg) from e

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
        script = """
        activities = TimeEntryActivity.active.map do |activity|
          {
            id: activity.id,
            name: activity.name,
            position: activity.position,
            is_default: activity.is_default,
            active: activity.active
          }
        end
        activities
        """
        
        try:
            result = self.execute_json_query(script)
            return result if isinstance(result, list) else []
        except Exception as e:
            msg = "Failed to retrieve time entry activities."
            raise QueryExecutionError(msg) from e

    def create_time_entry(self, time_entry_data: dict[str, Any]) -> dict[str, Any] | None:
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
            import re
            match = re.search(r'/work_packages/(\d+)', work_package_href)
            if match:
                work_package_id = int(match.group(1))
        
        # Get user ID from href
        user_href = embedded.get("user", {}).get("href", "")
        user_id = None
        if user_href:
            # Extract ID from href like "/api/v3/users/456"
            match = re.search(r'/users/(\d+)', user_href)
            if match:
                user_id = int(match.group(1))
        
        # Get activity ID from href
        activity_href = embedded.get("activity", {}).get("href", "")
        activity_id = None
        if activity_href:
            # Extract ID from href like "/api/v3/time_entries/activities/789"
            match = re.search(r'/activities/(\d+)', activity_href)
            if match:
                activity_id = int(match.group(1))
        
        if not all([work_package_id, user_id, activity_id]):
            raise ValueError(
                f"Missing required IDs: work_package_id={work_package_id}, "
                f"user_id={user_id}, activity_id={activity_id}"
            )
        
        # Prepare the script with proper Ruby syntax
        script = f"""
        begin
          time_entry = TimeEntry.new(
            work_package_id: {work_package_id},
            user_id: {user_id},
            activity_id: {activity_id},
            hours: {time_entry_data.get('hours', 0)},
            spent_on: Date.parse('{time_entry_data.get('spentOn', '')}'),
            comments: {repr(time_entry_data.get('comment', {}).get('raw', ''))}
          )
          
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
            result = self.execute_json_query(script)
            
            if isinstance(result, dict):
                if result.get("error"):
                    logger.warning(f"Time entry creation failed: {result}")
                    return None
                return result
            
            logger.warning(f"Unexpected time entry creation result: {result}")
            return None
            
        except Exception as e:
            msg = f"Failed to create time entry: {e}"
            raise QueryExecutionError(msg) from e

    def get_time_entries(
        self, 
        work_package_id: int | None = None,
        user_id: int | None = None,
        limit: int = 100
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
        
        script = f"""
        time_entries = TimeEntry{where_clause}.limit({limit}).includes(:work_package, :user, :activity)
        
        time_entries.map do |entry|
          {{
            id: entry.id,
            work_package_id: entry.work_package_id,
            work_package_subject: entry.work_package&.subject,
            user_id: entry.user_id,
            user_name: entry.user&.name,
            activity_id: entry.activity_id,
            activity_name: entry.activity&.name,
            hours: entry.hours.to_f,
            spent_on: entry.spent_on.to_s,
            comments: entry.comments,
            created_at: entry.created_at.to_s,
            updated_at: entry.updated_at.to_s
          }}
        end
        """
        
        try:
            result = self.execute_json_query(script)
            return result if isinstance(result, list) else []
        except Exception as e:
            msg = "Failed to retrieve time entries."
            raise QueryExecutionError(msg) from e

    def batch_create_time_entries(self, time_entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Create multiple time entries in a single operation.
        
        Args:
            time_entries: List of time entry data dictionaries
            
        Returns:
            Dictionary with creation results and statistics
            
        Raises:
            QueryExecutionError: If the batch operation fails
        """
        if not time_entries:
            return {"created": 0, "failed": 0, "results": []}
        
        # Build the batch creation script
        entries_data = []
        for i, entry_data in enumerate(time_entries):
            # Extract required data similar to create_time_entry
            embedded = entry_data.get("_embedded", {})
            
            # Get work package ID
            work_package_href = embedded.get("workPackage", {}).get("href", "")
            work_package_id = None
            if work_package_href:
                import re
                match = re.search(r'/work_packages/(\d+)', work_package_href)
                if match:
                    work_package_id = int(match.group(1))
            
            # Get user ID
            user_href = embedded.get("user", {}).get("href", "")
            user_id = None
            if user_href:
                match = re.search(r'/users/(\d+)', user_href)
                if match:
                    user_id = int(match.group(1))
            
            # Get activity ID
            activity_href = embedded.get("activity", {}).get("href", "")
            activity_id = None
            if activity_href:
                match = re.search(r'/activities/(\d+)', activity_href)
                if match:
                    activity_id = int(match.group(1))
            
            if all([work_package_id, user_id, activity_id]):
                entries_data.append({
                    "index": i,
                    "work_package_id": work_package_id,
                    "user_id": user_id,
                    "activity_id": activity_id,
                    "hours": entry_data.get('hours', 0),
                    "spent_on": entry_data.get('spentOn', ''),
                    "comments": entry_data.get('comment', {}).get('raw', '')
                })
        
        if not entries_data:
            return {"created": 0, "failed": len(time_entries), "results": []}
        
        # Generate Ruby script for batch creation
        entries_ruby = []
        for entry in entries_data:
            entries_ruby.append(f"""
            {{
              index: {entry['index']},
              work_package_id: {entry['work_package_id']},
              user_id: {entry['user_id']},
              activity_id: {entry['activity_id']},
              hours: {entry['hours']},
              spent_on: '{entry['spent_on']}',
              comments: {repr(entry['comments'])}
            }}""")
        
        script = f"""
        entries_data = [{', '.join(entries_ruby)}]
        results = []
        created_count = 0
        failed_count = 0
        
        entries_data.each do |entry_data|
          begin
            time_entry = TimeEntry.new(
              work_package_id: entry_data[:work_package_id],
              user_id: entry_data[:user_id],
              activity_id: entry_data[:activity_id],
              hours: entry_data[:hours],
              spent_on: Date.parse(entry_data[:spent_on]),
              comments: entry_data[:comments]
            )
            
            if time_entry.save
              created_count += 1
              results << {{
                index: entry_data[:index],
                success: true,
                id: time_entry.id,
                work_package_id: time_entry.work_package_id,
                hours: time_entry.hours.to_f
              }}
            else
              failed_count += 1
              results << {{
                index: entry_data[:index],
                success: false,
                errors: time_entry.errors.full_messages
              }}
            end
          rescue => e
            failed_count += 1
            results << {{
              index: entry_data[:index],
              success: false,
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
            return result if isinstance(result, dict) else {"created": 0, "failed": len(time_entries), "results": []}
        except Exception as e:
            msg = f"Failed to batch create time entries: {e}"
            raise QueryExecutionError(msg) from e

    # ===== ENHANCED PERFORMANCE FEATURES =====
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get comprehensive performance statistics."""
        return self.performance_optimizer.get_comprehensive_stats()

    # ===== BATCH OPERATIONS =====
    
    def batch_create_work_packages(self, work_packages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create multiple work packages in batches for optimal performance."""
        if not work_packages:
            return {"created": 0, "failed": 0, "results": []}
            
        return self.performance_optimizer.batch_processor.process_batches(
            work_packages,
            self._create_work_packages_batch
        )
    
    def _create_work_packages_batch(self, work_packages: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
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
            return result if isinstance(result, dict) else {"created": 0, "failed": len(work_packages), "results": []}
        except Exception as e:
            msg = f"Failed to batch create work packages: {e}"
            raise QueryExecutionError(msg) from e
    
    @cached(ttl=1800)  # Cache for 30 minutes
    def get_project_enhanced(self, project_id: int) -> Dict[str, Any]:
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

    def batch_get_users_by_ids(self, user_ids: List[int]) -> Dict[int, dict]:
        """Retrieve multiple users in batches."""
        if not user_ids:
            return {}
            
        # Get all users and filter to requested IDs
        all_users = self.get_all_users()
        return {
            user['id']: user 
            for user in all_users 
            if user['id'] in user_ids
        }

    @rate_limited()
    def stream_work_packages_for_project(self, project_id: int, batch_size: int = None) -> Iterator[Dict[str, Any]]:
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
                for wp in results:
                    yield wp
        except Exception as e:
            logger.error(f"Failed to stream work packages for project {project_id}: {e}")

    def batch_update_work_packages(self, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
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
              wp.send("#{key}=", value) if wp.respond_to?("#{key}=")
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

    @rate_limited()
    def batch_get_users_by_emails(
        self,
        emails: list[str],
        batch_size: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple users by email addresses in batches.

        Args:
            emails: List of email addresses to find
            batch_size: Size of each batch (defaults to configured batch_size)

        Returns:
            Dictionary mapping email to user data (missing emails are omitted)

        Raises:
            QueryExecutionError: If query fails
        """
        if not emails:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion
        effective_batch_size = batch_size or getattr(self, 'batch_size', 100)
        effective_batch_size = self._validate_batch_size(effective_batch_size)
        
        results = {}
        
        # Process emails in batches
        for i in range(0, len(emails), effective_batch_size):
            batch_emails = emails[i:i + effective_batch_size]
            
            def batch_operation():
                # Use safe query builder with ActiveRecord parameterization
                query = self._build_safe_batch_query('User', 'mail', batch_emails)
                return self.execute_json_query(query)
            
            try:
                # Execute batch operation with retry logic
                batch_results = self._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch users by email {batch_emails[:2]}{'...' if len(batch_emails) > 2 else ''}"
                )
                
                if batch_results:
                    # Ensure we have a list
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]
                    
                    # Map results by email
                    for record in batch_results:
                        if isinstance(record, dict) and 'mail' in record:
                            email = record['mail']
                            if email in batch_emails:
                                results[email] = record

            except Exception as e:
                self.logger.warning(
                    "Failed to fetch batch of user emails %s after retries: %s",
                    batch_emails,
                    e
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for email in batch_emails:
                    self.logger.debug(
                        "Failed to fetch user by email %s: %s", 
                        email, e
                    )
                continue

        return results

    @rate_limited()
    def batch_get_projects_by_identifiers(
        self,
        identifiers: list[str],
        batch_size: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple projects by identifiers in batches.

        Args:
            identifiers: List of project identifiers to find
            batch_size: Size of each batch (defaults to configured batch_size)

        Returns:
            Dictionary mapping identifier to project data (missing identifiers are omitted)

        Raises:
            QueryExecutionError: If query fails
        """
        if not identifiers:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion
        effective_batch_size = batch_size or getattr(self, 'batch_size', 100)
        effective_batch_size = self._validate_batch_size(effective_batch_size)
        
        results = {}
        
        # Process identifiers in batches
        for i in range(0, len(identifiers), effective_batch_size):
            batch_identifiers = identifiers[i:i + effective_batch_size]
            
            def batch_operation():
                # Use safe query builder with ActiveRecord parameterization  
                query = self._build_safe_batch_query('Project', 'identifier', batch_identifiers)
                return self.execute_json_query(query)
            
            try:
                # Execute batch operation with retry logic
                batch_results = self._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch projects by identifier {batch_identifiers[:2]}{'...' if len(batch_identifiers) > 2 else ''}"
                )
                
                if batch_results:
                    # Ensure we have a list
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]
                    
                    # Map results by identifier
                    for record in batch_results:
                        if isinstance(record, dict) and 'identifier' in record:
                            identifier = record['identifier']
                            if identifier in batch_identifiers:
                                results[identifier] = record

            except Exception as e:
                self.logger.warning(
                    "Failed to fetch batch of project identifiers %s after retries: %s",
                    batch_identifiers,
                    e
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for identifier in batch_identifiers:
                    self.logger.debug(
                        "Failed to fetch project by identifier %s: %s", 
                        identifier, e
                    )
                continue

        return results

    @rate_limited()
    def batch_get_custom_fields_by_names(
        self,
        names: list[str],
        batch_size: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple custom fields by names in batches.

        Args:
            names: List of custom field names to find
            batch_size: Size of each batch (defaults to configured batch_size)

        Returns:
            Dictionary mapping name to custom field data (missing names are omitted)

        Raises:
            QueryExecutionError: If query fails
        """
        if not names:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion
        effective_batch_size = batch_size or getattr(self, 'batch_size', 100)
        effective_batch_size = self._validate_batch_size(effective_batch_size)
        
        results = {}
        
        # Process names in batches
        for i in range(0, len(names), effective_batch_size):
            batch_names = names[i:i + effective_batch_size]
            
            def batch_operation():
                # Use safe query builder with ActiveRecord parameterization
                query = self._build_safe_batch_query('CustomField', 'name', batch_names)
                return self.execute_json_query(query)
            
            try:
                # Execute batch operation with retry logic
                batch_results = self._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch custom fields by name {batch_names[:2]}{'...' if len(batch_names) > 2 else ''}"
                )
                
                if batch_results:
                    # Ensure we have a list
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]
                    
                    # Map results by name
                    for record in batch_results:
                        if isinstance(record, dict) and 'name' in record:
                            name = record['name']
                            if name in batch_names:
                                results[name] = record

            except Exception as e:
                self.logger.warning(
                    "Failed to fetch batch of custom field names %s after retries: %s",
                    batch_names,
                    e
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for name in batch_names:
                    self.logger.debug(
                        "Failed to fetch custom field by name %s: %s", 
                        name, e
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
            raise ValueError(f"batch_size must be a positive integer, got: {batch_size}")
        
        # Enforce maximum batch size to prevent memory exhaustion
        MAX_BATCH_SIZE = 1000
        if batch_size > MAX_BATCH_SIZE:
            self.logger.warning(
                "batch_size %d exceeds maximum %d, clamping to maximum",
                batch_size, MAX_BATCH_SIZE
            )
            return MAX_BATCH_SIZE
        
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
        ALLOWED_MODELS = {
            'User', 'Project', 'WorkPackage', 'CustomField', 
            'Status', 'Type', 'Priority', 'Category', 'Version',
            'TimeEntry', 'Attachment', 'Repository', 'News',
            'Wiki', 'WikiPage', 'Forum', 'Message', 'Board'
        }
        
        if model not in ALLOWED_MODELS:
            raise ValueError(f"Model '{model}' not in allowed list: {sorted(ALLOWED_MODELS)}")
        
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
        import re
        
        # Validate model name first
        safe_model = self._validate_model_name(model)
        
        # Validate field name to prevent injection (Zen's critical recommendation)
        if not re.match(r"^[a-zA-Z_]+$", field):
            raise ValueError(f"Illegal field name '{field}' - only letters and underscores allowed")
        
        # Use ActiveRecord's built-in parameterization instead of string building
        # This approach delegates sanitization to Rails rather than DIY
        values_json = json.dumps(values)
        
        # Add payload byte cap to prevent memory exhaustion (Zen's recommendation)
        payload_bytes = len(values_json.encode("utf-8"))
        MAX_PAYLOAD_BYTES = 256_000  # 256 KB limit
        if payload_bytes > MAX_PAYLOAD_BYTES:
            raise ValueError(f"Batch payload {payload_bytes} bytes exceeds {MAX_PAYLOAD_BYTES} limit")
        
        query = f"{safe_model}.where({field}: {values_json}).map(&:as_json)"
        
        return query
