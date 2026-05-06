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

from collections import Counter
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
        # Production wp_map is keyed by str(jira_id) (numeric) outer with
        # the human-readable ``jira_key`` stored in the inner dict. Walk
        # values and match on the inner ``jira_key`` so callers can pass
        # either the numeric id or the human-readable key.
        wp_map = self.mappings.get_mapping("work_package") or {}
        for outer_key, raw_entry in wp_map.items():
            inner_jira_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
            candidate = inner_jira_key or str(outer_key)
            if candidate != jira_key:
                continue
            try:
                entry = WorkPackageMappingEntry.from_legacy(candidate, raw_entry)
            except ValueError:
                return None
            return int(entry.openproject_id)
        return None

    def _resolve_user_id(self, watcher: JiraWatcher) -> int | None:
        """Map a typed watcher to an OP user id.

        Probes the user mapping using (in order): ``account_id``,
        ``name``, ``email_address``, ``display_name`` — matching the
        pattern used in :class:`AttachmentProvenanceMigration`. Cloud
        instances primarily key on ``account_id``; Server/DC on ``name``.
        """
        user_map = self.mappings.get_mapping("user") or {}
        for probe in (
            watcher.account_id,
            watcher.name,
            watcher.email_address,
            watcher.display_name,
        ):
            if not probe:
                continue
            entry = user_map.get(probe)
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

        # Build Jira keys list from work package mapping. The on-disk
        # layout is ``{str(jira_id): {"jira_key": str, "openproject_id":
        # int, …}}`` — outer key is numeric, inner ``jira_key`` is the
        # human-readable PROJ-123 form. Read the inner ``jira_key`` for
        # the typed entry so downstream code uses the human-readable key
        # rather than the numeric id.
        wp_map = self.mappings.get_mapping("work_package") or {}
        jira_keys: list[str] = []
        for outer_key, raw_entry in wp_map.items():
            inner_jira_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
            key_for_legacy = inner_jira_key or str(outer_key)
            try:
                entry = WorkPackageMappingEntry.from_legacy(str(key_for_legacy), raw_entry)
            except ValueError:
                continue
            jira_keys.append(str(entry.jira_key))
        if not jira_keys:
            # FAIL LOUD. Same anti-pattern as pre-#194 attachments and
            # pre-#197 wp_skeleton — silent ``success=True`` here masks
            # an upstream broken precondition (e.g. ``_save_mapping``
            # swallowed a write error in ``work_packages_skeleton``).
            # Without the WP map there are no watchers to migrate, but
            # this is a *failure mode*, not a success, because every
            # downstream consumer reading this component's
            # ``ComponentResult`` would see green and move on.
            msg = (
                "No work_package mapping available — watchers cannot be"
                " correlated to OP work packages. Run work_packages_skeleton"
                " first (or verify its mapping persisted)."
            )
            logger.error(msg)
            return ComponentResult(
                success=False,
                message=msg,
                errors=["missing_work_package_mapping"],
                details={},
            )

        # Collect all watchers for bulk creation. Track *why* each
        # watcher was skipped so a post-run summary can distinguish
        # "expected loss" (unmapped user / locked-account artifact)
        # from "real loss" (parse failure, missing WP). Without this
        # breakdown the audit can only see the aggregate count delta
        # and operators have to guess which bucket dominates.
        watchers_to_create: list[dict[str, Any]] = []
        skip_reasons: Counter[str] = Counter()

        # Track DISTINCT unmapped watcher identities. Per the live
        # TEST audit (2026-05-06) the watcher migration dropped 93%
        # of watchers; the dominant bucket was ``user_unmapped`` —
        # Jira users not in the OP user mapping (locked / disabled /
        # never-synced accounts). The aggregate ``skip_reasons``
        # count tells the operator HOW MANY watcher rows were
        # dropped; this set tells them WHICH users to fix. Stored
        # as a set so a user watching N issues only logs once.
        unmapped_users: set[str] = set()

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
                skip_reasons["empty_issue"] += 1
                continue
            wp_id = self._resolve_wp_id(str(key))
            if not wp_id:
                skip_reasons["wp_unmapped"] += 1
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
                        skip_reasons["watcher_parse_failed"] += 1
                        continue
                    user_id = self._resolve_user_id(parsed)
                    if not user_id:
                        # Biggest bucket in practice — Jira watchers
                        # whose account_id/name/email/display_name
                        # never made it into the user mapping (locked
                        # users, accounts deleted in OP, mapping
                        # not refreshed, etc.).
                        skip_reasons["user_unmapped"] += 1
                        # Record the distinct identity. Prefer the
                        # most stable probe (account_id) and fall
                        # back through the same probe order
                        # ``_resolve_user_id`` uses, so the logged
                        # value is the one an operator would search
                        # the user mapping for.
                        identity = (
                            parsed.account_id
                            or parsed.name
                            or parsed.email_address
                            or parsed.display_name
                            or "<unknown>"
                        )
                        unmapped_users.add(str(identity))
                        continue
                    watchers_to_create.append(
                        {
                            "work_package_id": wp_id,
                            "user_id": user_id,
                        },
                    )
                except Exception:
                    skip_reasons["exception"] += 1
                    continue

        skipped = sum(skip_reasons.values())
        if skipped:
            logger.info(
                "Watcher skip breakdown (%d total, before bulk): %s",
                skipped,
                dict(skip_reasons),
            )

        # Sort the distinct identities once. Both the warning-sample
        # (first 20) and the ``result.details["unmapped_users"]``
        # full list use the same sorted view — sorting twice was
        # avoidable O(n log n) on large projects.
        sorted_unmapped_users = sorted(unmapped_users)

        if sorted_unmapped_users:
            # Log a sample so the migration log is forensically
            # actionable but doesn't blow up on huge projects. The
            # full sorted list is returned on
            # ``result.details["unmapped_users"]`` for downstream
            # consumers (audit, dashboards) that want the exhaustive
            # list.
            sample = sorted_unmapped_users[:20]
            logger.warning(
                "Watcher migration: %d distinct Jira user(s) not in OP user"
                " mapping (sample of up to 20: %s).%s Add these to the user"
                " mapping or create them in OP, then re-run.",
                len(unmapped_users),
                sample,
                "" if len(unmapped_users) <= 20 else f" {len(unmapped_users) - 20} more elided.",
            )

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

        # Surface the per-reason breakdown alongside the aggregate
        # ``skipped`` so a post-migration audit can answer "why" not
        # just "how many".
        skip_reasons_with_bulk: dict[str, int] = dict(skip_reasons)
        if bulk_skipped:
            skip_reasons_with_bulk["bulk_dedup_or_invalid"] = bulk_skipped
        result.details.update(
            {
                "created": created,
                "skipped": skipped + bulk_skipped,
                "skip_reasons": skip_reasons_with_bulk,
                "unmapped_users": sorted_unmapped_users,
                "unmapped_user_count": len(sorted_unmapped_users),
                "errors": errors,
            },
        )
        result.success = errors == 0
        result.message = f"Watchers created={created}, skipped={skipped + bulk_skipped}, errors={errors}"
        logger.info(result.message)
        return result
