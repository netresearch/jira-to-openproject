"""Enhanced progress tracking system for migration operations.

This module provides comprehensive progress tracking with real-time reporting,
ETA calculations, and multiple output formats for long-running migration tasks.
"""

import time
import threading
from typing import Dict, List, Optional, Callable, Any, Union
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import json

from rich.console import Console
from rich.progress import (
    Progress, 
    TaskID, 
    SpinnerColumn, 
    TextColumn, 
    BarColumn, 
    TaskProgressColumn,
    TimeRemainingColumn,
    TimeElapsedColumn,
    MofNCompleteColumn
)
from rich.live import Live
from rich.table import Table
from rich.panel import Panel

from src import config


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
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
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
        enable_console_output: bool = True,
        enable_logging: bool = True,
        enable_file_output: bool = False,
        output_file: Optional[str] = None,
        update_interval: float = 0.5,
    ):
        """Initialize the progress tracker.
        
        Args:
            operation_name: Name of the operation being tracked
            enable_console_output: Show progress in console
            enable_logging: Log progress to logger
            enable_file_output: Save progress to file
            output_file: File path for progress output
            update_interval: Minimum time between updates
        """
        self.operation_name = operation_name
        self.enable_console_output = enable_console_output
        self.enable_logging = enable_logging
        self.enable_file_output = enable_file_output
        self.output_file = output_file
        self.update_interval = update_interval
        
        self.logger = config.logger
        self.console = Console()
        
        # Progress tracking
        self.metrics = ProgressMetrics()
        self.callbacks: List[Callable[[ProgressMetrics], None]] = []
        
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
        
    def start(self, total_items: int, stage: ProgressStage = ProgressStage.PROCESSING) -> None:
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
                last_update_time=time.time()
            )
            
        if self.enable_console_output:
            self._start_rich_progress()
            
        if self.enable_logging:
            self.logger.info(
                f"Starting {self.operation_name}: {total_items} items to process"
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
        stage: Optional[ProgressStage] = None,
        message: Optional[str] = None,
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
                remaining_items = self.metrics.total_items - self.metrics.processed_items
                if self.metrics.items_per_second > 0:
                    self.metrics.estimated_time_remaining = remaining_items / self.metrics.items_per_second
                    
            self.metrics.last_update_time = current_time
            
        # Update rich progress
        if self.progress and self.task_id:
            self.progress.update(
                self.task_id,
                completed=self.metrics.processed_items,
                description=message or f"{self.operation_name} - {self.metrics.stage.value}"
            )
            
        # Trigger callbacks
        for callback in self.callbacks:
            try:
                callback(self.metrics)
            except Exception as e:
                self.logger.warning(f"Progress callback error: {e}")
                
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
            
    def finish(self, success: bool = True, final_message: Optional[str] = None) -> ProgressMetrics:
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
            self.metrics.stage = ProgressStage.COMPLETED if success else ProgressStage.FAILED
            
        # Final updates
        if self.progress and self.task_id:
            if success:
                self.progress.update(
                    self.task_id,
                    completed=self.metrics.total_items,
                    description=final_message or f"{self.operation_name} - Completed"
                )
            else:
                self.progress.update(
                    self.task_id,
                    description=final_message or f"{self.operation_name} - Failed"
                )
                
        # Stop rich progress
        if self.live:
            self.live.stop()
            
        # Final logging
        if self.enable_logging:
            elapsed = self.metrics.elapsed_time
            if success:
                self.logger.info(
                    f"{self.operation_name} completed: "
                    f"{self.metrics.processed_items}/{self.metrics.total_items} items "
                    f"in {elapsed:.2f}s ({self.metrics.items_per_second:.1f} items/s)"
                )
            else:
                self.logger.error(
                    f"{self.operation_name} failed after {elapsed:.2f}s: "
                    f"{self.metrics.processed_items}/{self.metrics.total_items} items processed"
                )
                
        # Save to file if enabled
        if self.enable_file_output and self.output_file:
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
            total=self.metrics.total_items
        )
        
        # Create live display with additional info
        self.live = Live(
            self._create_progress_panel(),
            console=self.console,
            refresh_per_second=2,
            auto_refresh=True
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
            table.add_row("Progress:", f"{self.metrics.processed_items}/{self.metrics.total_items}")
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
            border_style="blue"
        )
        
    def _update_loop(self) -> None:
        """Background thread for updating display."""
        while not self._stop_event.is_set():
            try:
                if self.live:
                    self.live.update(self._create_progress_panel())
                    
                time.sleep(self.update_interval)
            except Exception as e:
                self.logger.debug(f"Progress display update error: {e}")
                
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
                "timestamp": datetime.now().isoformat(),
            }
            
            with open(self.output_file, 'w') as f:
                json.dump(output_data, f, indent=2)
                
        except Exception as e:
            self.logger.warning(f"Failed to save progress to file: {e}")


