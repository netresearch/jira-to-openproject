#!/usr/bin/env python3
from src.display import configure_logging

"""Timezone Correction Script for Previously Migrated Timestamps.

This script scans previously migrated timestamps and corrects their timezone
metadata using the fixed timezone detection logic from EnhancedTimestampMigrator.

Usage:
    python scripts/fix_timestamp_timezones.py --dry-run
    python scripts/fix_timestamp_timezones.py --apply
    python scripts/fix_timestamp_timezones.py --batch-size 100 --apply
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Add src directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.clients.jira_client import JiraClient  # noqa: E402
from src.clients.openproject_client import OpenProjectClient  # noqa: E402
from src.utils.enhanced_timestamp_migrator import (  # noqa: E402
    EnhancedTimestampMigrator,
)

# Add config attribute for tests
config = type('Config', (), {
    'logger': configure_logging("INFO", None)
})()


class TimestampCorrectionScript:
    """Corrects timezone metadata for previously migrated timestamps."""

    def __init__(
        self,
        dry_run: bool = True,
        batch_size: int = 50,
        custom_field_id: str = "customField29",
        page_size: int = 100,
    ) -> None:
        """Initialize the correction script.

        Args:
            dry_run: If True, only report changes without applying them
            batch_size: Number of work packages to process in each batch
            custom_field_id: Custom field ID for Jira key references
            page_size: Page size for API requests (will paginate through all)
        """
        self.dry_run = dry_run
        self.batch_size = batch_size
        self.custom_field_id = custom_field_id
        self.page_size = page_size
        self.logger = configure_logging("INFO", None)

        # Initialize clients
        self.jira_client = JiraClient()
        self.op_client = OpenProjectClient()

        # Initialize enhanced timestamp migrator to get correct timezone detection
        self.timestamp_migrator = EnhancedTimestampMigrator(
            jira_client=self.jira_client, op_client=self.op_client
        )

        # Detect correct Jira timezone
        self.correct_timezone = self.timestamp_migrator.jira_timezone
        self.logger.info("Detected Jira timezone: %s", self.correct_timezone)

        # Statistics
        self.stats = {
            "total_packages": 0,
            "packages_with_timestamps": 0,
            "timestamps_corrected": 0,
            "errors": 0,
        }

    def run(self) -> None:
        """Run the correction script."""
        try:
            self.logger.info("Starting timestamp timezone correction script")
            self.logger.info("Mode: %s", "DRY RUN" if self.dry_run else "APPLY CHANGES")
            self.logger.info("Batch size: %d", self.batch_size)

            # Process work packages in streaming batches to control memory usage
            total_processed = 0
            batch_num = 1

            for batch in self._get_migrated_work_packages_streaming():
                if not batch:
                    break

                self.stats["total_packages"] += len(batch)
                total_processed += len(batch)

                self.logger.info(
                    "Processing batch %d (%d packages, %d total so far)",
                    batch_num,
                    len(batch),
                    total_processed,
                )

                self._process_batch(batch)
                batch_num += 1

            if total_processed == 0:
                self.logger.info("No migrated work packages found")
                return

            self.logger.info("Processed %d total work packages", total_processed)

            self._print_final_report()

        except Exception as e:
            self.logger.error("Correction script failed: %s", e)
            raise

    def _get_migrated_work_packages_streaming(self):
        """Stream work packages that were previously migrated in batches.

        Yields:
            List of work package data dictionaries (batch size)
        """
        offset = 0

        while True:
            try:
                # Get work packages from OpenProject that have Jira references
                params = {
                    "filters": json.dumps(
                        [
                            {
                                self.custom_field_id: {  # Configurable custom field for Jira key
                                    "operator": "!*",  # Not empty
                                    "values": [],
                                }
                            }
                        ]
                    ),
                    "pageSize": self.page_size,
                    "offset": offset,
                }

                response = self.op_client.get("/api/v3/work_packages", params=params)

                elements = response.get("_embedded", {}).get("elements", [])

                if not elements:
                    # No more results
                    break

                yield elements

                # Check if there are more pages
                if len(elements) < self.page_size:
                    # Last page (partial or complete)
                    break

                offset += self.page_size

            except Exception as e:
                self.logger.error(
                    "Failed to fetch work packages at offset %d: %s", offset, e
                )
                break

    def _process_batch(self, work_packages: list[dict[str, Any]]) -> None:
        """Process a batch of work packages.

        Args:
            work_packages: List of work package data
        """
        for wp in work_packages:
            try:
                self._process_work_package(wp)
            except Exception as e:
                self.logger.error(
                    "Failed to process work package %s: %s", wp.get("id", "unknown"), e
                )
                self.stats["errors"] += 1

    def _process_work_package(self, work_package: dict[str, Any]) -> None:
        """Process a single work package to correct timestamp timezones.

        Args:
            work_package: Work package data
        """
        wp_id = work_package.get("id")
        if not wp_id:
            return

        # Check if this work package has timestamp fields that need correction
        corrections_needed = []

        # Check standard timestamp fields
        timestamp_fields = ["createdAt", "updatedAt", "dueDate"]

        for field in timestamp_fields:
            if field in work_package:
                timestamp_str = work_package[field]
                if timestamp_str and self._needs_timezone_correction(timestamp_str):
                    corrections_needed.append(
                        {
                            "field": field,
                            "current_value": timestamp_str,
                            "corrected_value": self._correct_timestamp(timestamp_str),
                        }
                    )

        # Check custom date fields
        custom_fields = work_package.get("customFields", {})
        for field_id, field_data in custom_fields.items():
            if isinstance(field_data, dict) and "value" in field_data:
                timestamp_str = field_data["value"]
                if (
                    timestamp_str
                    and self._is_timestamp_field(field_data)
                    and self._needs_timezone_correction(timestamp_str)
                ):
                    corrections_needed.append(
                        {
                            "field": f"customField{field_id}",
                            "current_value": timestamp_str,
                            "corrected_value": self._correct_timestamp(timestamp_str),
                        }
                    )

        if corrections_needed:
            self.stats["packages_with_timestamps"] += 1
            self.stats["timestamps_corrected"] += len(corrections_needed)

            if self.dry_run:
                self._log_corrections(wp_id, corrections_needed)
            else:
                self._apply_corrections(wp_id, corrections_needed)

    def _needs_timezone_correction(self, timestamp_str: str) -> bool:
        """Check if a timestamp needs timezone correction.

        Args:
            timestamp_str: Timestamp string to check

        Returns:
            bool: True if correction is needed
        """
        if not timestamp_str:
            return False

        try:
            # Parse the timestamp
            if timestamp_str.endswith("Z"):
                # UTC timestamp - might need timezone correction if original was not UTC
                dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                return dt.tzinfo == timezone.utc and self.correct_timezone != "UTC"
            elif "+" in timestamp_str or timestamp_str.endswith(
                tuple(f"{h:02d}:00" for h in range(24))
            ):
                # Already has timezone info - check if it's correct
                dt = datetime.fromisoformat(timestamp_str)
                if dt.tzinfo:
                    current_tz = str(dt.tzinfo)
                    return current_tz != self.correct_timezone
            else:
                # Naive timestamp - needs timezone info
                return True

        except Exception:
            # If we can't parse it, assume it needs correction
            return True

        return False

    def _correct_timestamp(self, timestamp_str: str) -> str:
        """Correct a timestamp's timezone.

        Args:
            timestamp_str: Original timestamp string

        Returns:
            str: Corrected timestamp string
        """
        try:
            # Parse original timestamp
            if timestamp_str.endswith("Z"):
                dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(timestamp_str)

            # If it's naive, assume it was in Jira's timezone
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo(self.correct_timezone))

            # Convert to correct timezone if needed
            if str(dt.tzinfo) != self.correct_timezone:
                correct_tz = ZoneInfo(self.correct_timezone)
                dt = dt.astimezone(correct_tz)

            return dt.isoformat()

        except Exception as e:
            self.logger.warning("Failed to correct timestamp %s: %s", timestamp_str, e)
            return timestamp_str

    def _is_timestamp_field(self, field_data: dict[str, Any]) -> bool:
        """Check if a custom field contains timestamp data.

        Args:
            field_data: Custom field data

        Returns:
            bool: True if it's a timestamp field
        """
        # Check field type or value format
        field_type = field_data.get("type", "")
        value = field_data.get("value", "")

        if "date" in field_type.lower() or "time" in field_type.lower():
            return True

        # Check if value looks like a timestamp
        if isinstance(value, str) and value:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                return True
            except Exception:
                pass

        return False

    def _log_corrections(self, wp_id: str, corrections: list[dict[str, Any]]) -> None:
        """Log corrections that would be made (dry run mode).

        Args:
            wp_id: Work package ID
            corrections: List of corrections to be made
        """
        self.logger.info(
            "Work package %s - %d timestamp corrections needed:",
            wp_id,
            len(corrections),
        )

        for correction in corrections:
            self.logger.info(
                "  %s: %s -> %s",
                correction["field"],
                correction["current_value"],
                correction["corrected_value"],
            )

    def _apply_corrections(self, wp_id: str, corrections: list[dict[str, Any]]) -> None:
        """Apply timestamp corrections to a work package.

        Args:
            wp_id: Work package ID
            corrections: List of corrections to apply
        """
        try:
            # Build update payload
            update_data = {}

            for correction in corrections:
                field = correction["field"]
                corrected_value = correction["corrected_value"]

                if field.startswith("customField"):
                    # Custom field update
                    field_id = field.replace("customField", "")
                    if "customFields" not in update_data:
                        update_data["customFields"] = {}
                    update_data["customFields"][field_id] = {"value": corrected_value}
                else:
                    # Standard field update
                    update_data[field] = corrected_value

            # Apply the update
            response = self.op_client.patch(
                f"/api/v3/work_packages/{wp_id}", data=update_data
            )

            if response:
                self.logger.info(
                    "Successfully corrected %d timestamps for work package %s",
                    len(corrections),
                    wp_id,
                )
            else:
                self.logger.error("Failed to update work package %s", wp_id)
                self.stats["errors"] += 1

        except Exception as e:
            self.logger.error(
                "Failed to apply corrections to work package %s: %s", wp_id, e
            )
            self.stats["errors"] += 1

    def _print_final_report(self) -> None:
        """Print final statistics report."""
        self.logger.info("=== Timestamp Timezone Correction Report ===")
        self.logger.info("Mode: %s", "DRY RUN" if self.dry_run else "CHANGES APPLIED")
        self.logger.info(
            "Total work packages processed: %d", self.stats["total_packages"]
        )
        self.logger.info(
            "Work packages with timestamps: %d", self.stats["packages_with_timestamps"]
        )
        self.logger.info("Timestamps corrected: %d", self.stats["timestamps_corrected"])
        self.logger.info("Errors encountered: %d", self.stats["errors"])
        self.logger.info("Jira timezone used: %s", self.correct_timezone)

        if self.dry_run:
            self.logger.info("\nTo apply these changes, run with --apply flag")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Correct timezone metadata for previously migrated timestamps"
    )

    # Create mutually exclusive group for dry-run vs apply
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Show what would be changed without applying changes (default)",
    )
    action_group.add_argument(
        "--apply", action="store_true", help="Apply the corrections"
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of work packages to process in each batch (default: 50)",
    )
    parser.add_argument(
        "--custom-field-id",
        type=str,
        default="customField29",
        help="Custom field ID for Jira key references (default: customField29)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Page size for API requests (default: 100)",
    )

    args = parser.parse_args()

    # Determine dry-run mode
    dry_run = not args.apply

    try:
        script = TimestampCorrectionScript(
            dry_run=dry_run,
            batch_size=args.batch_size,
            custom_field_id=args.custom_field_id,
            page_size=args.page_size,
        )
        script.run()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Script failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
