"""OpenProject client for interacting with OpenProject instances via SSH and Rails console."""

import json
import os
import random
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from src import config
from src.display import configure_logging
from src.infrastructure.exceptions import (
    ClientConnectionError,
    JsonParseError,
    QueryExecutionError,
    RecordNotFoundError,
)
from src.infrastructure.openproject.docker_client import DockerClient
from src.infrastructure.openproject.rails_console_client import (
    RailsConsoleClient,
)
from src.infrastructure.openproject.ssh_client import SSHClient
from src.utils.config_validation import ConfigurationValidationError, SecurityValidator
from src.utils.file_manager import FileManager
from src.utils.idempotency_decorators import batch_idempotent
from src.utils.performance_optimizer import PerformanceOptimizer
from src.utils.rate_limiter import create_openproject_rate_limiter

try:
    # Prefer shared logger configured at startup
    from src.config import logger
except Exception:
    # Fallback to local configuration if config logger is unavailable
    logger = configure_logging("INFO", None)


# Module-level constants
BATCH_SIZE_DEFAULT = 50
SAFE_OFFSET_LIMIT = 5000
BATCH_LABEL_SAMPLE = 3


# Pre-compiled regex patterns for control character sanitization (hot path)
_RE_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_OSC_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_RE_OTHER_ESCAPE = re.compile(r"\x1b[^[]]")
_RE_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Allowlist of Rails model names that may be accessed via generic record operations.
# Prevents arbitrary model access through the Rails console.
_ALLOWED_MODELS = frozenset(
    {
        "Attachment",
        "Color",
        "CustomField",
        "CustomOption",
        "CustomValue",
        "EnabledModule",
        "Enumeration",
        "Group",
        "IssuePriority",
        "Journal",
        "Member",
        "MemberRole",
        "Principal",
        "Project",
        "ProjectCustomField",
        "Query",
        "Relation",
        "Role",
        "Status",
        "TimeEntry",
        "Type",
        "User",
        "Version",
        "Watcher",
        "Wiki",
        "WikiPage",
        "WorkPackage",
        "WorkPackageCustomField",
    },
)

# Also validate model names match a safe pattern (PascalCase identifier)
_RE_MODEL_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def _validate_model_name(model: str) -> None:
    """Validate that a model name is safe to use in Rails console queries.

    Raises:
        ValueError: If the model name is not in the allowlist or has invalid format

    """
    if not _RE_MODEL_NAME.match(model):
        msg = f"Invalid model name format: {model!r}"
        raise ValueError(msg)
    if model not in _ALLOWED_MODELS:
        msg = f"Model {model!r} is not in the allowed models list"
        raise ValueError(msg)