class MultiStageProgressTracker:
    """Progress tracker for multi-stage operations."""
    
    def __init__(self, operation_name: str = "Multi-Stage Migration"):
        """Initialize multi-stage progress tracker.
        
        Args:
            operation_name: Name of the overall operation
        """
        self.operation_name = operation_name
        self.logger = config.logger
        self.console = Console()
        
        # Stage tracking
        self.stages: Dict[str, ProgressTracker] = {}
        self.stage_order: List[str] = []
        self.current_stage: Optional[str] = None
        self.overall_start_time = time.time()
        
    def add_stage(
        self, 
        stage_name: str, 
        total_items: int,
        stage_description: Optional[str] = None
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
            enable_logging=True
        )
        
        self.stages[stage_name] = stage_tracker
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
            raise ValueError(f"Stage '{stage_name}' not found")
            
        self.current_stage = stage_name
        tracker = self.stages[stage_name]
        
        # Initialize the stage
        tracker.start(tracker.metrics.total_items)
        
        self.logger.info(f"Starting stage: {stage_name}")
        
        return tracker
        
    def get_overall_progress(self) -> Dict[str, Any]:
        """Get overall progress across all stages.
        
        Returns:
            Dictionary with overall progress information
        """
        total_items = sum(tracker.metrics.total_items for tracker in self.stages.values())
        processed_items = sum(tracker.metrics.processed_items for tracker in self.stages.values())
        failed_items = sum(tracker.metrics.failed_items for tracker in self.stages.values())
        
        overall_elapsed = time.time() - self.overall_start_time
        
        return {
            "total_items": total_items,
            "processed_items": processed_items,
            "failed_items": failed_items,
            "completion_percentage": (processed_items / total_items * 100) if total_items > 0 else 0,
            "elapsed_time": overall_elapsed,
            "current_stage": self.current_stage,
            "stages": {
                name: {
                    "completion_percentage": tracker.metrics.completion_percentage,
                    "stage": tracker.metrics.stage.value,
                    "items_per_second": tracker.metrics.items_per_second,
                }
                for name, tracker in self.stages.items()
            }
        }
        
    def finish(self) -> Dict[str, Any]:
        """Finish multi-stage tracking and return summary.
        
        Returns:
            Summary of all stages
        """
        overall_progress = self.get_overall_progress()
        
        self.logger.info(
            f"{self.operation_name} completed: "
            f"{overall_progress['processed_items']}/{overall_progress['total_items']} items "
            f"in {overall_progress['elapsed_time']:.2f}s"
        )
        
        return overall_progress


# Convenience functions for common progress tracking scenarios
def track_migration_progress(
    operation_name: str,
    total_items: int,
    processor_func: Callable[[Callable], Any],
) -> Any:
    """Convenience function to track progress for a migration operation.
    
    Args:
        operation_name: Name of the operation
        total_items: Total number of items to process
        processor_func: Function that accepts an update callback
        
    Returns:
        Result from processor_func
    """
    tracker = ProgressTracker(operation_name)
    tracker.start(total_items)
    
    def update_callback(processed: int = 1, failed: int = 0, message: str = None):
        tracker.update(processed=processed, failed=failed, message=message)
    
    try:
        result = processor_func(update_callback)
        tracker.finish(success=True)
        return result
    except Exception as e:
        tracker.finish(success=False, final_message=str(e))
        raise


def create_batch_progress_callback(tracker: ProgressTracker) -> Callable[[int, int, int], None]:
    """Create a progress callback for batch processing.
    
    Args:
        tracker: ProgressTracker instance
        
    Returns:
        Callback function for batch processing
    """
    def callback(processed: int, total: int, failed: int):
        # Calculate new items processed since last update
        new_processed = processed - tracker.metrics.processed_items
        new_failed = failed - tracker.metrics.failed_items
        
        tracker.update(
            processed=new_processed,
            failed=new_failed,
            message=f"Processing batch {processed}/{total}"
        )
    
    return callback 