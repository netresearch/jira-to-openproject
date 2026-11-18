# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Relation migration: create OpenProject relations from Jira issue links.

Minimal first pass: handles standard types (relates, duplicates, blocks, precedes)
with direction-safe mapping and idempotent creation via client helpers.

Phase 7d notes
--------------
The polymorphic ``wp_map`` (``dict | int``) ladder used to resolve a
Jira issue key to an OpenProject work-package id is normalised through
:meth:`WorkPackageMappingEntry.from_legacy` here. The Jira issue-link
parsing keeps its defensive duck-typing because ``issuelinks`` carry
tenant-specific fields (custom link types, both SDK objects and cache
dicts) that are not modelled by :class:`JiraIssueFields`. The
OpenProject-side ``bulk_create_relations`` payload (``from_id``,
``to_id``, ``relation_type``) is left as plain dicts — that's the API
surface and out of phase 7's scope.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry


@register_entity_types("relations", "issue_links")
class RelationMigration(BaseMigration):
    """Create OpenProject relations from Jira issue links using mappings."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client, op_client)

        self.direction_map = self.supported_directions()

    @staticmethod
    def supported_directions() -> dict[tuple[str, str], tuple[str, bool]]:
        """Map Jira relation vocabulary to OpenProject types and edge orientation."""
        # Inverse/direction mapping table
        # tuple of (jira_link_name_lower, direction) -> (op_type, swap)
        # direction is 'outward' or 'inward' from the Jira issue perspective
        # Note: Jira link type names vary by instance - these are for Netresearch Jira
        return {
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
            ("precedes", "outward"): ("follows", True),
            ("precedes", "inward"): ("follows", False),
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
        """Resolve OpenProject WP ID from a Jira key via mappings or local map.

        Uses :meth:`WorkPackageMappingEntry.from_legacy` to absorb the
        legacy polymorphic shape (``int``, ``{"openproject_id": int|str}``).
        Pydantic's coercion handles numeric strings; the ``from_legacy``
        layer reraises ``ValidationError`` (a ``ValueError`` subclass)
        for unparseable inputs, which we treat as "unresolvable" — same
        behaviour as the pre-typed lookup.
        """
        wp_map: dict[str, Any] = {}
        try:
            wp_map = self.mappings.get_mapping("work_package") or {}
        except Exception:
            wp_map = {}

        entry = wp_map.get(jira_key)
        if entry is None:
            alt = getattr(self, "_wp_key_map", None)
            if isinstance(alt, dict):
                entry = alt.get(jira_key)

        if entry is None:
            self._record_resolve_failure(jira_key, entry)
            return None

        # Bare numeric strings come from legacy mapping shapes that the
        # entry model rejects (it only accepts ``int`` / ``dict``). Coerce
        # them up-front so the typed parse below is the single source of
        # truth for valid lookups.
        if isinstance(entry, str) and entry.isdigit():
            entry = int(entry)

        try:
            typed = WorkPackageMappingEntry.from_legacy(jira_key, entry)
        except ValueError:
            self._record_resolve_failure(jira_key, entry)
            return None
        return int(typed.openproject_id)

    def _record_resolve_failure(self, jira_key: str, entry: Any) -> None:
        """Log the first few ``_resolve_wp_id`` misses for forensics."""
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
        work_package_map, jira_keys = self._mapped_issue_keys()

        if not jira_keys:
            logger.info("No work package mapping entries found; skipping relations")
            return result

        # Fetch issues with changelog (issuelinks are in fields by default when expanded)
        # Build quick lookup from mapping file by Jira key → OP ID
        self._index_mapped_issues(work_package_map)

        # Batch get issues using jira_client
        # First check for cached issues from work_packages_content migration
        issues = self._relation_issues(jira_keys)

        # Collect all relations for bulk creation. Track *why* each
        # link was skipped so a post-run summary can distinguish
        # "expected loss" (cross-project link, unmapped link type)
        # from "real loss" (target WP that should have migrated but
        # didn't). Without this breakdown the audit can only see the
        # aggregate count delta and operators have to guess which
        # bucket dominates.
        relations_to_create, skip_reasons = self._collect_relations(issues)

        if skip_reasons["target_wp_unmigrated"]:
            logger.info("Use j2o-references capture to retain links to work packages scheduled for a later cutover.")
        skipped = sum(skip_reasons.values())
        if skipped:
            logger.info(
                "Relation skip breakdown (%d total, before bulk): %s",
                skipped,
                dict(skip_reasons),
            )

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

        total_skipped = skipped + bulk_skipped
        # Surface the per-reason breakdown alongside the aggregate
        # ``skipped`` so a post-migration audit can answer "why" not
        # just "how many". ``bulk_skipped`` is included as its own
        # bucket so the breakdown sums to the total.
        skip_reasons_with_bulk: dict[str, int] = dict(skip_reasons)
        if bulk_skipped:
            skip_reasons_with_bulk["bulk_dedup_or_invalid"] = bulk_skipped
        result.details.update(
            {
                "created": created,
                "skipped": total_skipped,
                "skip_reasons": skip_reasons_with_bulk,
                "errors": errors,
                # Counts the migration runner's reporter looks for
                # (``base_migration._extract_counts``). Without these the
                # summary line reads '0/0 items migrated, 0 failed' even
                # when the bulk path actually created hundreds of relations.
                "success_count": created,
                "failed_count": errors,
                "total_count": created + total_skipped + errors,
            },
        )
        result.success = errors == 0
        result.message = f"Relations created={created}, skipped={skipped + bulk_skipped}, errors={errors}"
        logger.info(result.message)
        # Save simple summary
        self._save_to_json(result.details, Path("relation_migration_summary.json"))
        return result

    def _mapped_issue_keys(self) -> tuple[dict, list[str]]:
        """Read the persisted mapping and its source issue keys."""
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
        return work_package_map, jira_keys

    def _index_mapped_issues(self, work_package_map: dict) -> None:
        """Index target IDs for both persisted and in-memory mapping formats."""
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

    def _relation_issues(self, jira_keys: list[str]) -> dict[str, Any]:
        """Read cached Jira links or fetch the issues as a batch."""
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
        return issues

    @staticmethod
    def _issue_links(issue: Any) -> list:
        """Read the issue links from either an SDK issue or a stored JSON record."""
        if hasattr(issue, "fields"):
            links = getattr(issue.fields, "issuelinks", []) or []
        elif isinstance(issue, dict):
            fields = issue.get("fields", {}) or {}
            links = fields.get("issuelinks", []) or []
        else:
            links = []
        return links

    @staticmethod
    def _link_parts(link: Any) -> tuple | None:
        if hasattr(link, "type"):
            return link.type, getattr(link, "outwardIssue", None), getattr(link, "inwardIssue", None)
        if isinstance(link, dict):
            return link.get("type"), link.get("outwardIssue"), link.get("inwardIssue")
        return None

    @staticmethod
    def _linked_issue_key(issue: Any) -> str | None:
        if hasattr(issue, "key"):
            return issue.key
        return issue.get("key") if isinstance(issue, dict) else None

    @staticmethod
    def _link_type_name(link_type: Any) -> str:
        if hasattr(link_type, "name"):
            return link_type.name or getattr(link_type, "outward", "")
        if isinstance(link_type, dict):
            return link_type.get("name", "") or link_type.get("outward", "")
        return str(link_type)

    def _relation_from_link(self, link: Any, from_id: int, skipped: Counter[str]) -> dict | None:
        """Resolve a link's target and direction, accounting for each discarded row."""
        parts = self._link_parts(link)
        if parts is None:
            skipped["link_unknown_shape"] += 1
            return None
        link_type, outward, inward = parts
        if not link_type:
            skipped["link_no_type"] += 1
            return None
        if outward is None and inward is None:
            skipped["link_no_direction"] += 1
            return None
        direction, target = ("outward", outward) if outward is not None else ("inward", inward)
        target_key = self._linked_issue_key(target)
        if not target_key:
            skipped["link_no_target_key"] += 1
            return None
        to_id = self._resolve_wp_id(str(target_key))
        if not to_id:
            skipped["target_wp_unmigrated"] += 1
            return None
        mapping = self._map_type_and_direction(self._link_type_name(link_type), direction)
        if not mapping:
            skipped["link_type_unmapped"] += 1
            return None
        relation_type, swap = mapping
        source, target = (to_id, from_id) if swap else (from_id, to_id)
        return {"from_id": source, "to_id": target, "relation_type": relation_type}

    def _collect_relations(self, issues: dict) -> tuple[list[dict], Counter[str]]:
        """Collect directed relations and retain each reason an issue or link was skipped."""
        relations_to_create: list[dict[str, Any]] = []
        skip_reasons: Counter[str] = Counter()

        logger.info(
            "Processing %d issues for relations, _wp_key_map has %d entries",
            len(issues),
            len(self._wp_key_map),
        )

        for key, issue in issues.items():
            if not issue:
                skip_reasons["empty_issue"] += 1
                continue
            # Resolve local from_id
            from_id = self._resolve_wp_id(key)
            if not from_id:
                skip_reasons["unmapped_source_wp"] += 1
                continue

            # Handle both JIRA objects and raw dicts (from cache)
            links = self._issue_links(issue)

            for link in links:
                try:
                    relation = self._relation_from_link(link, from_id, skip_reasons)
                    if relation is not None:
                        relations_to_create.append(relation)
                except Exception:
                    skip_reasons["exception"] += 1
        return relations_to_create, skip_reasons
