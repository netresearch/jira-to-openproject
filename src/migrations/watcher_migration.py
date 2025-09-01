"""Watcher migration: add Jira watchers as OpenProject watchers idempotently."""

from __future__ import annotations

from typing import Any

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src.config import logger as logger  # type: ignore
    from src import config
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore  # noqa: PLC0415


@register_entity_types("watchers")
class WatcherMigration(BaseMigration):
    """Migrate watchers from Jira issues to OpenProject work packages."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client, op_client)
        self.mappings: Mappings = config.mappings

    def _resolve_wp_id(self, jira_key: str) -> int | None:
        wp_map = self.mappings.get_mapping("work_package") or {}
        entry = wp_map.get(jira_key)
        if isinstance(entry, int):
            return entry
        if isinstance(entry, dict):
            op_id = entry.get("openproject_id")
            if isinstance(op_id, int):
                return op_id
        return None

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

        # Build Jira keys list from work package mapping
        wp_map = self.mappings.get_mapping("work_package") or {}
        jira_keys = [
            (v.get("jira_key", k) if isinstance(v, dict) else k)
            for k, v in wp_map.items()
        ]
        jira_keys = [str(k) for k in jira_keys if k]
        if not jira_keys:
            logger.info("No work package mappings; skipping watcher migration")
            return result

        created = 0
        skipped = 0
        errors = 0

        # Batch fetch issues with watchers metadata where possible
        from src.clients.enhanced_jira_client import EnhancedJiraClient as _EJC  # noqa: PLC0415

        batch = _EJC.batch_get_issues(object(), jira_keys)  # type: ignore[misc]

        for key, issue in batch.items():
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
            except Exception:  # noqa: BLE001
                # Fallback to fields if available
                try:
                    w = getattr(getattr(issue, "fields", None), "watches", None)
                    watchers = getattr(w, "watchers", []) or []
                except Exception:  # noqa: BLE001
                    watchers = []

            for w in watchers or []:
                try:
                    username = w.get("name") if isinstance(w, dict) else getattr(w, "name", None)
                    user_id = self._resolve_user_id(username)
                    if not user_id:
                        skipped += 1
                        continue
                    if self.op_client.add_watcher(wp_id, user_id):
                        created += 1
                    else:
                        errors += 1
                except Exception:  # noqa: BLE001
                    errors += 1
                    continue

        result.details.update({"created": created, "skipped": skipped, "errors": errors})
        result.success = errors == 0
        result.message = f"Watchers created={created}, skipped={skipped}, errors={errors}"
        logger.info(result.message)
        return result


