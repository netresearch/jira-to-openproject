#!/usr/bin/env python3
"""Bulk update OpenProject work package metadata from Jira data.

This script corrects priority, author, assignee, and timestamps for all
work packages based on the original Jira issue metadata.

Prerequisites:
- Run scripts/cache_jira_metadata.py first to cache Jira metadata
- Existing mapping files in var/data/

Usage:
    python scripts/bulk_update_wp_metadata.py [--batch-size 500] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.display import configure_logging
from src.infrastructure.openproject.openproject_client import OpenProjectClient

logger = configure_logging("INFO", None)


def load_json(path: Path) -> dict[str, Any]:
    """Load JSON file."""
    if not path.exists():
        logger.error("File not found: %s", path)
        return {}
    with open(path) as f:
        return json.load(f)


def build_lookup_tables(data_dir: Path) -> dict[str, dict]:
    """Build lookup tables from mapping files."""
    # Work package mapping: jira_id -> {jira_key, openproject_id, project_key}
    wp_mapping = load_json(data_dir / "work_package_mapping.json")

    # User mapping: jira_username -> {openproject_id, ...}
    user_mapping = load_json(data_dir / "user_mapping.json")

    # Priority mapping: jira_priority_name -> openproject_priority_id
    priority_mapping = load_json(data_dir / "priority_mapping.json")

    # Jira metadata cache: jira_key -> {priority, assignee, reporter, created, updated}
    jira_metadata = load_json(data_dir / "jira_issue_metadata.json")

    # Build reverse mapping: jira_key -> openproject_id
    jira_key_to_op_id = {}
    for jira_id, entry in wp_mapping.items():
        jira_key = entry.get("jira_key")
        op_id = entry.get("openproject_id")
        if jira_key and op_id:
            jira_key_to_op_id[jira_key] = op_id

    logger.info("Loaded %d work package mappings", len(wp_mapping))
    logger.info("Loaded %d user mappings", len(user_mapping))
    logger.info("Loaded %d priority mappings", len(priority_mapping))
    logger.info("Loaded %d Jira metadata entries", len(jira_metadata))
    logger.info("Built %d jira_key -> op_id mappings", len(jira_key_to_op_id))

    return {
        "wp_mapping": wp_mapping,
        "user_mapping": user_mapping,
        "priority_mapping": priority_mapping,
        "jira_metadata": jira_metadata,
        "jira_key_to_op_id": jira_key_to_op_id,
    }


def prepare_update_batch(
    lookups: dict[str, dict],
    jira_keys: list[str],
) -> list[dict]:
    """Prepare update data for a batch of Jira keys."""
    updates = []
    user_mapping = lookups["user_mapping"]
    priority_mapping = lookups["priority_mapping"]
    jira_metadata = lookups["jira_metadata"]
    jira_key_to_op_id = lookups["jira_key_to_op_id"]

    for jira_key in jira_keys:
        if jira_key not in jira_key_to_op_id:
            continue
        if jira_key not in jira_metadata:
            continue

        op_id = jira_key_to_op_id[jira_key]
        meta = jira_metadata[jira_key]

        update = {
            "op_id": op_id,
            "jira_key": jira_key,
            "fields": {},
        }

        # Priority
        jira_priority = meta.get("priority")
        if jira_priority and jira_priority in priority_mapping:
            update["fields"]["priority_id"] = priority_mapping[jira_priority]

        # Author (from Jira reporter)
        jira_reporter = meta.get("reporter")
        if jira_reporter and jira_reporter in user_mapping:
            reporter_entry = user_mapping[jira_reporter]
            if reporter_entry.get("openproject_id"):
                update["fields"]["author_id"] = reporter_entry["openproject_id"]

        # Assignee
        jira_assignee = meta.get("assignee")
        if jira_assignee and jira_assignee in user_mapping:
            assignee_entry = user_mapping[jira_assignee]
            if assignee_entry.get("openproject_id"):
                update["fields"]["assigned_to_id"] = assignee_entry["openproject_id"]

        # Timestamps
        if meta.get("created"):
            update["fields"]["created_at"] = meta["created"]
        if meta.get("updated"):
            update["fields"]["updated_at"] = meta["updated"]

        if update["fields"]:
            updates.append(update)

    return updates


RUBY_BATCH_UPDATE_TEMPLATE = """
require 'json'
require 'time'

results = {updated: 0, skipped: 0, errors: []}

ActiveRecord::Base.transaction do
  input_data.each do |upd|
    op_id = upd['op_id']
    jira_key = upd['jira_key']
    fields = upd['fields']

    begin
      wp = WorkPackage.find_by(id: op_id)
      unless wp
        results[:skipped] += 1
        next
      end

      attrs = {}

      # Priority
      if fields['priority_id']
        attrs[:priority_id] = fields['priority_id']
      end

      # Author (set directly, bypass validations)
      if fields['author_id']
        attrs[:author_id] = fields['author_id']
      end

      # Assignee
      if fields['assigned_to_id']
        attrs[:assigned_to_id] = fields['assigned_to_id']
      end

      # Timestamps - parse ISO 8601 format
      if fields['created_at']
        attrs[:created_at] = Time.parse(fields['created_at'])
      end
      if fields['updated_at']
        attrs[:updated_at] = Time.parse(fields['updated_at'])
      end

      if attrs.any?
        # Use update_columns to bypass validations and callbacks
        # This is safe because we're just fixing metadata, not changing business logic
        wp.update_columns(attrs)
        results[:updated] += 1
      else
        results[:skipped] += 1
      end

    rescue => e
      results[:errors] << {op_id: op_id, jira_key: jira_key, error: e.message}
    end
  end
