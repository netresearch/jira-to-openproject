#!/usr/bin/env python
"""Cache all Jira issues for relation migration to avoid session timeout."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient


def main() -> int:
    """Cache all Jira issues to avoid session timeout during migrations."""
    data_dir = Path("var/data")
    cache_file = data_dir / "jira_issues_cache.json"

    # Load work package mapping to get all Jira keys
    wp_mapping_file = data_dir / "work_package_mapping.json"
    if not wp_mapping_file.exists():
        logger.error("work_package_mapping.json not found")
        return 1

    with open(wp_mapping_file) as f:
        wp_mapping = json.load(f)

    jira_keys = list(wp_mapping.keys())
    logger.info("Found %d Jira keys to cache", len(jira_keys))

    # Initialize Jira client
    jira_client = JiraClient()

    # Fetch all issues in batches
    all_issues: dict = {}
    batch_size = 100
    for i in range(0, len(jira_keys), batch_size):
        batch_keys = jira_keys[i : i + batch_size]
        logger.info(
            "Fetching batch %d/%d (%d keys)...",
            i // batch_size + 1,
            (len(jira_keys) + batch_size - 1) // batch_size,
            len(batch_keys),
        )

        try:
            batch_get = getattr(jira_client, "batch_get_issues", None)
            if callable(batch_get):
                result = batch_get(batch_keys)
                if isinstance(result, list):
                    for batch_dict in result:
                        if isinstance(batch_dict, dict):
                            # Convert JIRA objects to dicts for JSON serialization
                            for key, issue in batch_dict.items():
                                if hasattr(issue, "raw"):
                                    all_issues[key] = issue.raw
                                elif isinstance(issue, dict):
                                    all_issues[key] = issue
                elif isinstance(result, dict):
                    for key, issue in result.items():
                        if hasattr(issue, "raw"):
                            all_issues[key] = issue.raw
                        elif isinstance(issue, dict):
                            all_issues[key] = issue
        except Exception as e:
            logger.exception("Failed to fetch batch starting at %d: %s", i, e)
            continue

        # Progress
        if (i // batch_size) % 10 == 0:
            logger.info("Progress: %d/%d issues cached", len(all_issues), len(jira_keys))

    # Save cache
    logger.info("Saving %d issues to %s", len(all_issues), cache_file)
    with open(cache_file, "w") as f:
        json.dump(all_issues, f)

    logger.info("Done! Cached %d issues", len(all_issues))
    return 0


if __name__ == "__main__":
    sys.exit(main())
