"""Issue #260: only the first comment per work package was migrated.

Two compounding defects dropped comments silently:

1. The Rails ``bulk_create_work_package_activities`` helper pre-fetches each
   WorkPackage once and reuses that same in-memory object for *every* comment
   of that WP. Without a ``reload`` between saves the second-and-later saves of
   a WP fail (stale association/version state); the bare ``rescue`` counts them
   as ``failed`` and drops them.
2. The Python caller read only ``result["created"]`` and discarded the Rails
   ``failed``/``success`` counts, so the run reported success while comments
   were lost.

These tests pin both halves of the fix.
"""

from __future__ import annotations

import inspect
from pathlib import Path

# Reuse the shared content-migration builder rather than re-defining it.
from tests.unit.test_work_package_content_comment_idempotency import _build_mig


def test_bulk_ruby_reloads_wp_before_each_comment_save() -> None:
    """The Ruby loop must reload each WP so it doesn't reuse a stale object."""
    from src.infrastructure.openproject.openproject_work_package_content_service import (
        OpenProjectWorkPackageContentService,
    )

    source = inspect.getsource(OpenProjectWorkPackageContentService.bulk_create_work_package_activities)
    assert "wp.reload" in source, (
        "each comment must call wp.reload so the loop doesn't reuse a stale "
        "in-memory object — otherwise only the first comment per WP persists (#260)"
    )


def test_bulk_process_surfaces_partial_comment_failures(tmp_path: Path) -> None:
    """A Rails partial failure (created=1, failed=2) must be surfaced, not
    silently reduced to ``comments_migrated=1``.
    """
    mig = _build_mig(tmp_path)
    collected_items = [
        {
            "wp_id": 5040,
            "jira_key": "PROJ-1",
            "description_update": None,
            "custom_field_updates": {},
            "comments": [
                {"comment": f"c{i}", "user_id": 1, "jira_comment_id": str(i), "created_at": None} for i in range(1, 4)
            ],
            "watchers": [],
        },
    ]
    mig.op_client.bulk_create_work_package_activities.return_value = {
        "created": 1,
        "skipped": 0,
        "failed": 2,
        "success": False,
        "errors": [{"wp_id": 5040, "error": "stale object"}],
    }

    results = mig._bulk_process_collected_content(collected_items)

    assert results["comments_migrated"] == 1
    assert results["comments_failed"] == 2, "dropped comments must be surfaced, not silently discarded"
