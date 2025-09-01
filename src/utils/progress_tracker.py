"""Enhanced progress tracking system for migration operations.

This module provides comprehensive progress tracking with real-time reporting,
ETA calculations, and multiple output formats for long-running migration tasks.
"""

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from src.display import configure_logging


class ProgressStage(Enum):
    """Migration stages for progress tracking."""

    INITIALIZING = "initializing"
    EXTRACTING = "extracting"
    PROCESSING = "processing"
    BATCHING = "batching"
    MIGRATING = "migrating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ProgressMetrics:
    """Metrics for progress tracking."""

    stage: ProgressStage = ProgressStage.INITIALIZING
    total_items: int = 0
    processed_items: int = 0
    failed_items: int = 0
    skipped_items: int = 0
    start_time: float = field(default_factory=time.time)
    last_update_time: float = field(default_factory=time.time)

    # Performance metrics
    items_per_second: float = 0.0
    estimated_time_remaining: float = 0.0

    # Error tracking
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def completion_percentage(self) -> float:
        """Calculate completion percentage."""
        if self.total_items == 0:
            return 0.0
        return (self.processed_items / self.total_items) * 100

    @property
    def elapsed_time(self) -> float:
        """Calculate elapsed time in seconds."""
        return time.time() - self.start_time

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        total_attempted = self.processed_items + self.failed_items
        if total_attempted == 0:
            return 100.0
        return (self.processed_items / total_attempted) * 100


