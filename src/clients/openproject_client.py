"""OpenProject client for interacting with OpenProject instances via SSH and Rails console."""

import json
import os
import random
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import config
from src.clients.docker_client import DockerClient
from src.clients.exceptions import (
    ClientConnectionError,
    JsonParseError,
    QueryExecutionError,
    RecordNotFoundError,
)
from src.clients.rails_console_client import (
    RailsConsoleClient,
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
        from src.clients.openproject_associations_service import OpenProjectAssociationsService
        from src.clients.openproject_custom_field_service import OpenProjectCustomFieldService
        from src.clients.openproject_file_transfer_service import OpenProjectFileTransferService
        from src.clients.openproject_issue_priority_service import OpenProjectIssuePriorityService
        from src.clients.openproject_membership_service import OpenProjectMembershipService
        from src.clients.openproject_project_service import OpenProjectProjectService
        from src.clients.openproject_provenance_service import OpenProjectProvenanceService
        from src.clients.openproject_rails_runner_service import OpenProjectRailsRunnerService
        from src.clients.openproject_records_service import OpenProjectRecordsService
        from src.clients.openproject_time_entry_service import OpenProjectTimeEntryService
        from src.clients.openproject_user_service import OpenProjectUserService
        from src.clients.openproject_work_package_service import OpenProjectWorkPackageService

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
            f"  display_name = '{escape_ruby_single_quoted(clean_name)}'\n"
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
            msg = f"Unexpected response when ensuring reporting project: {result!r}"
            raise QueryExecutionError(msg)
        if not result.get("success"):
            msg = f"Failed to ensure reporting project '{clean_identifier}': {result.get('error')}"
            raise QueryExecutionError(
                msg,
            )
        project_id = int(result.get("id", 0) or 0)
        if project_id <= 0:
            msg = f"Reporting project '{clean_identifier}' returned invalid id: {project_id}"
            raise QueryExecutionError(
                msg,
            )
        return project_id

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
        see :py:class:`~src.clients.openproject_custom_field_service.OpenProjectCustomFieldService`
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
    # ``src.clients.openproject_provenance_service.OpenProjectProvenanceService``
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

            container_payload = Path("/tmp") / payload_path.name
            container_output = Path("/tmp") / (payload_path.name + ".result")
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
        payload = f"system={escape_ruby_single_quoted(origin_system)};key={escape_ruby_single_quoted(project_key)};id={escape_ruby_single_quoted(external_id or '')};url={escape_ruby_single_quoted(external_url or '')}"
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
        except Exception as e:
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
          name = '{escape_ruby_single_quoted(name)}'.dup
          fmt  = '{escape_ruby_single_quoted(field_format)}'.dup
          val  = '{escape_ruby_single_quoted(value)}'.dup

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
        except Exception as e:
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
            data.append(
                {
                    "pid": int(attr["project_id"]),
                    "name": str(attr["name"]),
                    "value": str(attr.get("value", "")),
                    "fmt": str(attr.get("field_format", "string")),
                },
            )

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
        except Exception as e:
            self.logger.warning("Bulk upsert project attributes failed: %s", e)
            return {"success": False, "processed": 0, "failed": len(attributes), "error": str(e)}

    def rename_project_attribute(self, *, old_name: str, new_name: str) -> bool:
        """Rename a Project attribute (ProjectCustomField) if it exists.

        Returns True if renamed or already at new_name; False if missing or failed.
        """
        ruby = f"""
          old_name = '{escape_ruby_single_quoted(old_name)}'.dup
          new_name = '{escape_ruby_single_quoted(new_name)}'.dup
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
        except Exception as e:
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
            msg = "Invalid snapshot from OpenProject"
            raise QueryExecutionError(msg)
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
        except Exception as e:
            self.logger.warning(
                "Failed to set J2O Last Update Date for project %s: %s",
                project_id,
                e,
            )
            return {"updated": 0, "examined": 0, "error": str(e)}

    def bulk_create_records(
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
        # Validate model name against allowlist to prevent injection
        self._validate_model_name(model)

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
        container_json = Path("/tmp") / local_json.name
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
        container_result = Path("/tmp") / result_name
        local_result = temp_dir / result_name

        # Progress file within the container, mirrored locally for monitoring
        container_progress = Path("/tmp") / (result_name + ".progress")
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
            runner_script_path = f"/tmp/j2o_bulk_{os.urandom(4).hex()}.rb"
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
                        msg = "Rails console execution failed and runner fallback is disabled"
                        raise QueryExecutionError(
                            msg,
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
                        except Exception:
                            pass
                        msg = f"rails runner timed out for {runner_script_path}"
                        raise QueryExecutionError(
                            msg,
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
                    except Exception:
                        pass
                    msg = f"rails runner timed out for {runner_script_path}"
                    raise QueryExecutionError(
                        msg,
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
                except Exception:
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
            except Exception:
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
                                msg = f"bulk_create_records stalled for {stall_seconds}s without progress"
                                raise QueryExecutionError(
                                    msg,
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
                    from src.clients.rails_console_client import ConsoleNotReadyError

                    msg = "Forced runner mode via J2O_FORCE_RAILS_RUNNER"
                    raise ConsoleNotReadyError(msg)
                output = self.rails_client.execute(write_query, suppress_output=True)
                self._check_console_output_for_errors(output or "", context="get_statuses")
                logger.debug("Successfully executed statuses write command")
            except Exception as e:
                from src.clients.rails_console_client import (
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
                    runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"
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
                ssh_command = f"docker exec {shlex.quote(self.container_name)} cat {shlex.quote(file_path)}"
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
                            f"docker exec {shlex.quote(self.container_name)} rm -f {shlex.quote(file_path)}",
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

    def get_work_package_types(self) -> list[dict[str, Any]]:
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
                    from src.clients.rails_console_client import ConsoleNotReadyError

                    msg = "Forced runner mode via J2O_FORCE_RAILS_RUNNER"
                    raise ConsoleNotReadyError(msg)
                self.rails_client.execute(write_query, suppress_output=True)
                logger.debug("Successfully executed work package types write command")
            except Exception as e:
                from src.clients.rails_console_client import (
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
                    runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"
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
                ssh_command = f"docker exec {shlex.quote(self.container_name)} cat {shlex.quote(file_path)}"
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
                            f"docker exec {shlex.quote(self.container_name)} rm -f {shlex.quote(file_path)}",
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

        Thin delegator over ``self.custom_fields.delete_all_custom_fields``.
        """
        return self.custom_fields.delete_all_custom_fields()

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
        """Set custom field values for multiple work packages in a single Rails call.

        Thin delegator over ``self.custom_fields.bulk_set_wp_custom_field_values``.
        """
        return self.custom_fields.bulk_set_wp_custom_field_values(cf_values)

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
        # Escape content for Ruby single-quoted string
        safe_content = escape_ruby_single_quoted(content).replace("\n", "\\n")
        safe_marker = escape_ruby_single_quoted(section_marker)

        script = f"""
          wp = WorkPackage.find_by(id: {work_package_id})
          if !wp
            {{ success: false, error: 'WorkPackage not found' }}.to_json
          else
            desc = wp.description || ''
            marker = '## {safe_marker}'
            content = '{safe_content}'

            # Find existing section
            escaped_marker = Regexp.escape('## {safe_marker}')
            section_regex = /\\n?#{{escaped_marker}}\\n[\\s\\S]*?(?=\\n## |\\z)/
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
        except Exception as e:
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
            data.append(
                {
                    "wp_id": int(s["work_package_id"]),
                    "marker": str(s["section_marker"]),
                    "content": str(s["content"]),
                },
            )

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
        except Exception as e:
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
        escaped_comment = escape_ruby_single_quoted(comment_text)

        # OpenProject 15+ requires using journal_notes/journal_user + save!
        script = f"""
        begin
          wp = WorkPackage.find({work_package_id})
          user = User.current || User.find_by(admin: true)
          wp.journal_notes = '{escaped_comment}'
          wp.journal_user = user
          wp.save!
          {{ id: wp.journals.last.id, status: 'created' }}
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
        except Exception as e:
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
            data.append(
                {
                    "work_package_id": int(act["work_package_id"]),
                    "comment": str(comment),
                    "user_id": act.get("user_id"),
                },
            )

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        # NOTE: OpenProject 15+ requires using journal_notes/journal_user + save!
        # instead of direct journals.create! to properly set validity_period and data_type
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ created: 0, failed: 0, errors: [] }}
          default_user = User.current || User.find_by(admin: true)

          # Pre-fetch all referenced WPs and Users to avoid N+1 queries
          wp_ids = data.map {{ |d| d['work_package_id'] }}.compact.uniq
          user_ids = data.map {{ |d| d['user_id'] }}.compact.uniq
          wps = WorkPackage.where(id: wp_ids).index_by(&:id)
          users = User.where(id: user_ids).index_by(&:id)

          data.each do |item|
            begin
              wp = wps[item['work_package_id']]
              unless wp
                results[:failed] += 1
                results[:errors] << {{ wp_id: item['work_package_id'], error: 'WorkPackage not found' }}
                next
              end

              user = item['user_id'] ? (users[item['user_id']] || default_user) : default_user
              user ||= default_user

              comment_text = item['comment'].to_s
              next if comment_text.empty?

              # OpenProject 15+ journal creation - use journal_notes/journal_user
              wp.journal_notes = comment_text
              wp.journal_user = user
              wp.save!
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
        except Exception as e:
            logger.warning("Bulk create WP activities failed: %s", e)
            return {"success": False, "created": 0, "failed": len(activities), "error": str(e)}

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

        # Pre-fetch all referenced entities to avoid N+1 queries (6N -> 5 constant queries)
        project_ids = work_packages_data.map {{ |d| d['project_id'] }}.compact.uniq
        type_ids = work_packages_data.map {{ |d| d['type_id'] }}.compact.uniq
        type_names = work_packages_data.map {{ |d| d['type_name'] }}.compact.uniq
        status_ids = work_packages_data.map {{ |d| d['status_id'] }}.compact.uniq
        status_names = work_packages_data.map {{ |d| d['status_name'] }}.compact.uniq
        priority_ids = work_packages_data.map {{ |d| d['priority_id'] }}.compact.uniq
        priority_names = work_packages_data.map {{ |d| d['priority_name'] }}.compact.uniq
        user_ids = work_packages_data.flat_map {{ |d| [d['author_id'], d['assigned_to_id']] }}.compact.uniq

        projects_by_id = Project.where(id: project_ids).index_by(&:id)
        types_by_id = Type.where(id: type_ids).index_by(&:id)
        types_by_name = Type.where(name: type_names).index_by(&:name)
        statuses_by_id = Status.where(id: status_ids).index_by(&:id)
        statuses_by_name = Status.where(name: status_names).index_by(&:name)
        priorities_by_id = IssuePriority.where(id: priority_ids).index_by(&:id)
        priorities_by_name = IssuePriority.where(name: priority_names).index_by(&:name)
        users_by_id = User.where(id: user_ids).index_by(&:id)

        work_packages_data.each do |wp_data|
          begin
            # Create work package with provided attributes
            wp = WorkPackage.new

            # Set basic attributes
            wp.subject = wp_data['subject'] if wp_data['subject']
            wp.description = wp_data['description'] if wp_data['description']

            # Set project (required) - using pre-fetched lookup
            if wp_data['project_id']
              wp.project = projects_by_id[wp_data['project_id']]
            end

            # Set type (required) - using pre-fetched lookup
            if wp_data['type_id']
              wp.type = types_by_id[wp_data['type_id']]
            elsif wp_data['type_name']
              wp.type = types_by_name[wp_data['type_name']]
            end

            # Set status - using pre-fetched lookup
            if wp_data['status_id']
              wp.status = statuses_by_id[wp_data['status_id']]
            elsif wp_data['status_name']
              wp.status = statuses_by_name[wp_data['status_name']]
            end

            # Set priority - using pre-fetched lookup
            if wp_data['priority_id']
              wp.priority = priorities_by_id[wp_data['priority_id']]
            elsif wp_data['priority_name']
              wp.priority = priorities_by_name[wp_data['priority_name']]
            end

            # Set author - using pre-fetched lookup
            if wp_data['author_id']
              wp.author = users_by_id[wp_data['author_id']]
            end

            # Set assignee - using pre-fetched lookup
            if wp_data['assigned_to_id']
              wp.assigned_to = users_by_id[wp_data['assigned_to_id']]
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

              # Set original timestamps if provided (using update_columns to bypass callbacks)
              timestamp_attrs = {{}}
              timestamp_attrs[:created_at] = Time.parse(wp_data['created_at']) if wp_data['created_at']
              timestamp_attrs[:updated_at] = Time.parse(wp_data['updated_at']) if wp_data['updated_at']
              wp.update_columns(timestamp_attrs) if timestamp_attrs.any?

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
        """Get comprehensive project information.

        Thin delegator over ``self.projects.get_project_enhanced``.
        """
        return self.projects.get_project_enhanced(project_id)

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
        except Exception as e:
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
                data.append(
                    {
                        "pid": int(pm["project_id"]),
                        "modules": [str(m) for m in pm["modules"]],
                    },
                )

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
        except Exception as e:
            logger.warning("Bulk enable project modules failed: %s", e)
            return {"success": False, "processed": 0, "failed": len(data), "error": str(e)}

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
