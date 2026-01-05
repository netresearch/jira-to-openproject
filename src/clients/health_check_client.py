"""Health check client for monitoring disk space and temp file accumulation.

Provides pre-migration, during-migration, and post-migration health checks
to prevent failures due to disk space exhaustion or temp file accumulation.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .docker_client import DockerClient
    from .ssh_client import SSHClient

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    """Status of a single health check."""

    name: str
    healthy: bool
    metric: int | float | str
    threshold: int | float | str
    units: str
    severity: str  # "ERROR", "WARNING", "INFO"
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class HealthSnapshot:
    """Snapshot of system health at a point in time."""

    timestamp: datetime
    local_disk_free_mb: int
    remote_disk_free_mb: int
    container_disk_free_mb: int
    container_inodes_free: int
    temp_file_count: int
    checks: dict[str, HealthStatus] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """Return True if all checks are healthy or only warnings."""
        return all(
            check.healthy or check.severity != "ERROR" for check in self.checks.values()
        )

    @property
    def has_warnings(self) -> bool:
        """Return True if any checks have warnings."""
        return any(
            not check.healthy and check.severity == "WARNING"
            for check in self.checks.values()
        )

    @property
    def has_errors(self) -> bool:
        """Return True if any checks have errors."""
        return any(
            not check.healthy and check.severity == "ERROR"
            for check in self.checks.values()
        )


@dataclass
class CleanupResult:
    """Result of a cleanup operation."""

    success: bool
    files_before: int
    files_after: int
    files_removed: int
    space_freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def error(self) -> str | None:
        """Return first error or None."""
        return self.errors[0] if self.errors else None

    @property
    def space_freed_human(self) -> str:
        """Return human-readable space freed."""
        if self.space_freed_bytes >= 1024 * 1024 * 1024:
            return f"{self.space_freed_bytes / (1024 * 1024 * 1024):.2f}GB"
        if self.space_freed_bytes >= 1024 * 1024:
            return f"{self.space_freed_bytes / (1024 * 1024):.2f}MB"
        if self.space_freed_bytes >= 1024:
            return f"{self.space_freed_bytes / 1024:.2f}KB"
        return f"{self.space_freed_bytes}B"


class HealthCheckClient:
    """Client for performing health checks on the migration environment.

    Monitors disk space, inode availability, and temp file accumulation
    on both local and remote systems.
    """

    # Default thresholds
    DEFAULT_LOCAL_DISK_WARNING_MB = 500
    DEFAULT_LOCAL_DISK_ERROR_MB = 200
    DEFAULT_REMOTE_DISK_WARNING_MB = 2000
    DEFAULT_REMOTE_DISK_ERROR_MB = 1000
    DEFAULT_CONTAINER_DISK_WARNING_MB = 1500
    DEFAULT_CONTAINER_DISK_ERROR_MB = 500
    DEFAULT_INODES_WARNING = 10000
    DEFAULT_INODES_ERROR = 5000
    DEFAULT_FILE_COUNT_WARNING = 300
    DEFAULT_FILE_COUNT_ERROR = 10000  # Temporarily raised from 1000 - old files need root cleanup

    def __init__(
        self,
        ssh_client: SSHClient,
        docker_client: DockerClient,
        container_name: str,
        *,
        local_temp_path: str = "/tmp",
        thresholds: dict[str, int] | None = None,
    ) -> None:
        """Initialize the health check client.

        Args:
            ssh_client: SSH client for remote commands
            docker_client: Docker client for container commands
            container_name: Name of the OpenProject container
            local_temp_path: Local path to check for disk space
            thresholds: Optional custom thresholds for health checks
        """
        self.ssh_client = ssh_client
        self.docker_client = docker_client
        self.container_name = container_name
        self.local_temp_path = local_temp_path
        self.thresholds = thresholds or {}
        self._baseline_snapshot: HealthSnapshot | None = None

    def _get_threshold(self, name: str, default: int) -> int:
        """Get a threshold value, using custom or default."""
        return self.thresholds.get(name, default)

    def check_local_disk_space(self) -> HealthStatus:
        """Check available disk space on local system."""
        import shutil

        try:
            usage = shutil.disk_usage(self.local_temp_path)
            free_mb = usage.free // (1024 * 1024)

            warning_threshold = self._get_threshold(
                "local_disk_warning_mb", self.DEFAULT_LOCAL_DISK_WARNING_MB
            )
            error_threshold = self._get_threshold(
                "local_disk_error_mb", self.DEFAULT_LOCAL_DISK_ERROR_MB
            )

            if free_mb < error_threshold:
                return HealthStatus(
                    name="local_disk_space",
                    healthy=False,
                    metric=free_mb,
                    threshold=error_threshold,
                    units="MB",
                    severity="ERROR",
                    message=f"Local disk critically low: {free_mb}MB free (need {error_threshold}MB)",
                )
            if free_mb < warning_threshold:
                return HealthStatus(
                    name="local_disk_space",
                    healthy=False,
                    metric=free_mb,
                    threshold=warning_threshold,
                    units="MB",
                    severity="WARNING",
                    message=f"Local disk low: {free_mb}MB free (want {warning_threshold}MB)",
                )
            return HealthStatus(
                name="local_disk_space",
                healthy=True,
                metric=free_mb,
                threshold=warning_threshold,
                units="MB",
                severity="INFO",
                message=f"Local disk OK: {free_mb}MB free",
            )
        except Exception as e:
            return HealthStatus(
                name="local_disk_space",
                healthy=False,
                metric=0,
                threshold=0,
                units="MB",
                severity="ERROR",
                message=f"Failed to check local disk: {e}",
            )

    def check_remote_disk_space(self) -> HealthStatus:
        """Check available disk space on remote host."""
        try:
            stdout, stderr, rc = self.ssh_client.execute_command(
                "df /tmp | tail -1 | awk '{print $4}'"
            )
            if rc != 0:
                return HealthStatus(
                    name="remote_disk_space",
                    healthy=False,
                    metric=0,
                    threshold=0,
                    units="MB",
                    severity="ERROR",
                    message=f"Failed to check remote disk: {stderr}",
                )

            # Parse output (in KB)
            free_kb = int(stdout.strip())
            free_mb = free_kb // 1024

            warning_threshold = self._get_threshold(
                "remote_disk_warning_mb", self.DEFAULT_REMOTE_DISK_WARNING_MB
            )
            error_threshold = self._get_threshold(
                "remote_disk_error_mb", self.DEFAULT_REMOTE_DISK_ERROR_MB
            )

            if free_mb < error_threshold:
                return HealthStatus(
                    name="remote_disk_space",
                    healthy=False,
                    metric=free_mb,
                    threshold=error_threshold,
                    units="MB",
                    severity="ERROR",
                    message=f"Remote disk critically low: {free_mb}MB free",
                )
            if free_mb < warning_threshold:
                return HealthStatus(
                    name="remote_disk_space",
                    healthy=False,
                    metric=free_mb,
                    threshold=warning_threshold,
                    units="MB",
                    severity="WARNING",
                    message=f"Remote disk low: {free_mb}MB free",
                )
            return HealthStatus(
                name="remote_disk_space",
                healthy=True,
                metric=free_mb,
                threshold=warning_threshold,
                units="MB",
                severity="INFO",
                message=f"Remote disk OK: {free_mb}MB free",
            )
        except Exception as e:
            return HealthStatus(
                name="remote_disk_space",
                healthy=False,
                metric=0,
                threshold=0,
                units="MB",
                severity="ERROR",
                message=f"Failed to check remote disk: {e}",
            )

    def check_container_disk_space(self) -> HealthStatus:
        """Check available disk space inside container."""
        try:
            stdout, stderr, rc = self.ssh_client.execute_command(
                f"docker exec {self.container_name} df /tmp | tail -1 | awk '{{print $4}}'"
            )
            if rc != 0:
                return HealthStatus(
                    name="container_disk_space",
                    healthy=False,
                    metric=0,
                    threshold=0,
                    units="MB",
                    severity="ERROR",
                    message=f"Failed to check container disk: {stderr}",
                )

            free_kb = int(stdout.strip())
            free_mb = free_kb // 1024

            warning_threshold = self._get_threshold(
                "container_disk_warning_mb", self.DEFAULT_CONTAINER_DISK_WARNING_MB
            )
            error_threshold = self._get_threshold(
                "container_disk_error_mb", self.DEFAULT_CONTAINER_DISK_ERROR_MB
            )

            if free_mb < error_threshold:
                return HealthStatus(
                    name="container_disk_space",
                    healthy=False,
                    metric=free_mb,
                    threshold=error_threshold,
                    units="MB",
                    severity="ERROR",
                    message=f"Container disk critically low: {free_mb}MB free",
                )
            if free_mb < warning_threshold:
                return HealthStatus(
                    name="container_disk_space",
                    healthy=False,
                    metric=free_mb,
                    threshold=warning_threshold,
                    units="MB",
                    severity="WARNING",
                    message=f"Container disk low: {free_mb}MB free",
                )
            return HealthStatus(
                name="container_disk_space",
                healthy=True,
                metric=free_mb,
                threshold=warning_threshold,
                units="MB",
                severity="INFO",
                message=f"Container disk OK: {free_mb}MB free",
            )
        except Exception as e:
            return HealthStatus(
                name="container_disk_space",
                healthy=False,
                metric=0,
                threshold=0,
                units="MB",
                severity="ERROR",
                message=f"Failed to check container disk: {e}",
            )

    def check_container_inodes(self) -> HealthStatus:
        """Check available inodes in container /tmp."""
        try:
            stdout, stderr, rc = self.ssh_client.execute_command(
                f"docker exec {self.container_name} df -i /tmp | tail -1 | awk '{{print $4}}'"
            )
            if rc != 0:
                return HealthStatus(
                    name="container_inodes",
                    healthy=False,
                    metric=0,
                    threshold=0,
                    units="inodes",
                    severity="ERROR",
                    message=f"Failed to check container inodes: {stderr}",
                )

            free_inodes = int(stdout.strip())

            warning_threshold = self._get_threshold(
                "inodes_warning", self.DEFAULT_INODES_WARNING
            )
            error_threshold = self._get_threshold(
                "inodes_error", self.DEFAULT_INODES_ERROR
            )

            if free_inodes < error_threshold:
                return HealthStatus(
                    name="container_inodes",
                    healthy=False,
                    metric=free_inodes,
                    threshold=error_threshold,
                    units="inodes",
                    severity="ERROR",
                    message=f"Container inodes critically low: {free_inodes} free",
                )
            if free_inodes < warning_threshold:
                return HealthStatus(
                    name="container_inodes",
                    healthy=False,
                    metric=free_inodes,
                    threshold=warning_threshold,
                    units="inodes",
                    severity="WARNING",
                    message=f"Container inodes low: {free_inodes} free",
                )
            return HealthStatus(
                name="container_inodes",
                healthy=True,
                metric=free_inodes,
                threshold=warning_threshold,
                units="inodes",
                severity="INFO",
                message=f"Container inodes OK: {free_inodes} free",
            )
        except Exception as e:
            return HealthStatus(
                name="container_inodes",
                healthy=False,
                metric=0,
                threshold=0,
                units="inodes",
                severity="ERROR",
                message=f"Failed to check container inodes: {e}",
            )

    def check_temp_file_count(self, pattern: str = "j2o_*") -> HealthStatus:
        """Check count of temp files matching pattern in container /tmp."""
        try:
            stdout, stderr, rc = self.ssh_client.execute_command(
                f"docker exec {self.container_name} sh -c 'find /tmp -name \"{pattern}\" 2>/dev/null | wc -l'"
            )
            if rc != 0:
                return HealthStatus(
                    name="temp_file_count",
                    healthy=False,
                    metric=0,
                    threshold=0,
                    units="files",
                    severity="ERROR",
                    message=f"Failed to count temp files: {stderr}",
                )

            file_count = int(stdout.strip())

            warning_threshold = self._get_threshold(
                "file_count_warning", self.DEFAULT_FILE_COUNT_WARNING
            )
            error_threshold = self._get_threshold(
                "file_count_error", self.DEFAULT_FILE_COUNT_ERROR
            )

            if file_count >= error_threshold:
                return HealthStatus(
                    name="temp_file_count",
                    healthy=False,
                    metric=file_count,
                    threshold=error_threshold,
                    units="files",
                    severity="ERROR",
                    message=f"Too many temp files: {file_count} (max {error_threshold})",
                )
            if file_count >= warning_threshold:
                return HealthStatus(
                    name="temp_file_count",
                    healthy=False,
                    metric=file_count,
                    threshold=warning_threshold,
                    units="files",
                    severity="WARNING",
                    message=f"High temp file count: {file_count} (warn at {warning_threshold})",
                )
            return HealthStatus(
                name="temp_file_count",
                healthy=True,
                metric=file_count,
                threshold=warning_threshold,
                units="files",
                severity="INFO",
                message=f"Temp file count OK: {file_count} files",
            )
        except Exception as e:
            return HealthStatus(
                name="temp_file_count",
                healthy=False,
                metric=0,
                threshold=0,
                units="files",
                severity="ERROR",
                message=f"Failed to count temp files: {e}",
            )

    def check_cleanup_capability(self) -> HealthStatus:
        """Test that cleanup mechanism works by creating and deleting a test file."""
        test_file = f"/tmp/j2o_health_check_test_{int(time.time())}"
        try:
            # Create test file
            stdout, stderr, rc = self.ssh_client.execute_command(
                f"docker exec {self.container_name} touch {test_file}"
            )
            if rc != 0:
                return HealthStatus(
                    name="cleanup_capability",
                    healthy=False,
                    metric="create_failed",
                    threshold="success",
                    units="",
                    severity="ERROR",
                    message=f"Cannot create test file: {stderr}",
                )

            # Delete test file
            stdout, stderr, rc = self.ssh_client.execute_command(
                f"docker exec {self.container_name} rm -f {test_file}"
            )
            if rc != 0:
                return HealthStatus(
                    name="cleanup_capability",
                    healthy=False,
                    metric="delete_failed",
                    threshold="success",
                    units="",
                    severity="ERROR",
                    message=f"Cannot delete test file: {stderr}",
                )

            # Verify file is gone
            stdout, stderr, rc = self.ssh_client.execute_command(
                f"docker exec {self.container_name} test -f {test_file} && echo exists || echo deleted"
            )
            if "exists" in stdout:
                return HealthStatus(
                    name="cleanup_capability",
                    healthy=False,
                    metric="verify_failed",
                    threshold="success",
                    units="",
                    severity="ERROR",
                    message="Test file still exists after deletion",
                )

            return HealthStatus(
                name="cleanup_capability",
                healthy=True,
                metric="success",
                threshold="success",
                units="",
                severity="INFO",
                message="Cleanup capability verified",
            )
        except Exception as e:
            return HealthStatus(
                name="cleanup_capability",
                healthy=False,
                metric="exception",
                threshold="success",
                units="",
                severity="ERROR",
                message=f"Cleanup test failed: {e}",
            )

    def get_health_snapshot(self) -> HealthSnapshot:
        """Get a complete health snapshot of the system."""
        now = datetime.now(timezone.utc)

        checks = {}

        # Run all checks
        checks["local_disk_space"] = self.check_local_disk_space()
        checks["remote_disk_space"] = self.check_remote_disk_space()
        checks["container_disk_space"] = self.check_container_disk_space()
        checks["container_inodes"] = self.check_container_inodes()
        checks["temp_file_count"] = self.check_temp_file_count()
        checks["cleanup_capability"] = self.check_cleanup_capability()

        # Extract metrics for snapshot
        local_disk_free = (
            int(checks["local_disk_space"].metric)
            if isinstance(checks["local_disk_space"].metric, (int, float))
            else 0
        )
        remote_disk_free = (
            int(checks["remote_disk_space"].metric)
            if isinstance(checks["remote_disk_space"].metric, (int, float))
            else 0
        )
        container_disk_free = (
            int(checks["container_disk_space"].metric)
            if isinstance(checks["container_disk_space"].metric, (int, float))
            else 0
        )
        container_inodes_free = (
            int(checks["container_inodes"].metric)
            if isinstance(checks["container_inodes"].metric, (int, float))
            else 0
        )
        temp_file_count = (
            int(checks["temp_file_count"].metric)
            if isinstance(checks["temp_file_count"].metric, (int, float))
            else 0
        )

        return HealthSnapshot(
            timestamp=now,
            local_disk_free_mb=local_disk_free,
            remote_disk_free_mb=remote_disk_free,
            container_disk_free_mb=container_disk_free,
            container_inodes_free=container_inodes_free,
            temp_file_count=temp_file_count,
            checks=checks,
        )

    def cleanup_temp_files(
        self, pattern: str = "j2o_*", max_age_minutes: int = 60
    ) -> CleanupResult:
        """Clean up old temp files matching pattern.

        Args:
            pattern: Glob pattern for files to clean
            max_age_minutes: Only clean files older than this many minutes

        Returns:
            CleanupResult with details of cleanup operation
        """
        start_time = time.time()
        errors: list[str] = []

        try:
            # Count files before
            stdout, _, _ = self.ssh_client.execute_command(
                f"docker exec {self.container_name} sh -c 'find /tmp -name \"{pattern}\" 2>/dev/null | wc -l'"
            )
            files_before = int(stdout.strip()) if stdout.strip().isdigit() else 0

            # Delete old files
            stdout, stderr, rc = self.ssh_client.execute_command(
                f"docker exec {self.container_name} find /tmp -name '{pattern}' -mmin +{max_age_minutes} -delete 2>&1"
            )
            if rc != 0 and stderr:
                errors.append(f"Cleanup command failed: {stderr}")

            # Count files after
            stdout, _, _ = self.ssh_client.execute_command(
                f"docker exec {self.container_name} sh -c 'find /tmp -name \"{pattern}\" 2>/dev/null | wc -l'"
            )
            files_after = int(stdout.strip()) if stdout.strip().isdigit() else 0

            duration = time.time() - start_time

            return CleanupResult(
                success=len(errors) == 0,
                files_before=files_before,
                files_after=files_after,
                files_removed=files_before - files_after,
                errors=errors,
                duration_seconds=duration,
            )
        except Exception as e:
            duration = time.time() - start_time
            return CleanupResult(
                success=False,
                files_before=0,
                files_after=0,
                files_removed=0,
                errors=[str(e)],
                duration_seconds=duration,
            )

    def run_pre_migration_checks(self) -> tuple[bool, list[str]]:
        """Run all pre-migration health checks.

        Returns:
            Tuple of (passed, issues) where passed is True if all critical checks pass
            and issues is a list of issue messages
        """
        logger.info("Running pre-migration health checks...")
        snapshot = self.get_health_snapshot()
        issues: list[str] = []

        # Log all check results
        for name, check in snapshot.checks.items():
            level = (
                logging.ERROR
                if check.severity == "ERROR"
                else (logging.WARNING if check.severity == "WARNING" else logging.INFO)
            )
            logger.log(level, "Health check [%s]: %s", name, check.message)
            if not check.healthy:
                prefix = "CRITICAL: " if check.severity == "ERROR" else "WARNING: "
                issues.append(f"{prefix}{check.message}")

        # Store baseline for trend analysis
        self._baseline_snapshot = snapshot

        if snapshot.has_errors:
            logger.error("Pre-migration health checks FAILED")
            return False, issues

        if snapshot.has_warnings:
            logger.warning("Pre-migration health checks passed with WARNINGS")
        else:
            logger.info("Pre-migration health checks PASSED")

        return True, issues

    def run_during_migration_check(
        self, previous_snapshot: HealthSnapshot | None = None
    ) -> dict[str, Any]:
        """Run health checks during migration and check for degradation.

        Args:
            previous_snapshot: Previous snapshot to compare against

        Returns:
            Dict with keys: healthy, warnings, temp_files_exceeded
        """
        current = self.get_health_snapshot()
        previous = previous_snapshot or self._baseline_snapshot
        warnings: list[str] = []
        temp_files_exceeded = False

        # Check for errors
        if current.has_errors:
            for name, check in current.checks.items():
                if check.severity == "ERROR" and not check.healthy:
                    warnings.append(check.message)

        # Check temp file threshold
        temp_check = current.checks.get("temp_file_count")
        if temp_check and not temp_check.healthy:
            temp_files_exceeded = True
            if temp_check.severity == "WARNING":
                warnings.append(temp_check.message)

        # Check for degradation trends
        if previous:
            disk_decrease = previous.container_disk_free_mb - current.container_disk_free_mb
            file_increase = current.temp_file_count - previous.temp_file_count

            if disk_decrease > 500:
                warnings.append(f"Disk space decreased by {disk_decrease}MB since last check")

            if file_increase > 100:
                warnings.append(f"Temp file count increased by {file_increase} since last check")

        # Update stored snapshot for next comparison
        self._baseline_snapshot = current

        return {
            "healthy": current.healthy,
            "warnings": warnings,
            "temp_files_exceeded": temp_files_exceeded,
            "snapshot": current,
        }