class ProgressTracker:
    """Enhanced progress tracker with multiple output formats."""

    def __init__(
        self,
        operation_name: str = "Migration",
        *,
        enable_console_output: bool = True,
        enable_logging: bool = True,
        enable_file_output: bool = False,
        output_file: str | None = None,
        update_interval: float = 0.5,
    ) -> None:
        """Initialize the progress tracker.

        Args:
            operation_name: Name of the operation being tracked
            enable_console_output: Show progress in console
            enable_logging: Log progress to logger
            enable_file_output: When True, write progress to a JSON file
            output_file: File path for progress output (if provided)
            update_interval: Minimum time between updates

        """
        self.operation_name = operation_name
        self.enable_console_output = enable_console_output
        self.enable_logging = enable_logging
        # Determine output file path when file output is enabled
        if enable_file_output and not output_file:
            try:
                results_dir = Path("var/results")
                results_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(tz=UTC).strftime("%Y-%m-%d_%H-%M-%S")
                self.output_file = str(results_dir / f"progress_{ts}.json")
            except Exception:  # noqa: BLE001 - best-effort file output setup
                self.output_file = None
        else:
            self.output_file = output_file
        self.update_interval = update_interval

        self.logger = configure_logging("INFO", None)
        self.console = Console()

        # Progress tracking
        self.metrics = ProgressMetrics()
        self.callbacks: list[Callable[[ProgressMetrics], None]] = []

        # Rich progress components
        self.progress = None
        self.task_id = None
        self.live = None

        # Threading
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._update_thread = None

    def add_callback(self, callback: Callable[[ProgressMetrics], None]) -> None:
        """Add a progress callback function.

        Args:
            callback: Function that receives ProgressMetrics

        """
        self.callbacks.append(callback)

    def start(
        self,
        total_items: int,
        stage: ProgressStage = ProgressStage.PROCESSING,
    ) -> None:
        """Start progress tracking.

        Args:
            total_items: Total number of items to process
            stage: Initial stage of the operation

        """
        with self._lock:
            self.metrics = ProgressMetrics(
                stage=stage,
                total_items=total_items,
                start_time=time.time(),
                last_update_time=time.time(),
            )

        if self.enable_console_output:
            self._start_rich_progress()

        if self.enable_logging:
            self.logger.info(
                "Starting %s: %d items to process",
                self.operation_name,
                total_items,
            )

        # Start background update thread
        self._stop_event.clear()
        self._update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self._update_thread.start()

    def update(
        self,
        processed: int = 0,
        failed: int = 0,
        skipped: int = 0,
        stage: ProgressStage | None = None,
        message: str | None = None,
    ) -> None:
        """Update progress metrics.

        Args:
            processed: Number of newly processed items
            failed: Number of newly failed items
            skipped: Number of newly skipped items
            stage: Updated stage (optional)
            message: Status message (optional)

        """
        with self._lock:
            self.metrics.processed_items += processed
            self.metrics.failed_items += failed
            self.metrics.skipped_items += skipped

            if stage:
                self.metrics.stage = stage

            # Update performance metrics
            current_time = time.time()
            elapsed = current_time - self.metrics.start_time

            if elapsed > 0:
                self.metrics.items_per_second = self.metrics.processed_items / elapsed

                # Calculate ETA based on current rate
                remaining_items = (
                    self.metrics.total_items - self.metrics.processed_items
                )
                if self.metrics.items_per_second > 0:
                    self.metrics.estimated_time_remaining = (
                        remaining_items / self.metrics.items_per_second
                    )

            self.metrics.last_update_time = current_time

        # Update rich progress
        if self.progress and self.task_id:
            self.progress.update(
                self.task_id,
                completed=self.metrics.processed_items,
                description=message
                or f"{self.operation_name} - {self.metrics.stage.value}",
            )

        # Trigger callbacks
        for callback in self.callbacks:
            try:
                callback(self.metrics)
            except (RuntimeError, ValueError, TypeError) as e:
                self.logger.warning("Progress callback error: %s", e)

    def add_error(self, error: str) -> None:
        """Add an error to the tracking.

        Args:
            error: Error message

        """
        with self._lock:
            self.metrics.errors.append(error)

    def add_warning(self, warning: str) -> None:
        """Add a warning to the tracking.

        Args:
            warning: Warning message

        """
        with self._lock:
            self.metrics.warnings.append(warning)

    def finish(
        self,
        *,
        success: bool = True,
        final_message: str | None = None,
    ) -> ProgressMetrics:
        """Finish progress tracking and return final metrics.

        Args:
            success: Whether the operation completed successfully
            final_message: Final status message

        Returns:
            Final progress metrics

        """
        # Stop update thread
        self._stop_event.set()
        if self._update_thread:
            self._update_thread.join(timeout=1.0)

        with self._lock:
            self.metrics.stage = (
                ProgressStage.COMPLETED if success else ProgressStage.FAILED
            )

        # Final updates
        if self.progress and self.task_id:
            if success:
                self.progress.update(
                    self.task_id,
                    completed=self.metrics.total_items,
                    description=final_message or f"{self.operation_name} - Completed",
                )
            else:
                self.progress.update(
                    self.task_id,
                    description=final_message or f"{self.operation_name} - Failed",
                )

        # Stop rich progress
        if self.live:
            self.live.stop()

        # Final logging
        if self.enable_logging:
            elapsed = self.metrics.elapsed_time
            if success:
                self.logger.info(
                    "%s completed: %d/%d items in %.2fs (%.1f items/s)",
                    self.operation_name,
                    self.metrics.processed_items,
                    self.metrics.total_items,
                    elapsed,
                    self.metrics.items_per_second,
                )
            else:
                self.logger.error(
                    "%s failed after %.2fs: %d/%d items processed",
                    self.operation_name,
                    elapsed,
                    self.metrics.processed_items,
                    self.metrics.total_items,
                )

        # Save to file if enabled
        if self.output_file:
            self._save_to_file()

        return self.metrics

    def _start_rich_progress(self) -> None:
        """Initialize Rich progress display."""
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self.console,
            refresh_per_second=2,
        )

        self.task_id = self.progress.add_task(
            f"{self.operation_name} - {self.metrics.stage.value}",
            total=self.metrics.total_items,
        )

        # Create live display with additional info
        self.live = Live(
            self._create_progress_panel(),
            console=self.console,
            refresh_per_second=2,
            auto_refresh=True,
        )
        self.live.start()

    def _create_progress_panel(self) -> Panel:
        """Create a Rich panel with progress information."""
        # Create summary table
        table = Table.grid(padding=1)
        table.add_column(style="cyan", no_wrap=True)
        table.add_column(style="magenta")

        with self._lock:
            table.add_row("Stage:", self.metrics.stage.value.title())
            table.add_row(
                "Progress:",
                f"{self.metrics.processed_items}/{self.metrics.total_items}",
            )
            table.add_row("Success Rate:", f"{self.metrics.success_rate:.1f}%")
            table.add_row("Speed:", f"{self.metrics.items_per_second:.1f} items/s")

            if self.metrics.estimated_time_remaining > 0:
                eta = str(timedelta(seconds=int(self.metrics.estimated_time_remaining)))
                table.add_row("ETA:", eta)

            if self.metrics.failed_items > 0:
                table.add_row("Failed:", str(self.metrics.failed_items))

            if self.metrics.errors:
                table.add_row("Errors:", str(len(self.metrics.errors)))

        # Combine progress bar and table
        progress_group = [self.progress, table]

        return Panel(
            "\n".join(str(item) for item in progress_group),
            title=f"[bold blue]{self.operation_name}[/bold blue]",
            border_style="blue",
        )

    def _update_loop(self) -> None:
        """Background thread for updating display."""
        while not self._stop_event.is_set():
            try:
                if self.live:
                    self.live.update(self._create_progress_panel())

                time.sleep(self.update_interval)
            except (RuntimeError, ValueError, OSError) as e:
                self.logger.debug("Progress display update error: %s", e)

    def _save_to_file(self) -> None:
        """Save progress metrics to file."""
        try:
            output_data = {
                "operation_name": self.operation_name,
                "stage": self.metrics.stage.value,
                "total_items": self.metrics.total_items,
                "processed_items": self.metrics.processed_items,
                "failed_items": self.metrics.failed_items,
                "skipped_items": self.metrics.skipped_items,
                "completion_percentage": self.metrics.completion_percentage,
                "success_rate": self.metrics.success_rate,
                "elapsed_time": self.metrics.elapsed_time,
                "items_per_second": self.metrics.items_per_second,
                "errors": self.metrics.errors,
                "warnings": self.metrics.warnings,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }

            if self.output_file:
                out_path = Path(self.output_file)
                with out_path.open("w", encoding="utf-8") as f:
                    json.dump(output_data, f, indent=2)

        except (OSError, TypeError, ValueError) as e:
            self.logger.warning("Failed to save progress to file: %s", e)