end

puts "JSON_OUTPUT_START"
puts results.to_json
puts "JSON_OUTPUT_END"
"""


def run_bulk_update(
    op_client: OpenProjectClient,
    lookups: dict[str, dict],
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict:
    """Run bulk update of work package metadata."""
    jira_metadata = lookups["jira_metadata"]
    jira_key_to_op_id = lookups["jira_key_to_op_id"]

    # Get all Jira keys that have both metadata and OP mapping
    all_jira_keys = [k for k in jira_metadata.keys() if k in jira_key_to_op_id]
    logger.info("Found %d work packages to update", len(all_jira_keys))

    total_updated = 0
    total_skipped = 0
    total_errors = []
    total_batches = (len(all_jira_keys) + batch_size - 1) // batch_size

    for i in range(0, len(all_jira_keys), batch_size):
        batch_num = i // batch_size + 1
        batch_keys = all_jira_keys[i : i + batch_size]

        logger.info(
            "Processing batch %d/%d (%d work packages)...",
            batch_num,
            total_batches,
            len(batch_keys),
        )

        # Prepare update data
        updates = prepare_update_batch(lookups, batch_keys)
        if not updates:
            logger.info("  No updates for this batch")
            continue

        if dry_run:
            logger.info("  [DRY RUN] Would update %d work packages", len(updates))
            total_updated += len(updates)
            continue

        # Execute Ruby code with data
        try:
            result = op_client.execute_script_with_data(
                RUBY_BATCH_UPDATE_TEMPLATE,
                updates,
                timeout=120,
            )
            if result.get("status") == "success" and result.get("data"):
                result_data = result["data"]
                total_updated += result_data.get("updated", 0)
                total_skipped += result_data.get("skipped", 0)
                if result_data.get("errors"):
                    total_errors.extend(result_data["errors"])
                logger.info(
                    "  Batch result: updated=%d, skipped=%d, errors=%d",
                    result_data.get("updated", 0),
                    result_data.get("skipped", 0),
                    len(result_data.get("errors", [])),
                )
            else:
                logger.warning("  Batch returned non-success: %s", result.get("message", "unknown"))
                total_errors.append({"batch": batch_num, "error": result.get("message", "unknown")})
        except Exception as e:
            logger.error("  Error executing batch: %s", e)
            total_errors.append({"batch": batch_num, "error": str(e)})

        # Progress update
        progress_pct = 100 * (i + len(batch_keys)) / len(all_jira_keys)
        logger.info(
            "  Progress: %d/%d (%.1f%%)",
            i + len(batch_keys),
            len(all_jira_keys),
            progress_pct,
        )

    return {
        "total_processed": len(all_jira_keys),
        "updated": total_updated,
        "skipped": total_skipped,
        "errors": total_errors,
    }


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Bulk update OpenProject work package metadata from Jira",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("var/data"),
        help="Path to migration data directory",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of work packages per batch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without making changes",
    )
    args = parser.parse_args()

    # Check for Jira metadata cache
    metadata_path = args.data_dir / "jira_issue_metadata.json"
    if not metadata_path.exists():
        logger.error("Jira metadata cache not found. Run cache_jira_metadata.py first.")
        sys.exit(1)

    # Load lookup tables
    logger.info("Loading mapping files...")
    lookups = build_lookup_tables(args.data_dir)

    if not lookups["jira_metadata"]:
        logger.error("No Jira metadata available")
        sys.exit(1)

    # Initialize OpenProject client
    logger.info("Connecting to OpenProject...")
    op_client = OpenProjectClient()

    # Run bulk update
    logger.info("=" * 60)
    logger.info("STARTING BULK METADATA UPDATE")
    if args.dry_run:
        logger.info("DRY RUN MODE - No changes will be made")
    logger.info("=" * 60)

    result = run_bulk_update(
        op_client=op_client,
        lookups=lookups,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

    # Summary
    logger.info("=" * 60)
    logger.info("BULK UPDATE COMPLETE")
    logger.info("=" * 60)
    logger.info("Total processed: %d", result["total_processed"])
    logger.info("Updated: %d", result["updated"])
    logger.info("Skipped: %d", result["skipped"])
    logger.info("Errors: %d", len(result["errors"]))

    if result["errors"]:
        logger.warning("First 10 errors:")
        for err in result["errors"][:10]:
            logger.warning("  %s", err)

    # Save results
    results_path = args.data_dir / "bulk_update_results.json"
    with open(results_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    logger.info("Results saved to: %s", results_path)


if __name__ == "__main__":
    main()
