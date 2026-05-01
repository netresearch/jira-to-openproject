"""Relation migration: create OpenProject relations from Jira issue links.

Minimal first pass: handles standard types (relates, duplicates, blocks, precedes)
with direction-safe mapping and idempotent creation via client helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult


@register_entity_types("relations", "issue_links")
class RelationMigration(BaseMigration):
    """Create OpenProject relations from Jira issue links using mappings."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client, op_client)

        # Inverse/direction mapping table
        # tuple of (jira_link_name_lower, direction) -> (op_type, swap)
        # direction is 'outward' or 'inward' from the Jira issue perspective
        # Note: Jira link type names vary by instance - these are for Netresearch Jira
        self.direction_map: dict[tuple[str, str], tuple[str, bool]] = {
            # Standard relation types
            ("relates", "outward"): ("relates", False),
            ("relates", "inward"): ("relates", False),
            ("relation", "outward"): ("relates", False),  # Netresearch: "Relation"
            ("relation", "inward"): ("relates", False),
            # Duplicates
            ("duplicates", "outward"): ("duplicates", False),
            ("duplicates", "inward"): ("duplicates", True),  # duplicated by
            ("duplicate", "outward"): ("duplicates", False),  # Netresearch: "Duplicate"
            ("duplicate", "inward"): ("duplicates", True),
            # Blocks
            ("blocks", "outward"): ("blocks", False),
            ("blocks", "inward"): ("blocks", True),  # blocked by => swap
            ("blockade", "outward"): ("blocks", False),  # Netresearch: "Blockade"
            ("blockade", "inward"): ("blocks", True),
            # Precedes/Follows
            ("precedes", "outward"): ("precedes", False),
            ("precedes", "inward"): ("follows", True),
            # Additional Netresearch link types mapped to relates
            ("cause", "outward"): ("relates", False),
            ("cause", "inward"): ("relates", False),
            ("mention", "outward"): ("relates", False),
            ("mention", "inward"): ("relates", False),
            ("deploy", "outward"): ("relates", False),
            ("deploy", "inward"): ("relates", False),
            ("collision", "outward"): ("relates", False),
            ("collision", "inward"): ("relates", False),
            ("admin", "outward"): ("relates", False),
            ("admin", "inward"): ("relates", False),
            ("qa", "outward"): ("relates", False),
            ("qa", "inward"): ("relates", False),
            ("resolve", "outward"): ("relates", False),
            ("resolve", "inward"): ("relates", False),
            ("side effect", "outward"): ("relates", False),
            ("side effect", "inward"): ("relates", False),
        }

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on issue relationships
        rather than fetching directly from Jira. It operates on already-fetched
        work package data and link type mappings.

        Args:
            entity_type: The type of entities requested (e.g., "relations", "issue_links")

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "RelationMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on data from other migrations."
        )
        raise ValueError(msg)

    def _resolve_wp_id(self, jira_key: str) -> int | None:
        """Resolve OpenProject WP ID from Jira key via mappings or local map."""
        wp_map = {}
        try:
            wp_map = self.mappings.get_mapping("work_package") or {}
        except Exception:
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
        # Debug: log first few failures
        if not hasattr(self, "_resolve_failures"):
            self._resolve_failures = 0
        self._resolve_failures += 1
        if self._resolve_failures <= 5:
            logger.warning(
                "_resolve_wp_id failed for %s: wp_map_entry=%s, _wp_key_map_has=%s",
                jira_key,
                entry,
                jira_key in (getattr(self, "_wp_key_map", {}) or {}),
            )
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
        logger.info("Looking for work_package_mapping at: %s (exists=%s)", wp_map_file, wp_map_file.exists())
        work_package_map: dict[str, Any] = {}
        if wp_map_file.exists():
            try:
                from src.utils import data_handler as _dh

                work_package_map = _dh.load_dict(wp_map_file) or {}
                logger.info("Loaded work_package_mapping: %d entries", len(work_package_map))
            except Exception as e:
                logger.warning("Failed to load work_package_mapping: %s", e)
                work_package_map = {}

        # Assemble set of issues to process using WPM mapping keys
        if work_package_map:
            jira_keys: list[str] = [str(v.get("jira_key")) for v in work_package_map.values() if v.get("jira_key")]
        else:
            jira_keys = list((self.mappings.get_mapping("work_package") or {}).keys())
            logger.info("Using mappings fallback for jira_keys: %d keys", len(jira_keys))

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
            else:
                # Fallback: build from mappings store
                # Note: mapping keys are numeric Jira IDs, values have jira_key field
                wp_mappings = self.mappings.get_mapping("work_package") or {}
                for entry in wp_mappings.values():
                    if isinstance(entry, dict):
                        jira_key = entry.get("jira_key")
                        op_id = entry.get("openproject_id")
                        if jira_key and op_id:
                            self._wp_key_map[str(jira_key)] = op_id
                logger.info("Built _wp_key_map from mappings fallback: %d entries", len(self._wp_key_map))
        except Exception as e:
            logger.warning("Failed to build _wp_key_map: %s", e)
            self._wp_key_map = {}

        # Batch get issues using jira_client
        # First check for cached issues from work_packages_content migration
        issues: dict[str, Any] = {}
        cache_file = self.data_dir / "jira_issues_cache.json"
        logger.info("Checking for cached issues at: %s (exists=%s)", cache_file, cache_file.exists())
        if cache_file.exists():
            try:
                import json

                logger.info(
                    "Loading cached issues from %s (size=%d MB)...",
                    cache_file,
                    cache_file.stat().st_size // 1024 // 1024,
                )
                with open(cache_file) as f:
                    cached = json.load(f)
                if isinstance(cached, dict) and len(cached) > 0:
                    logger.info("Using cached issues from %s (%d issues)", cache_file, len(cached))
                    issues = cached
            except Exception as e:
                logger.warning("Failed to load cached issues: %s. Will fetch from Jira", e)

        if not issues:
            logger.info("Fetching %d issues from Jira for relation extraction...", len(jira_keys))
            issues = self._merge_batch_issues(jira_keys)
            logger.info("Fetched %d issues from Jira", len(issues))

        # Collect all relations for bulk creation
        relations_to_create: list[dict[str, Any]] = []
        skipped = 0

        logger.info(
            "Processing %d issues for relations, _wp_key_map has %d entries",
            len(issues),
            len(self._wp_key_map),
        )

        for key, issue in issues.items():
            if not issue:
                skipped += 1
                continue
            # Resolve local from_id
            from_id = self._resolve_wp_id(key)
            if not from_id:
                skipped += 1
                continue

            # Handle both JIRA objects and raw dicts (from cache)
            if hasattr(issue, "fields"):
                links = getattr(issue.fields, "issuelinks", []) or []
            elif isinstance(issue, dict):
                fields = issue.get("fields", {}) or {}
                links = fields.get("issuelinks", []) or []
            else:
                links = []

            for l in links:
                try:
                    # Handle both JIRA objects and raw dicts (from cache)
                    if hasattr(l, "type"):
                        lt = l.type
                        outward = getattr(l, "outwardIssue", None)
                        inward = getattr(l, "inwardIssue", None)
                    elif isinstance(l, dict):
                        lt = l.get("type")
                        outward = l.get("outwardIssue")
                        inward = l.get("inwardIssue")
                    else:
                        skipped += 1
                        continue

                    if not lt:
                        skipped += 1
                        continue

                    if outward is not None:
                        direction = "outward"
                        target_key = (
                            getattr(outward, "key", None)
                            if hasattr(outward, "key")
                            else outward.get("key")
                            if isinstance(outward, dict)
                            else None
                        )
                    elif inward is not None:
                        direction = "inward"
                        target_key = (
                            getattr(inward, "key", None)
                            if hasattr(inward, "key")
                            else inward.get("key")
                            if isinstance(inward, dict)
                            else None
                        )
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

                    # Map type/direction - handle both JIRA objects and dicts
                    if hasattr(lt, "name"):
                        name = lt.name or getattr(lt, "outward", "")
                    elif isinstance(lt, dict):
                        name = lt.get("name", "") or lt.get("outward", "")
                    else:
                        name = str(lt)
                    mapping = self._map_type_and_direction(name, direction)
                    if not mapping:
                        skipped += 1
                        continue
                    relation_type, swap = mapping
                    a, b = (from_id, to_id) if not swap else (to_id, from_id)

                    # Collect for bulk creation
                    relations_to_create.append(
                        {
                            "from_id": a,
                            "to_id": b,
                            "relation_type": relation_type,
                        },
                    )
                except Exception:
                    skipped += 1
                    continue

        # Bulk create all relations in single Rails call
        created = 0
        errors = 0
        bulk_skipped = 0
        if relations_to_create:
            logger.info("Bulk creating %d relations...", len(relations_to_create))
            bulk_result = self.op_client.bulk_create_relations(relations_to_create)
            created = bulk_result.get("created", 0)
            bulk_skipped = bulk_result.get("skipped", 0)
            errors = bulk_result.get("failed", 0)
            logger.info(
                "Bulk relations: created=%d, skipped=%d, failed=%d",
                created,
                bulk_skipped,
                errors,
            )

        result.details.update(
            {
                "created": created,
                "skipped": skipped + bulk_skipped,
                "errors": errors,
            },
        )
        result.success = errors == 0
        result.message = f"Relations created={created}, skipped={skipped + bulk_skipped}, errors={errors}"
        logger.info(result.message)
        # Save simple summary
        self._save_to_json(result.details, Path("relation_migration_summary.json"))
        return result