class MultiStageProgressTracker:
    """Progress tracker for multi-stage operations."""

    def __init__(self, operation_name: str = "Multi-Stage Migration") -> None:
        """Initialize multi-stage progress tracker.

        Args:
            operation_name: Name of the overall operation

        """
        self.operation_name = operation_name
        self.logger = configure_logging("INFO", None)
        self.console = Console()

        # Stage tracking
        self.stages: dict[str, ProgressTracker] = {}
        self.stage_order: list[str] = []
        self.current_stage: str | None = None
        self.overall_start_time = time.time()

    def add_stage(
        self,
        stage_name: str,
        total_items: int,
        stage_description: str | None = None,
    ) -> ProgressTracker:
        """Add a stage to the multi-stage tracker.

        Args:
            stage_name: Unique name for the stage
            total_items: Total items for this stage
            stage_description: Optional description

        Returns:
            ProgressTracker for this stage

        """
        stage_tracker = ProgressTracker(
            operation_name=stage_description or stage_name,
            enable_console_output=False,  # We'll handle display centrally
            enable_logging=True,
        )

        self.stages[stage_name] = stage_tracker
        # Preserve total items for later start
        stage_tracker.metrics.total_items = total_items
        self.stage_order.append(stage_name)

        return stage_tracker

    def start_stage(self, stage_name: str) -> ProgressTracker:
        """Start a specific stage.

        Args:
            stage_name: Name of the stage to start

        Returns:
            ProgressTracker for the stage

        """
        if stage_name not in self.stages:
            msg = f"Stage '{stage_name}' not found"
            raise ValueError(msg)

        self.current_stage = stage_name
        tracker = self.stages[stage_name]

        # Initialize the stage
        tracker.start(tracker.metrics.total_items)

        self.logger.info("Starting stage: %s", stage_name)

        return tracker

    def get_overall_progress(self) -> dict[str, Any]:
        """Get overall progress across all stages.

        Returns:
            Dictionary with overall progress information

        """
        total_items = sum(
            tracker.metrics.total_items for tracker in self.stages.values()
        )
        processed_items = sum(
            tracker.metrics.processed_items for tracker in self.stages.values()
        )
        failed_items = sum(
            tracker.metrics.failed_items for tracker in self.stages.values()
        )

        overall_elapsed = time.time() - self.overall_start_time

        return {
            "total_items": total_items,
            "processed_items": processed_items,
            "failed_items": failed_items,
            "completion_percentage": (
                (processed_items / total_items * 100) if total_items > 0 else 0
            ),
            "elapsed_time": overall_elapsed,
            "current_stage": self.current_stage,
            "stages": {
                name: {
                    "completion_percentage": tracker.metrics.completion_percentage,
                    "stage": tracker.metrics.stage.value,
                    "items_per_second": tracker.metrics.items_per_second,
                }
                for name, tracker in self.stages.items()
            },
        }

    def finish(self) -> dict[str, Any]:
        """Finish multi-stage tracking and return summary.

        Returns:
            Summary of all stages

        """
        overall_progress = self.get_overall_progress()

        self.logger.info(
            "%s completed: %d/%d items in %.2fs",
            self.operation_name,
            overall_progress["processed_items"],
            overall_progress["total_items"],
            overall_progress["elapsed_time"],
        )

        return overall_progress


# Convenience functions for common progress tracking scenarios
T = TypeVar("T")

def track_migration_progress(  # noqa: UP047
    operation_name: str,
    total_items: int,
    processor_func: Callable[[Callable[[int, int, str | None], None]], T],
) -> T:
    """Track progress for a migration operation.

    Args:
        operation_name: Name of the operation
        total_items: Total number of items to process
        processor_func: Function that accepts an update callback

    Returns:
        Result from processor_func

    """
    tracker = ProgressTracker(operation_name)
    tracker.start(total_items)

    def update_callback(
        processed: int = 1,
        failed: int = 0,
        message: str | None = None,
    ) -> None:
        tracker.update(processed=processed, failed=failed, message=message)

    try:
        result = processor_func(update_callback)
        tracker.finish(success=True)
    except Exception as e:
        tracker.finish(success=False, final_message=str(e))
        raise
    else:
        return result


def create_batch_progress_callback(
    tracker: ProgressTracker,
) -> Callable[[int, int, int], None]:
    """Create a progress callback for batch processing.

    Args:
        tracker: ProgressTracker instance

    Returns:
        Callback function for batch processing

    """

    def callback(processed: int, total: int, failed: int) -> None:
        # Calculate new items processed since last update
        new_processed = processed - tracker.metrics.processed_items
        new_failed = failed - tracker.metrics.failed_items

        tracker.update(
            processed=new_processed,
            failed=new_failed,
            message=f"Processing batch {processed}/{total}",
        )

    return callback

