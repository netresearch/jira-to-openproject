"""Watcher migration: add Jira watchers as OpenProject watchers idempotently.

Note on dict access patterns kept here
--------------------------------------
The ``user`` mapping is a flat dict keyed by Jira identifier (login,
accountId, …) whose values are typically dicts with an
``openproject_id`` field but may legacy-fall back to bare ints. There
is no dedicated typed user-mapping model yet (it is its own
boundary), so ``_resolve_user_id`` keeps its narrow ``isinstance``
ladder; the work-package side, however, is normalised through
:class:`WorkPackageMappingEntry.from_legacy`.

The Jira watchers list returned by
:meth:`JiraClient.get_issue_watchers` is parsed at the boundary into
:class:`JiraWatcher` instances so the per-row ``isinstance(w, dict)``
ladder disappears from this file.
"""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, JiraWatcher, WorkPackageMappingEntry


@register_entity_types("watchers")
class WatcherMigration(BaseMigration):
    """Migrate watchers from Jira issues to OpenProject work packages."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client, op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on issue watchers
        rather than fetching directly from Jira. It operates on already-fetched
        work package mapping data.

        Args:
            entity_type: The type of entities requested

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "WatcherMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on data from other migrations."
        )
        raise ValueError(msg)

    def _resolve_wp_id(self, jira_key: str) -> int | None:
        wp_map = self.mappings.get_mapping("work_package") or {}
        raw_entry = wp_map.get(jira_key)
        if raw_entry is None:
            return None
        try:
            entry = WorkPackageMappingEntry.from_legacy(jira_key, raw_entry)
        except ValueError:
            return None
        return int(entry.openproject_id)

    def _resolve_user_id(self, jira_username: str | None) -> int | None:
        if not jira_username:
            return None
        user_map = self.mappings.get_mapping("user") or {}
        entry = user_map.get(jira_username)
        if isinstance(entry, dict):
            op_id = entry.get("openproject_id")
            if isinstance(op_id, int):
                return op_id
        if isinstance(entry, int):
            return entry
        return None

    def run(self) -> ComponentResult:  # type: ignore[override]
        logger.info("Starting watcher migration...")
        result = ComponentResult(success=True, message="Watcher migration completed", details={})

        # Build Jira keys list from work package mapping. We normalise each
        # entry through :class:`WorkPackageMappingEntry.from_legacy` so the
        # ``jira_key``/bare-int polymorphism is collapsed at the boundary.
        wp_map = self.mappings.get_mapping("work_package") or {}
        jira_keys: list[str] = []
        for k, raw_entry in wp_map.items():
            try:
                entry = WorkPackageMappingEntry.from_legacy(str(k), raw_entry)
            except ValueError:
                continue
            jira_keys.append(str(entry.jira_key))
        if not jira_keys:
            logger.info("No work package mappings; skipping watcher migration")
            return result

        # Collect all watchers for bulk creation
        watchers_to_create: list[dict[str, Any]] = []
        skipped = 0

        # Use cached issues if available, otherwise iterate through keys directly
        issues: dict[str, Any] = {}
        cache_file = self.data_dir / "jira_issues_cache.json"
        if cache_file.exists():
            try:
                import json

                with open(cache_file) as f:
                    issues = json.load(f)
                logger.info("Using cached issues for watcher migration (%d issues)", len(issues))
            except Exception:
                issues = {}

        # If no cache, create a minimal dict from jira_keys for iteration
        if not issues:
            logger.info("No cached issues, will iterate through %d Jira keys", len(jira_keys))
            issues = {k: {} for k in jira_keys}

        for key, issue in issues.items():
            if not issue:
                skipped += 1
                continue
            wp_id = self._resolve_wp_id(str(key))
            if not wp_id:
                skipped += 1
                continue

            watchers = []
            try:
                # Prefer explicit API for watchers (more complete)
                watchers = self.jira_client.get_issue_watchers(str(key))
            except Exception:
                # Fallback to fields if available
                try:
                    w = getattr(getattr(issue, "fields", None), "watches", None)
                    watchers = getattr(w, "watchers", []) or []
                except Exception:
                    watchers = []

            for w in watchers or []:
                try:
                    parsed = JiraWatcher.from_any(w)
                    if parsed is None:
                        skipped += 1
                        continue
                    user_id = self._resolve_user_id(parsed.name)
                    if not user_id:
                        skipped += 1
                        continue
                    watchers_to_create.append(
                        {
                            "work_package_id": wp_id,
                            "user_id": user_id,
                        },
                    )
                except Exception:
                    skipped += 1
                    continue

        # Bulk create all watchers in single Rails call
        created = 0
        errors = 0
        bulk_skipped = 0
        if watchers_to_create:
            logger.info("Bulk adding %d watchers...", len(watchers_to_create))
            bulk_result = self.op_client.bulk_add_watchers(watchers_to_create)
            created = bulk_result.get("created", 0)
            bulk_skipped = bulk_result.get("skipped", 0)
            errors = bulk_result.get("failed", 0)
            logger.info(
                "Bulk watchers: created=%d, skipped=%d, failed=%d",
                created,
                bulk_skipped,
                errors,
            )

        result.details.update({"created": created, "skipped": skipped + bulk_skipped, "errors": errors})
        result.success = errors == 0
        result.message = f"Watchers created={created}, skipped={skipped + bulk_skipped}, errors={errors}"
        logger.info(result.message)
        return result
