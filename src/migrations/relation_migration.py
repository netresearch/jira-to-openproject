"""Relation migration: create OpenProject relations from Jira issue links.

Minimal first pass: handles standard types (relates, duplicates, blocks, precedes)
with direction-safe mapping and idempotent creation via client helpers.
"""

from __future__ import annotations

from pathlib import Path
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


@register_entity_types("relations", "issue_links")
class RelationMigration(BaseMigration):
    """Create OpenProject relations from Jira issue links using mappings."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client, op_client)
        self.mappings: Mappings = config.mappings

        # Inverse/direction mapping table
        # tuple of (jira_link_name_lower, direction) -> (op_type, swap)
        # direction is 'outward' or 'inward' from the Jira issue perspective
        self.direction_map: dict[tuple[str, str], tuple[str, bool]] = {
            ("relates", "outward"): ("relates", False),
            ("relates", "inward"): ("relates", False),
            ("duplicates", "outward"): ("duplicates", False),
            ("duplicates", "inward"): ("duplicates", True),  # duplicated by
            ("blocks", "outward"): ("blocks", False),
            ("blocks", "inward"): ("blocks", True),  # blocked by => swap
            ("precedes", "outward"): ("precedes", False),
            ("precedes", "inward"): ("follows", True),
        }

    def _resolve_wp_id(self, jira_key: str) -> int | None:
        """Resolve OpenProject WP ID from Jira key via mappings or local map."""
        wp_map = {}
        try:
            wp_map = self.mappings.get_mapping("work_package") or {}
        except Exception:  # noqa: BLE001
            wp_map = {}

        entry = wp_map.get(jira_key)
        if entry is None:
            alt = getattr(self, "_wp_key_map", None)
            if isinstance(alt, dict):
                entry = alt.get(jira_key)

        if isinstance(entry, int):
            return entry
        if isinstance(entry, str) and entry.isdigit():
            return int(entry)
        if isinstance(entry, dict):
            op_id = entry.get("openproject_id")
            if isinstance(op_id, int):
                return op_id
            if isinstance(op_id, str) and op_id.isdigit():
                return int(op_id)
        return None

    def _map_type_and_direction(
        self,
        jira_link_name: str,
        direction: str,
    ) -> tuple[str, bool] | None:
        key = (jira_link_name.lower(), direction)
        return self.direction_map.get(key)

    def run(self) -> ComponentResult:  # type: ignore[override]
        """Execute relation creation based on persisted mapping and Jira links."""
        logger.info("Starting relation migration...")
        result = ComponentResult(success=True, message="Relation migration completed", details={})

        # Load persisted link_type_mapping (from link_type_migration)
        link_type_mapping = self.mappings.get_mapping("link_type")
        if not link_type_mapping:
            logger.warning("No link_type_mapping found; relations may be skipped")

        # Load work_package_mapping file if present (guarded inside WPM too)
        # Fallback to mappings store
        wp_map_file = self.data_dir / "work_package_mapping.json"
        work_package_map: dict[str, Any] = {}
        if wp_map_file.exists():
            try:
                from src.utils import data_handler as _dh  # noqa: PLC0415

                work_package_map = _dh.load(wp_map_file) or {}
            except Exception:  # noqa: BLE001
                work_package_map = {}

        # Assemble set of issues to process using WPM mapping keys
        if work_package_map:
            jira_keys: list[str] = [str(v.get("jira_key")) for v in work_package_map.values() if v.get("jira_key")]
        else:
            jira_keys = list((self.mappings.get_mapping("work_package") or {}).keys())

        if not jira_keys:
            logger.info("No work package mapping entries found; skipping relations")
            return result

        # Fetch issues with changelog (issuelinks are in fields by default when expanded)
        # Build quick lookup from mapping file by Jira key → OP ID
        self._wp_key_map = {}
        try:
            if work_package_map:
                self._wp_key_map = {
                    str(v.get("jira_key")): v.get("openproject_id")
                    for v in work_package_map.values()
                    if isinstance(v, dict) and v.get("jira_key")
                }
        except Exception:  # noqa: BLE001
            self._wp_key_map = {}

        # Use EnhancedJiraClient.batch_get_issues without constructing it (avoid connection)
        from src.clients.enhanced_jira_client import EnhancedJiraClient as _EJC  # noqa: PLC0415

        batch = _EJC.batch_get_issues(object(), jira_keys)  # type: ignore[misc]

        created = 0
        skipped = 0
        errors = 0

        for key, issue in batch.items():
            if not issue:
                skipped += 1
                continue
            # Resolve local from_id
            from_id = self._resolve_wp_id(key)
            if not from_id:
                skipped += 1
                continue

            links = getattr(getattr(issue, "fields", None), "issuelinks", []) or []
            for l in links:
                try:
                    lt = getattr(l, "type", None)
                    if not lt:
                        skipped += 1
                        continue
                    outward = getattr(l, "outwardIssue", None)
                    inward = getattr(l, "inwardIssue", None)
                    if outward is not None:
                        direction = "outward"
                        target_key = getattr(outward, "key", None)
                    elif inward is not None:
                        direction = "inward"
                        target_key = getattr(inward, "key", None)
                    else:
                        skipped += 1
                        continue

                    if not target_key:
                        skipped += 1
                        continue

                    to_id = self._resolve_wp_id(str(target_key))
                    if not to_id:
                        skipped += 1
                        continue

                    # Map type/direction
                    name = getattr(lt, "name", "") or getattr(lt, "outward", "")
                    mapping = self._map_type_and_direction(name, direction)
                    if not mapping:
                        skipped += 1
                        continue
                    relation_type, swap = mapping
                    a, b = (from_id, to_id) if not swap else (to_id, from_id)

                    # Idempotent creation
                    existing = self.op_client.find_relation(a, b)
                    if existing:
                        skipped += 1
                        continue
                    created_ok = self.op_client.create_relation(a, b, relation_type)
                    if created_ok:
                        created += 1
                    else:
                        errors += 1
                except Exception:  # noqa: BLE001
                    errors += 1
                    continue

        result.details.update(
            {
                "created": created,
                "skipped": skipped,
                "errors": errors,
            }
        )
        result.success = errors == 0
        result.message = (
            f"Relations created={created}, skipped={skipped}, errors={errors}"
        )
        logger.info(result.message)
        # Save simple summary
        self._save_to_json(result.details, Path("relation_migration_summary.json"))
        return result