def escape_ruby_single_quoted(s: str) -> str:
    r"""Escape a string for safe inclusion in a Ruby single-quoted literal.

    In Ruby single-quoted strings, only two escape sequences are recognized:
    \\' (escaped quote) and \\\\ (escaped backslash). All other characters
    are treated literally.

    Also replaces newlines and carriage returns with their literal escape
    sequences to prevent tmux console line-break issues.
    """
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")


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
        # ``_custom_fields_cache`` and ``_custom_fields_cache_time`` moved into
        # ``OpenProjectCustomFieldService`` (see ``self.custom_fields`` below,
        # initialised after the dependent clients are wired up).

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

        # Composed services (Phase 2 of ADR-002 — splitting the god-class along
        # functional seams). Backward-compat: same-named methods on
        # OpenProjectClient delegate to the corresponding service.
        from src.infrastructure.openproject.openproject_admin_cleanup_service import OpenProjectAdminCleanupService
        from src.infrastructure.openproject.openproject_associations_service import OpenProjectAssociationsService
        from src.infrastructure.openproject.openproject_bulk_create_service import OpenProjectBulkCreateService
        from src.infrastructure.openproject.openproject_content_service import OpenProjectContentService
        from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService
        from src.infrastructure.openproject.openproject_file_transfer_service import OpenProjectFileTransferService
        from src.infrastructure.openproject.openproject_issue_priority_service import OpenProjectIssuePriorityService
        from src.infrastructure.openproject.openproject_membership_service import OpenProjectMembershipService
        from src.infrastructure.openproject.openproject_project_attribute_service import (
            OpenProjectProjectAttributeService,
        )
        from src.infrastructure.openproject.openproject_project_service import OpenProjectProjectService
        from src.infrastructure.openproject.openproject_project_setup_service import OpenProjectProjectSetupService
        from src.infrastructure.openproject.openproject_provenance_service import OpenProjectProvenanceService
        from src.infrastructure.openproject.openproject_rails_runner_service import OpenProjectRailsRunnerService
        from src.infrastructure.openproject.openproject_records_service import OpenProjectRecordsService
        from src.infrastructure.openproject.openproject_status_type_service import OpenProjectStatusTypeService
        from src.infrastructure.openproject.openproject_time_entry_service import OpenProjectTimeEntryService
        from src.infrastructure.openproject.openproject_user_service import OpenProjectUserService
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )
        from src.infrastructure.openproject.openproject_work_package_custom_field_service import (
            OpenProjectWorkPackageCustomFieldService,
        )
        from src.infrastructure.openproject.openproject_work_package_service import OpenProjectWorkPackageService

        self.custom_fields = OpenProjectCustomFieldService(self)
        self.provenance = OpenProjectProvenanceService(self)
        self.file_transfer = OpenProjectFileTransferService(self)
        self.rails_runner = OpenProjectRailsRunnerService(self)
        self.users = OpenProjectUserService(self)
        self.projects = OpenProjectProjectService(self)
        self.memberships = OpenProjectMembershipService(self)
        self.records = OpenProjectRecordsService(self)
        self.work_packages = OpenProjectWorkPackageService(self)
        self.associations = OpenProjectAssociationsService(self)
        self.time_entries = OpenProjectTimeEntryService(self)
        self.priorities = OpenProjectIssuePriorityService(self)
        self.status_types = OpenProjectStatusTypeService(self)
        self.wp_content = OpenProjectWorkPackageContentService(self)
        self.project_attributes = OpenProjectProjectAttributeService(self)
        self.project_setup = OpenProjectProjectSetupService(self)
        self.admin_cleanup = OpenProjectAdminCleanupService(self)
        self.wp_cf = OpenProjectWorkPackageCustomFieldService(self)
        self.content = OpenProjectContentService(self)
        self.bulk_create = OpenProjectBulkCreateService(self)

        logger.success(
            "OpenProjectClient initialized for host %s, container %s",
            self.ssh_host,
            self.container_name,
        )

    def ensure_reporting_project(self, identifier: str, name: str) -> int:
        """Thin delegator over ``self.project_setup.ensure_reporting_project``."""
        return self.project_setup.ensure_reporting_project(identifier, name)

    def _generate_unique_temp_filename(self, base_name: str) -> str:
        """Generate a temporary filename; stable for tests, unique in prod.

        Thin delegator over ``self.file_transfer.generate_unique_temp_filename``.
        """
        return self.file_transfer.generate_unique_temp_filename(base_name)

    def _create_script_file(self, script_content: str) -> Path:
        """Create a temporary file with the script content.

        Thin delegator over ``self.file_transfer.create_script_file``.
        """
        return self.file_transfer.create_script_file(script_content)

    def _transfer_rails_script(self, local_path: Path | str) -> Path:
        """Transfer a script to the Rails environment.

        Thin delegator over ``self.file_transfer.transfer_rails_script``.
        """
        return self.file_transfer.transfer_rails_script(local_path)

    def _cleanup_script_files(self, files_or_local: Any, remote_path: Path | None = None) -> None:
        """Clean up temporary files after execution.

        Thin delegator over ``self.file_transfer.cleanup_script_files``.
        """
        self.file_transfer.cleanup_script_files(files_or_local, remote_path)

    def execute(self, script_content: str) -> Any:
        """Execute a Ruby script directly.

        Returns the parsed JSON value (dict / list / scalar / None) when the
        console output is valid JSON, otherwise a ``{"result": ...}`` dict
        with the raw text. Return is intentionally ``Any`` because callers
        cannot rely on a dict shape.

        Thin delegator over ``self.rails_runner.execute``.
        """
        return self.rails_runner.execute(script_content)

    def execute_script_with_data(
        self,
        script_content: str,
        data: Any,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Execute a Ruby script in the Rails console with structured input data.

        Thin delegator over ``self.rails_runner.execute_script_with_data``.
        """
        return self.rails_runner.execute_script_with_data(script_content, data, timeout)

    def transfer_file_to_container(
        self,
        local_path: Path,
        container_path: Path,
    ) -> None:
        """Transfer a file from local to the OpenProject container.

        Thin delegator over ``self.file_transfer.transfer_file_to_container``.
        """
        self.file_transfer.transfer_file_to_container(local_path, container_path)

    def is_connected(self) -> bool:
        """Test if connected to OpenProject.

        Thin delegator over ``self.rails_runner.is_connected``.
        """
        return self.rails_runner.is_connected()

    def execute_query(self, query: str, timeout: int | None = None) -> str:
        """Execute a Rails query.

        Thin delegator over ``self.rails_runner.execute_query``.
        """
        return self.rails_runner.execute_query(query, timeout)

    def execute_query_to_json_file(self, query: str, timeout: int | None = None) -> dict[str, Any]:
        """Execute a Rails query and return parsed JSON result.

        Thin delegator over ``self.rails_runner.execute_query_to_json_file``.
        """
        return self.rails_runner.execute_query_to_json_file(query, timeout)

    def execute_large_query_to_json_file(
        self,
        query: str,
        container_file: str = "/tmp/j2o_query.json",
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Execute a Rails query by writing JSON to a container file, then read it back.

        Thin delegator over ``self.rails_runner.execute_large_query_to_json_file``.
        Use this for large result sets to avoid tmux/console truncation.
        """
        return self.rails_runner.execute_large_query_to_json_file(
            query,
            container_file,
            timeout,
        )

    def _check_console_output_for_errors(self, output: str, context: str) -> None:
        """Raise a QueryExecutionError if console output indicates a Ruby error.

        Thin delegator over ``self.rails_runner.check_console_output_for_errors``.
        """
        self.rails_runner.check_console_output_for_errors(output, context)

    def _assert_expected_console_notice(self, output: str, expected_prefix: str, context: str) -> None:
        """Treat any unexpected console response as error in strict file-write flows.

        Thin delegator over ``self.rails_runner.assert_expected_console_notice``.
        """
        self.rails_runner.assert_expected_console_notice(output, expected_prefix, context)

    # Removed rails runner helper; all scripts go through persistent tmux console

    def _execute_batched_query(
        self,
        model_name: str,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a query in batches to avoid any truncation issues.

        Thin delegator over ``self.rails_runner.execute_batched_query``.
        """
        return self.rails_runner.execute_batched_query(model_name, timeout)

    def _parse_rails_output(self, result_output: str) -> object:
        """Parse Rails console output to extract JSON or scalar values.

        Thin delegator over ``self.rails_runner.parse_rails_output``.
        """
        return self.rails_runner.parse_rails_output(result_output)

    def execute_json_query(self, query: str, timeout: int | None = None) -> object:
        """Execute a Rails query and return parsed JSON result.

        Thin delegator over ``self.rails_runner.execute_json_query``.
        """
        return self.rails_runner.execute_json_query(query, timeout)

    def count_records(self, model: str) -> int:
        """Count records for a given Rails model.

        Thin delegator over ``self.rails_runner.count_records``.
        """
        return self.rails_runner.count_records(model)

    # -------------------------------------------------------------
    # Work Package Custom Field helpers for fast-forward migrations
    # -------------------------------------------------------------
    def ensure_work_package_custom_field(self, name: str, field_format: str = "string") -> dict[str, Any]:
        """Ensure a WorkPackage custom field exists, create if missing.

        Thin delegator over ``self.custom_fields.ensure_work_package_custom_field``.
        """
        return self.custom_fields.ensure_work_package_custom_field(name, field_format)

    def ensure_custom_field(
        self,
        name: str,
        *,
        field_format: str = "string",
        cf_type: str = "WorkPackageCustomField",
        searchable: bool = False,
    ) -> dict[str, Any]:
        """Ensure a CustomField exists for the given type, create if missing.

        Thin delegator over ``self.custom_fields.ensure_custom_field``.
        """
        return self.custom_fields.ensure_custom_field(
            name,
            field_format=field_format,
            cf_type=cf_type,
            searchable=searchable,
        )

    def ensure_wp_custom_field_id(self, name: str, field_format: str = "text") -> int:
        """Ensure a WorkPackageCustomField exists, returning its ID.

        Thin delegator over ``self.custom_fields.ensure_wp_custom_field_id``;
        see :py:class:`~src.infrastructure.openproject.openproject_custom_field_service.OpenProjectCustomFieldService`
        for the full docstring.
        """
        return self.custom_fields.ensure_wp_custom_field_id(name, field_format)

    def enable_custom_field_for_projects(
        self,
        cf_id: int,
        project_ids: set[int],
        cf_name: str | None = None,
    ) -> None:
        """Enable a custom field for specific projects only.

        Thin delegator over ``self.custom_fields.enable_custom_field_for_projects``.
        """
        self.custom_fields.enable_custom_field_for_projects(cf_id, project_ids, cf_name=cf_name)

    def remove_custom_field(self, name: str, *, cf_type: str | None = None) -> dict[str, int]:
        """Remove CustomField records matching the provided name/type.

        Thin delegator over ``self.custom_fields.remove_custom_field``.
        """
        return self.custom_fields.remove_custom_field(name, cf_type=cf_type)

    def ensure_origin_custom_fields(self) -> dict[str, list[dict[str, Any]]]:
        """Ensure origin mapping CFs exist for WorkPackage / User / TimeEntry.

        Project custom fields are intentionally skipped on this OpenProject
        instance; project origin metadata is persisted via
        ``upsert_project_origin_attributes`` / ``upsert_project_attribute``
        instead. The returned dict still has a ``project`` key but it is
        always an empty list.

        Thin delegator over ``self.custom_fields.ensure_origin_custom_fields``.
        """
        return self.custom_fields.ensure_origin_custom_fields()

    # =========================================================================
    # J2O Provenance Registry
    # =========================================================================
    # The full implementation lives in
    # ``src.infrastructure.openproject.openproject_provenance_service.OpenProjectProvenanceService``
    # (Phase 2c of ADR-002). The methods below are thin delegators kept for
    # backward compatibility with existing call sites.
    # =========================================================================

    def ensure_j2o_migration_project(self) -> int:
        """Ensure the J2O Migration Provenance project exists.

        Thin delegator over ``self.provenance.ensure_migration_project``.
        """
        return self.provenance.ensure_migration_project()

    def ensure_j2o_provenance_types(self, project_id: int) -> dict[str, int]:
        """Ensure work package types exist for each provenance entity type.

        Thin delegator over ``self.provenance.ensure_provenance_types``.
        """
        return self.provenance.ensure_provenance_types(project_id)

    def ensure_j2o_provenance_custom_fields(self) -> dict[str, int]:
        """Ensure custom fields for OP entity ID mapping exist.

        Thin delegator over ``self.provenance.ensure_provenance_custom_fields``.
        """
        return self.provenance.ensure_provenance_custom_fields()

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

        Thin delegator over ``self.provenance.record_entity``.
        """
        return self.provenance.record_entity(
            entity_type=entity_type,
            jira_key=jira_key,
            jira_id=jira_id,
            op_entity_id=op_entity_id,
            jira_name=jira_name,
        )

    def restore_entity_mappings_from_provenance(self, entity_type: str) -> dict[str, dict[str, Any]]:
        """Restore entity mappings from provenance work packages.

        Thin delegator over ``self.provenance.restore_entity_mappings``.
        """
        return self.provenance.restore_entity_mappings(entity_type)

    def bulk_record_entity_provenance(
        self,
        entity_type: str,
        mappings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk record provenance for multiple entities.

        Thin delegator over ``self.provenance.bulk_record_entities``.
        """
        return self.provenance.bulk_record_entities(entity_type, mappings)

    def get_roles(self) -> list[dict[str, Any]]:
        """Return OpenProject roles (id, name, builtin flag).

        Thin delegator over ``self.memberships.get_roles``.
        """
        return self.memberships.get_roles()

    def get_groups(self) -> list[dict[str, Any]]:
        """Return existing OpenProject groups with member IDs.

        Thin delegator over ``self.memberships.get_groups``.
        """
        return self.memberships.get_groups()

    def sync_group_memberships(self, assignments: list[dict[str, Any]]) -> dict[str, int]:
        """Ensure each group has the provided membership list.

        Thin delegator over ``self.memberships.sync_group_memberships``.
        """
        return self.memberships.sync_group_memberships(assignments)

    def assign_group_roles(
        self,
        assignments: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Assign OpenProject groups to projects with given role IDs.

        Thin delegator over ``self.memberships.assign_group_roles``.
        """
        return self.memberships.assign_group_roles(assignments)

    def assign_user_roles(
        self,
        *,
        project_id: int,
        user_id: int,
        role_ids: list[int],
    ) -> dict[str, Any]:
        """Ensure a user has the given roles on a project.

        Thin delegator over ``self.memberships.assign_user_roles``.
        """
        return self.memberships.assign_user_roles(
            project_id=project_id,
            user_id=user_id,
            role_ids=role_ids,
        )

    def sync_workflow_transitions(
        self,
        transitions: list[dict[str, int]],
        role_ids: list[int],
    ) -> dict[str, int]:
        """Thin delegator over ``self.project_setup.sync_workflow_transitions``."""
        return self.project_setup.sync_workflow_transitions(transitions, role_ids)

    def _read_result_file(
        self,
        container_path: Path,
        local_path: Path,
    ) -> dict[str, Any]:
        """Helper to read JSON results from container with cat fallback."""
        for _attempt in range(10):
            try:
                stdout, _stderr, rc = self.docker_client.execute_command(
                    f"cat {container_path.as_posix()}",
                )
            except Exception:
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
        """Thin delegator over ``self.project_setup.ensure_project_version``."""
        return self.project_setup.ensure_project_version(
            project_id,
            name=name,
            description=description,
            start_date=start_date,
            due_date=due_date,
            status=status,
            sharing=sharing,
        )

    def create_or_update_query(
        self,
        *,
        name: str,
        description: str | None = None,
        project_id: int | None = None,
        is_public: bool = True,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update an OpenProject query (saved filter).

        Thin delegator over ``self.content.create_or_update_query``.
        """
        return self.content.create_or_update_query(
            name=name,
            description=description,
            project_id=project_id,
            is_public=is_public,
            options=options,
        )

    def create_or_update_wiki_page(
        self,
        *,
        project_id: int,
        title: str,
        content: str,
    ) -> dict[str, Any]:
        """Create or update a Wiki page within a project.

        Thin delegator over ``self.content.create_or_update_wiki_page``.
        """
        return self.content.create_or_update_wiki_page(
            project_id=project_id,
            title=title,
            content=content,
        )

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

        Thin delegator over ``self.project_attributes.upsert_project_origin_attributes``.
        """
        return self.project_attributes.upsert_project_origin_attributes(
            project_id,
            origin_system=origin_system,
            project_key=project_key,
            external_id=external_id,
            external_url=external_url,
        )

    def upsert_project_attribute(
        self,
        project_id: int,
        *,
        name: str,
        value: str,
        field_format: str = "string",
    ) -> bool:
        """Create/enable a Project attribute and set its value for a project.

        Thin delegator over ``self.project_attributes.upsert_project_attribute``.
        """
        return self.project_attributes.upsert_project_attribute(
            project_id,
            name=name,
            value=value,
            field_format=field_format,
        )

    def bulk_upsert_project_attributes(
        self,
        attributes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk upsert project attributes in a single Rails call.

        Thin delegator over ``self.project_attributes.bulk_upsert_project_attributes``.
        """
        return self.project_attributes.bulk_upsert_project_attributes(attributes)

    def rename_project_attribute(self, *, old_name: str, new_name: str) -> bool:
        """Rename a Project attribute (ProjectCustomField) if it exists.

        Thin delegator over ``self.project_attributes.rename_project_attribute``.
        """
        return self.project_attributes.rename_project_attribute(old_name=old_name, new_name=new_name)

    def get_project_wp_cf_snapshot(self, project_id: int) -> list[dict[str, Any]]:
        """Return snapshot of WorkPackages in a project with Jira CFs and updated_at.

        Thin delegator over ``self.project_attributes.get_project_wp_cf_snapshot``.
        """
        return self.project_attributes.get_project_wp_cf_snapshot(project_id)

    def set_wp_last_update_date_by_keys(
        self,
        project_id: int,
        jira_keys: list[str],
        date_str: str,
    ) -> dict[str, Any]:
        """Thin delegator over ``self.wp_cf.set_wp_last_update_date_by_keys``."""
        return self.wp_cf.set_wp_last_update_date_by_keys(project_id, jira_keys, date_str)

    def bulk_create_records(
        self,
        model: str,
        records: list[dict[str, Any]],
        *,
        timeout: int | None = None,
        result_basename: str | None = None,
    ) -> dict[str, Any]:
        """Thin delegator over ``self.bulk_create.bulk_create_records``."""
        return self.bulk_create.bulk_create_records(
            model,
            records,
            timeout=timeout,
            result_basename=result_basename,
        )

    def find_record(
        self,
        model: str,
        id_or_conditions: int | dict[str, Any],
    ) -> dict[str, Any]:
        """Find a record by ID or conditions.

        Thin delegator over ``self.records.find_record``.
        """
        return self.records.find_record(model, id_or_conditions)

    def _retry_with_exponential_backoff(
        self,
        operation: Callable[[], object],
        operation_name: str,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        *,
        jitter: bool = True,
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

    def batch_find_records(
        self,
        model: str,
        ids: list[int | str],
        batch_size: int | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[int | str, dict[str, Any]]:
        """Find multiple records by IDs in batches with idempotency support.

        Thin delegator over ``self.records.batch_find_records``. The
        ``@batch_idempotent`` decorator lives on the service method;
        ``headers`` is keyword-only and forwarded as a kwarg so the
        decorator's ``extract_headers_from_kwargs`` helper can see it
        (positional headers would silently disable caching).
        """
        return self.records.batch_find_records(model, ids, batch_size, headers=headers)

    def create_record(self, model: str, attributes: dict[str, Any]) -> dict[str, Any]:
        """Create a new record.

        Thin delegator over ``self.records.create_record``.
        """
        return self.records.create_record(model, attributes)

    def update_record(
        self,
        model: str,
        record_id: int,
        attributes: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a record with given attributes.

        Thin delegator over ``self.records.update_record``.
        """
        return self.records.update_record(model, record_id, attributes)

    def delete_record(self, model: str, record_id: int) -> None:
        """Delete a record.

        Thin delegator over ``self.records.delete_record``.
        """
        self.records.delete_record(model, record_id)

    def find_all_records(
        self,
        model: str,
        conditions: dict[str, Any] | None = None,
        limit: int | None = None,
        includes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Find all records matching conditions.

        Thin delegator over ``self.records.find_all_records``.
        """
        return self.records.find_all_records(model, conditions, limit, includes)

    def execute_transaction(self, commands: list[str]) -> object:
        """Execute multiple commands in a transaction.

        Thin delegator over ``self.rails_runner.execute_transaction``.
        """
        return self.rails_runner.execute_transaction(commands)

    def transfer_file_from_container(
        self,
        container_path: Path,
        local_path: Path,
    ) -> Path:
        """Copy a file from the container to the local system.

        Thin delegator over ``self.file_transfer.transfer_file_from_container``.
        """
        return self.file_transfer.transfer_file_from_container(container_path, local_path)

    def get_users(self) -> list[dict[str, Any]]:
        """Get all users from OpenProject.

        Thin delegator over ``self.users.get_users``. Uses caching to avoid
        repeated Rails console queries.
        """
        return self.users.get_users()

    def get_user(self, user_identifier: int | str) -> dict[str, Any]:
        """Get a single user by id, email, or login.

        Thin delegator over ``self.users.get_user``.
        """
        return self.users.get_user(user_identifier)

    def get_user_by_email(self, email: str) -> dict[str, Any]:
        """Get a user by email address.

        Thin delegator over ``self.users.get_user_by_email``. Uses cached
        user data if available.
        """
        return self.users.get_user_by_email(email)

    def get_custom_field_by_name(self, name: str) -> dict[str, Any]:
        """Find a custom field by name.

        Thin delegator over ``self.custom_fields.get_by_name``.
        """
        return self.custom_fields.get_by_name(name)

    def get_custom_field_id_by_name(self, name: str) -> int:
        """Find a custom field ID by name.

        Thin delegator over ``self.custom_fields.get_id_by_name``.
        """
        return self.custom_fields.get_id_by_name(name)

    def get_custom_fields(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Get all custom fields from OpenProject (cached for 5 minutes).

        Thin delegator over ``self.custom_fields.get_all``.
        """
        return self.custom_fields.get_all(force_refresh=force_refresh)

    def get_statuses(self) -> list[dict[str, Any]]:
        """Get all statuses from OpenProject.

        Thin delegator over ``self.status_types.get_statuses``.
        """
        return self.status_types.get_statuses()

    def get_work_package_types(self) -> list[dict[str, Any]]:
        """Get all work package types from OpenProject.

        Thin delegator over ``self.status_types.get_work_package_types``.
        """
        return self.status_types.get_work_package_types()

    def get_projects(self, *, top_level_only: bool = False) -> list[dict[str, Any]]:
        """Get projects from OpenProject using file-based approach.

        Thin delegator over ``self.projects.get_projects``.
        """
        return self.projects.get_projects(top_level_only=top_level_only)

    def get_project_by_identifier(self, identifier: str) -> dict[str, Any]:
        """Get a project by identifier.

        Thin delegator over ``self.projects.get_project_by_identifier``.
        """
        return self.projects.get_project_by_identifier(identifier)

    def delete_all_work_packages(self) -> int:
        """Delete all work packages in bulk.

        Thin delegator over ``self.work_packages.delete_all_work_packages``.
        """
        return self.work_packages.delete_all_work_packages()

    def delete_all_projects(self) -> int:
        """Delete all projects in bulk.

        Thin delegator over ``self.admin_cleanup.delete_all_projects``.
        """
        return self.admin_cleanup.delete_all_projects()

    def delete_all_custom_fields(self) -> int:
        """Delete all custom fields in bulk.

        Thin delegator over ``self.custom_fields.delete_all_custom_fields``.
        """
        return self.custom_fields.delete_all_custom_fields()

    def delete_non_default_issue_types(self) -> int:
        """Delete non-default issue types (work package types).

        Thin delegator over ``self.admin_cleanup.delete_non_default_issue_types``.
        """
        return self.admin_cleanup.delete_non_default_issue_types()

    def delete_non_default_issue_statuses(self) -> int:
        """Delete non-default issue statuses.

        Thin delegator over ``self.admin_cleanup.delete_non_default_issue_statuses``.
        """
        return self.admin_cleanup.delete_non_default_issue_statuses()

    def get_time_entry_activities(self) -> list[dict[str, Any]]:
        """Get all available time entry activities from OpenProject.

        Thin delegator over ``self.time_entries.get_time_entry_activities``.
        """
        return self.time_entries.get_time_entry_activities()

    def create_time_entry(
        self,
        time_entry_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create a time entry in OpenProject.

        Thin delegator over ``self.time_entries.create_time_entry``.
        """
        return self.time_entries.create_time_entry(time_entry_data)

    def get_time_entries(
        self,
        work_package_id: int | None = None,
        user_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get time entries from OpenProject with optional filtering.

        Thin delegator over ``self.time_entries.get_time_entries``.
        """
        return self.time_entries.get_time_entries(work_package_id, user_id, limit)

    # ----- Priority helpers -----
    def get_issue_priorities(self) -> list[dict[str, Any]]:
        """Thin delegator over ``self.priorities.get_issue_priorities``."""
        return self.priorities.get_issue_priorities()

    def find_issue_priority_by_name(self, name: str) -> dict[str, Any] | None:
        """Thin delegator over ``self.priorities.find_issue_priority_by_name``."""
        return self.priorities.find_issue_priority_by_name(name)

    def create_issue_priority(self, name: str, position: int | None = None, is_default: bool = False) -> dict[str, Any]:
        """Thin delegator over ``self.priorities.create_issue_priority``."""
        return self.priorities.create_issue_priority(name, position, is_default)

    def ensure_local_avatars_enabled(self) -> bool:
        """Enable local avatar uploads if disabled.

        Thin delegator over ``self.users.ensure_local_avatars_enabled``.
        """
        return self.users.ensure_local_avatars_enabled()

    def set_user_avatar(
        self,
        *,
        user_id: int,
        container_path: Path,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Upload and assign a local avatar for a user.

        Thin delegator over ``self.users.set_user_avatar``.
        """
        return self.users.set_user_avatar(
            user_id=user_id,
            container_path=container_path,
            filename=filename,
            content_type=content_type,
        )

    # ----- Watchers helpers -----
    def find_watcher(self, work_package_id: int, user_id: int) -> dict[str, Any] | None:
        """Find a watcher for a work package and user if it exists.

        Thin delegator over ``self.associations.find_watcher``.
        """
        return self.associations.find_watcher(work_package_id, user_id)

    def add_watcher(self, work_package_id: int, user_id: int) -> bool:
        """Idempotently add a watcher to the work package.

        Thin delegator over ``self.associations.add_watcher``.
        """
        return self.associations.add_watcher(work_package_id, user_id)

    def bulk_add_watchers(
        self,
        watchers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Add multiple watchers in a single Rails call.

        Thin delegator over ``self.associations.bulk_add_watchers``.
        """
        return self.associations.bulk_add_watchers(watchers)

    def bulk_set_wp_custom_field_values(
        self,
        cf_values: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Thin delegator over ``self.wp_cf.bulk_set_wp_custom_field_values``."""
        return self.wp_cf.bulk_set_wp_custom_field_values(cf_values)

    def upsert_work_package_description_section(
        self,
        work_package_id: int,
        section_marker: str,
        content: str,
    ) -> bool:
        """Upsert a section in a work package's description.

        Thin delegator over ``self.wp_content.upsert_work_package_description_section``.
        """
        return self.wp_content.upsert_work_package_description_section(
            work_package_id,
            section_marker,
            content,
        )

    def bulk_upsert_wp_description_sections(
        self,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Upsert description sections for multiple work packages in a single Rails call.

        Thin delegator over ``self.wp_content.bulk_upsert_wp_description_sections``.
        """
        return self.wp_content.bulk_upsert_wp_description_sections(sections)

    def create_work_package_activity(
        self,
        work_package_id: int,
        activity_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create a journal/activity (comment) on a work package.

        Thin delegator over ``self.wp_content.create_work_package_activity``.
        """
        return self.wp_content.create_work_package_activity(work_package_id, activity_data)

    def bulk_create_work_package_activities(
        self,
        activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple journal/activity entries (comments) in a single Rails call.

        Thin delegator over ``self.wp_content.bulk_create_work_package_activities``.
        """
        return self.wp_content.bulk_create_work_package_activities(activities)

    def find_relation(
        self,
        from_work_package_id: int,
        to_work_package_id: int,
    ) -> dict[str, Any] | None:
        """Find a relation between two work packages if it exists.

        Thin delegator over ``self.associations.find_relation``.
        """
        return self.associations.find_relation(from_work_package_id, to_work_package_id)

    def create_relation(
        self,
        from_work_package_id: int,
        to_work_package_id: int,
        relation_type: str,
    ) -> dict[str, Any] | None:
        """Create a relation idempotently between two work packages.

        Thin delegator over ``self.associations.create_relation``.
        """
        return self.associations.create_relation(
            from_work_package_id,
            to_work_package_id,
            relation_type,
        )

    def bulk_create_relations(
        self,
        relations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple relations in a single Rails call.

        Thin delegator over ``self.associations.bulk_create_relations``.
        """
        return self.associations.bulk_create_relations(relations)

    def batch_create_time_entries(
        self,
        time_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple time entries via file-based JSON in the container.

        Thin delegator over ``self.time_entries.batch_create_time_entries``.
        """
        return self.time_entries.batch_create_time_entries(time_entries)

    # ===== ENHANCED PERFORMANCE FEATURES =====

    def get_performance_stats(self) -> dict[str, Any]:
        """Get comprehensive performance statistics."""
        return self.performance_optimizer.get_comprehensive_stats()

    # ===== BATCH OPERATIONS =====

    def batch_create_work_packages(
        self,
        work_packages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Thin delegator over ``self.bulk_create.batch_create_work_packages``."""
        return self.bulk_create.batch_create_work_packages(work_packages)

    def _create_work_packages_batch(
        self,
        work_packages: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, Any]:
        """Thin delegator over ``self.bulk_create._create_work_packages_batch``.

        Kept on the client because
        ``OpenProjectWorkPackageService.create_work_package`` reaches the
        batch worker through ``self._client._create_work_packages_batch`` —
        the delegator preserves that call shape after the move.
        """
        return self.bulk_create._create_work_packages_batch(work_packages, **_kwargs)

    def get_project_enhanced(self, project_id: int) -> dict[str, Any]:
        """Get comprehensive project information.

        Thin delegator over ``self.projects.get_project_enhanced``.
        """
        return self.projects.get_project_enhanced(project_id)

    def enable_project_modules(self, project_id: int, modules: list[str]) -> bool:
        """Thin delegator over ``self.project_setup.enable_project_modules``."""
        return self.project_setup.enable_project_modules(project_id, modules)

    def bulk_enable_project_modules(
        self,
        project_modules: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Thin delegator over ``self.project_setup.bulk_enable_project_modules``."""
        return self.project_setup.bulk_enable_project_modules(project_modules)

    def batch_get_users_by_ids(self, user_ids: list[int]) -> dict[int, dict]:
        """Retrieve multiple users in batches.

        Thin delegator over ``self.users.batch_get_users_by_ids``.
        """
        return self.users.batch_get_users_by_ids(user_ids)

    def stream_work_packages_for_project(
        self,
        project_id: int,
        batch_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream work packages for a project.

        Thin delegator over ``self.work_packages.stream_work_packages_for_project``.
        """
        yield from self.work_packages.stream_work_packages_for_project(project_id, batch_size)

    def batch_update_work_packages(
        self,
        updates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Update multiple work packages in batches.

        Thin delegator over ``self.work_packages.batch_update_work_packages``.
        """
        return self.work_packages.batch_update_work_packages(updates)

    def create_work_package(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Create a single work package.

        Thin delegator over ``self.work_packages.create_work_package``.
        """
        return self.work_packages.create_work_package(payload)

    def update_work_package(
        self,
        wp_id: int,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Update a single work package.

        Thin delegator over ``self.work_packages.update_work_package``.
        """
        return self.work_packages.update_work_package(wp_id, updates)

    def batch_get_users_by_emails(
        self,
        emails: list[str],
        batch_size: int | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple users by email addresses in batches with idempotency support.

        Thin delegator over ``self.users.batch_get_users_by_emails``. The
        ``@batch_idempotent`` decorator lives on the service method; its
        cache key comes from ``headers["X-Idempotency-Key"]`` when
        supplied, or a fresh UUID per call otherwise (so callers that
        actually want cached results must pass a stable header).
        ``headers`` is keyword-only and is forwarded as a kwarg so the
        decorator's ``extract_headers_from_kwargs`` helper can see it
        (positional headers would silently disable caching).
        """
        return self.users.batch_get_users_by_emails(
            emails,
            batch_size,
            headers=headers,
        )

    def batch_get_projects_by_identifiers(
        self,
        identifiers: list[str],
        batch_size: int | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple projects by identifiers in batches with idempotency support.

        Thin delegator over ``self.projects.batch_get_projects_by_identifiers``.
        The ``@batch_idempotent`` decorator lives on the service method;
        its cache key comes from ``headers["X-Idempotency-Key"]`` when
        supplied, or a fresh UUID per call otherwise. ``headers`` is
        keyword-only and is forwarded as a kwarg so the decorator's
        ``extract_headers_from_kwargs`` helper can see it (positional
        headers would silently disable caching).
        """
        return self.projects.batch_get_projects_by_identifiers(
            identifiers,
            batch_size,
            headers=headers,
        )

    @batch_idempotent(
        ttl=7200,
    )  # 2 hour TTL for custom field lookups (less frequent changes)
    def batch_get_custom_fields_by_names(
        self,
        names: list[str],
        batch_size: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple custom fields by names in batches with retry support.

        Thin delegator over ``self.custom_fields.batch_get_custom_fields_by_names``.
        """
        return self.custom_fields.batch_get_custom_fields_by_names(names, batch_size=batch_size)

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
        if model not in _ALLOWED_MODELS:
            msg = f"Model '{model}' not in allowed list: {sorted(_ALLOWED_MODELS)}"
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
