"""
Centralized display utilities for console output and progress tracking.
Provides standardized progress bars and logging displays using rich.
"""

import time
import logging
import os
from typing import List, Dict, Any, Optional, Callable, TypeVar, Iterable, Generic
from collections import deque
from rich.console import Console
from rich.progress import Progress, TaskID, TextColumn, BarColumn, SpinnerColumn
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.logging import RichHandler
from rich.theme import Theme

# Type variables for generic functions
T = TypeVar("T")
U = TypeVar("U")

# Create a custom theme for logging
LOGGING_THEME = Theme({
    "logging.level.debug": "dim",
    "logging.level.info": "blue",
    "logging.level.notice": "cyan",  # New NOTICE level styling
    "logging.level.warning": "bold yellow",
    "logging.level.error": "bold red",
    "logging.level.critical": "bold red on white",
    "success": "bold green"
})

# Global console instance with theme
console = Console(theme=LOGGING_THEME)

# Set up a rich handler for logging
rich_handler = RichHandler(
    console=console,
    rich_tracebacks=True,
    tracebacks_show_locals=False,
    markup=True,
    show_time=True,
    show_level=True,
    enable_link_path=True,
    log_time_format="[%X.%f]"
)

def configure_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """
    Configure logging with rich formatting.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional path to a log file

    Returns:
        Configured logger instance
    """
    # Define custom log levels first
    # Create a special success level (between INFO and WARNING)
    logging.addLevelName(25, "SUCCESS")

    # Create a NOTICE level (between INFO and DEBUG)
    logging.addLevelName(21, "NOTICE")

    # Get the numeric logging level - now we support our custom levels too
    if level.upper() == "NOTICE":
        numeric_level = 21
    elif level.upper() == "SUCCESS":
        numeric_level = 25
    else:
        numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Create handlers list starting with the rich handler
    handlers = [rich_handler]

    # Add a file handler if a log file path is provided
    if log_file:
        # Create directory for log file if it doesn't exist
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # Create a file handler with a more detailed format for the log file
        file_format = logging.Formatter(
            "%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(file_format)
        file_handler.setLevel(numeric_level)
        handlers.append(file_handler)

    # Set up basic configuration
    logging.basicConfig(
        level=numeric_level,
        format="%(message)s",
        datefmt="[%X.%f]",
        handlers=handlers,
        force=True  # Ensure we can reconfigure logging if needed
    )

    # Get the logger and report configuration
    logger = logging.getLogger("migration")

    # Define a success method for the logger
    def success(self, message, *args, **kwargs):
        if self.isEnabledFor(25):
            kwargs["extra"] = kwargs.get("extra", {})
            kwargs["extra"]["markup"] = True
            self._log(25, f"[success]{message}[/]", args, **kwargs)

    # Define a notice method for the logger (less prominent than INFO)
    def notice(self, message, *args, **kwargs):
        if self.isEnabledFor(21):
            kwargs["extra"] = kwargs.get("extra", {})
            kwargs["extra"]["markup"] = True
            self._log(21, message, args, **kwargs)

    # Add the success method to the logger class
    logging.Logger.success = success

    # Add the notice method to the logger class
    logging.Logger.notice = notice

    logger.info("Rich logging configured")
    if log_file:
        logger.info(f"Log file: {log_file}")

    return logger

class ProgressTracker(Generic[T]):
    """
    Centralized progress tracker that provides standardized rich progress bars
    with a rolling log of recent items below the progress bar.
    """

    def __init__(
        self,
        description: str,
        total: int,
        log_title: str = "Recent Items",
        max_log_items: int = 5
    ):
        """
        Initialize a progress tracker with a progress bar and rolling log.

        Args:
            description: Initial description for the progress bar
            total: Total number of items to process
            log_title: Title for the rolling log panel
            max_log_items: Maximum number of items to show in the rolling log
        """
        self.description = description
        self.total = total
        self.log_title = log_title
        self.progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
            console=console
        )
        self.task_id = self.progress.add_task(description, total=total)
        self.recent_items = deque(maxlen=max_log_items)
        self.processed_count = 0
        self.log_panel = None
        self.live = None

    def __enter__(self):
        """Start the live display when entering context."""
        self.live = Live(console=console, refresh_per_second=4)
        self.live.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close the live display when exiting context."""
        if self.live:
            self.live.__exit__(exc_type, exc_val, exc_tb)

    def update_description(self, description: str):
        """Update the progress bar description."""
        self.progress.update(self.task_id, description=description)

    def add_log_item(self, item: str):
        """
        Add an item to the rolling log.

        Args:
            item: Item text to add to the log
        """
        self.recent_items.append(item)
        self._update_display()

    def increment(self, advance: int = 1, description: Optional[str] = None):
        """
        Increment the progress bar.

        Args:
            advance: Number of steps to advance
            description: New description (optional)
        """
        self.processed_count += advance
        if description:
            self.progress.update(self.task_id, completed=self.processed_count, description=description)
        else:
            self.progress.update(self.task_id, completed=self.processed_count)
        self._update_display()

    def _update_display(self):
        """Update the live display with current progress and log."""
        if not self.live:
            return

        # Create the rolling log table
        log_table = Table.grid(padding=(0, 1))
        log_table.add_column()
        log_table.add_row(Text(f"{self.log_title}:", style="bold yellow"))

        # Add the recent items to the table
        for item in self.recent_items:
            log_table.add_row(f"  - {item}")

        # Create a panel with the log table
        panel = Panel.fit(log_table, title=self.log_title)

        # Update the live display with progress and panel
        self.live.update(self.progress)
        if self.recent_items:
            self.live.update(Panel.fit(
                self._create_combined_display(self.progress, log_table),
                title=self.description
            ))
        else:
            self.live.update(self.progress)

    def _create_combined_display(self, progress, log_table):
        """Create a combined display with progress and log table."""
        combined = Table.grid(padding=1)
        combined.add_column()
        combined.add_row(progress)
        combined.add_row(log_table)
        return combined

    def track(self, iterable: Iterable[T]) -> Iterable[T]:
        """
        Track progress through an iterable.

        Args:
            iterable: The iterable to track

        Yields:
            Items from the iterable with progress tracking
        """
        for item in iterable:
            yield item
            self.increment()
            # Small delay to make the display visible
            time.sleep(0.05)


def process_with_progress(
    items: List[T],
    process_func: Callable[[T, Dict[str, Any]], U],
    description: str,
    log_title: str = "Recent Items",
    context: Dict[str, Any] = None,
    item_name_func: Callable[[T], str] = None
) -> List[U]:
    """
    Process a list of items with a progress bar and rolling log.

    Args:
        items: List of items to process
        process_func: Function to process each item, taking the item and context
        description: Description for the progress bar
        log_title: Title for the rolling log
        context: Additional context to pass to the process function
        item_name_func: Function to extract a name from each item for the log

    Returns:
        List of processed results
    """
    if context is None:
        context = {}

    if item_name_func is None:
        item_name_func = lambda x: str(x)

    results = []

    with ProgressTracker(description, len(items), log_title) as tracker:
        for item in items:
            # Update progress description with current item
            item_name = item_name_func(item)
            tracker.update_description(f"{description}: {item_name[:20]}")

            # Process the item
            result = process_func(item, context)
            results.append(result)

            # Add to log and increment progress
            tracker.add_log_item(item_name)
            tracker.increment()

    return results
