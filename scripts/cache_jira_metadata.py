#!/usr/bin/env python3
"""Cache Jira issue metadata for bulk update of OpenProject work packages.

This script fetches priority, assignee, reporter, created, and updated fields
from all Jira issues that were migrated, saving the metadata for use in
bulk correction of OpenProject work packages.

Usage:
    python scripts/cache_jira_metadata.py [--batch-size 500] [--output var/data/jira_issue_metadata.json]
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
from src.infrastructure.jira.jira_client import JiraClient

logger = configure_logging("INFO", None)


def load_work_package_mapping(data_dir: Path) -> dict[str, dict[str, Any]]:
    """Load the work package mapping to get all Jira keys."""
    mapping_path = data_dir / "work_package_mapping.json"
    if not mapping_path.exists():
        logger.error("Work package mapping not found at %s", mapping_path)
        sys.exit(1)

    with open(mapping_path) as f:
        return json.load(f)


def extract_jira_keys(wp_mapping: dict[str, dict[str, Any]]) -> list[str]:
    """Extract all unique Jira keys from the mapping."""
    keys = []
    for entry in wp_mapping.values():
        jira_key = entry.get("jira_key")
        if jira_key:
            keys.append(jira_key)
    return sorted(set(keys))


def fetch_metadata_batch(
    jira_client: JiraClient,
    jira_keys: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch metadata for a batch of Jira issues."""
    if not jira_keys:
        return {}

    # Build JQL query for batch
    keys_str = ",".join(jira_keys)
    jql = f"key in ({keys_str})"

    metadata = {}
    try:
        issues = jira_client.jira.search_issues(
            jql,
            maxResults=len(jira_keys),
            fields="priority,assignee,reporter,created,updated",
        )

        for issue in issues:
            fields = issue.fields
            metadata[issue.key] = {
                "priority": getattr(fields.priority, "name", None) if fields.priority else None,
                "priority_id": getattr(fields.priority, "id", None) if fields.priority else None,
                "assignee": getattr(fields.assignee, "name", None) if fields.assignee else None,
                "assignee_key": getattr(fields.assignee, "key", None) if fields.assignee else None,
                "assignee_display": getattr(fields.assignee, "displayName", None) if fields.assignee else None,
                "reporter": getattr(fields.reporter, "name", None) if fields.reporter else None,
                "reporter_key": getattr(fields.reporter, "key", None) if fields.reporter else None,
                "reporter_display": getattr(fields.reporter, "displayName", None) if fields.reporter else None,
                "created": fields.created,
                "updated": fields.updated,
            }
    except Exception as e:
        logger.warning("Error fetching batch: %s", e)

    return metadata


def cache_all_jira_metadata(
    data_dir: Path,
    output_path: Path,
    batch_size: int = 500,
    resume: bool = True,
) -> dict[str, dict[str, Any]]:
    """Cache all Jira issue metadata for migrated work packages."""
    # Load existing metadata if resuming
    existing_metadata = {}
    if resume and output_path.exists():
        try:
            with open(output_path) as f:
                existing_metadata = json.load(f)
            logger.info("Loaded %d existing metadata entries", len(existing_metadata))
        except Exception:
            logger.warning("Could not load existing metadata, starting fresh")

    # Load work package mapping
    wp_mapping = load_work_package_mapping(data_dir)
    all_jira_keys = extract_jira_keys(wp_mapping)
    logger.info("Found %d unique Jira keys to fetch metadata for", len(all_jira_keys))

    # Filter out keys we already have
    if resume and existing_metadata:
        keys_to_fetch = [k for k in all_jira_keys if k not in existing_metadata]
        logger.info(
            "Skipping %d already cached, fetching %d remaining",
            len(all_jira_keys) - len(keys_to_fetch),
            len(keys_to_fetch),
        )
    else:
        keys_to_fetch = all_jira_keys

    if not keys_to_fetch:
        logger.info("All metadata already cached!")
        return existing_metadata

    # Initialize Jira client
    logger.info("Connecting to Jira...")
    jira_client = JiraClient()

    # Fetch in batches
    all_metadata = dict(existing_metadata)
    total_batches = (len(keys_to_fetch) + batch_size - 1) // batch_size

    for i in range(0, len(keys_to_fetch), batch_size):
        batch_num = i // batch_size + 1
        batch_keys = keys_to_fetch[i : i + batch_size]

        logger.info(
            "Fetching batch %d/%d (%d keys)...",
            batch_num,
            total_batches,
            len(batch_keys),
        )

        batch_metadata = fetch_metadata_batch(jira_client, batch_keys)
        all_metadata.update(batch_metadata)

        # Save progress after each batch
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(all_metadata, f, indent=2)

        logger.info(
            "Progress: %d/%d (%.1f%%)",
            len(all_metadata),
            len(all_jira_keys),
            100 * len(all_metadata) / len(all_jira_keys),
        )

    # Final summary
    logger.info("=" * 60)
    logger.info("METADATA CACHING COMPLETE")
    logger.info("=" * 60)
    logger.info("Total entries cached: %d", len(all_metadata))
    logger.info("Output file: %s", output_path)

    # Statistics
    with_priority = sum(1 for m in all_metadata.values() if m.get("priority"))
    with_assignee = sum(1 for m in all_metadata.values() if m.get("assignee"))
    with_reporter = sum(1 for m in all_metadata.values() if m.get("reporter"))

    logger.info(
        "Issues with priority: %d (%.1f%%)",
        with_priority,
        100 * with_priority / len(all_metadata) if all_metadata else 0,
    )
    logger.info(
        "Issues with assignee: %d (%.1f%%)",
        with_assignee,
        100 * with_assignee / len(all_metadata) if all_metadata else 0,
    )
    logger.info(
        "Issues with reporter: %d (%.1f%%)",
        with_reporter,
        100 * with_reporter / len(all_metadata) if all_metadata else 0,
    )

    return all_metadata


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Cache Jira issue metadata for bulk OpenProject update",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("var/data"),
        help="Path to migration data directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("var/data/jira_issue_metadata.json"),
        help="Output path for cached metadata",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of issues to fetch per batch",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start fresh, don't resume from existing cache",
    )
    args = parser.parse_args()

    cache_all_jira_metadata(
        data_dir=args.data_dir,
        output_path=args.output,
        batch_size=args.batch_size,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()
