"""Migrate Jira components to OpenProject Categories and assign to work packages.

Phase 7d converts the work-package side of this migration to the typed
pipeline: issues are parsed at the boundary via
:meth:`JiraIssueFields.from_issue_any`, and the legacy ``wp_map``
``dict | int`` ladder is normalised through
:meth:`WorkPackageMappingEntry.from_legacy`. The ``project`` mapping
ladder uses a separate, unrelated polymorphic shape and is intentionally
left as-is — phase 7 targets the ``wp_map`` polymorphism only.
"""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry
from src.models.jira import JiraIssueFields


@register_entity_types("components")
class ComponentsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on work package mappings
        rather than fetching directly from Jira. It operates on already-migrated
        work packages to extract and assign components to Categories.

        Args:
            entity_type: The type of entities requested (e.g., "components")

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "ComponentsMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on work package mappings."
        )
        raise ValueError(msg)

    def _extract(self) -> ComponentResult:
        """Collect component names per Jira project from migrated issues.

        Issues are normalised through :meth:`JiraIssueFields.from_issue_any`
        so the rest of the pipeline reads ``fields.components`` as a typed
        list of :class:`JiraComponentRef`.
        """
        wp_map = self.mappings.get_mapping("work_package") or {}
        # Production wp_map is keyed by str(jira_id) (numeric) outer with
        # the human-readable ``jira_key`` stored inside. Prefer the inner
        # ``jira_key``; fall back to the outer key for legacy/test layouts.
        jira_keys: list[str] = []
        for outer_key, raw_entry in wp_map.items():
            inner_jira_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
            jira_keys.append(str(inner_jira_key or outer_key))
        if not jira_keys:
            return ComponentResult(success=True, extracted=0, data={"by_project": {}})

        issues: dict[str, Any] = self._merge_batch_issues(jira_keys)

        by_project: dict[str, set[str]] = {}
        for key, issue in issues.items():
            pj = self._issue_project_key(key)
            try:
                fields = JiraIssueFields.from_issue_any(issue)
            except Exception:
                continue
            for comp in fields.components:
                name = comp.name
                if name and name.strip():
                    by_project.setdefault(pj, set()).add(name.strip())

        materialized = {k: sorted(v) for k, v in by_project.items() if v}
        # Cache issues for reuse in _load() to avoid second Jira API call
        return ComponentResult(
            success=True,
            extracted=sum(len(v) for v in materialized.values()),
            data={"by_project": materialized, "issues": issues},
        )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        by_project: dict[str, list[str]] = data.get("by_project", {}) if isinstance(data, dict) else {}
        # Preserve cached issues from extract phase for reuse in load phase
        cached_issues = data.get("issues", {}) if isinstance(data, dict) else {}
        if not by_project:
            return ComponentResult(success=True, created=0, data={"category_map": {}, "issues": cached_issues})

        proj_map = self.mappings.get_mapping("project") or {}
        op_project_ids: dict[str, int] = {}
        for jira_key in by_project:
            entry = proj_map.get(jira_key)
            pid = None
            if isinstance(entry, dict):
                pid = entry.get("openproject_id")
            elif isinstance(entry, int):
                pid = entry
            if pid:
                try:
                    op_project_ids[jira_key] = int(pid)
                except Exception:
                    continue

        pids = sorted(set(op_project_ids.values()))
        existing: list[dict[str, Any]] = []
        if pids:
            try:
                query = f"Category.where(project_id: [{', '.join(str(x) for x in pids)}]).select(:id,:name,:project_id)"
                res = self.op_client.execute_json_query(query)
                if isinstance(res, list):
                    existing = [r for r in res if isinstance(r, dict)]
            except Exception:
                logger.exception("Failed to list existing Categories")
                existing = []

        by_pid_name_to_id: dict[str, dict[str, int]] = {}
        for r in existing:
            try:
                pid = int(r.get("project_id"))
                name = str(r.get("name"))
                cid = int(r.get("id"))
                by_pid_name_to_id.setdefault(str(pid), {})[name] = cid
            except Exception:
                continue

        to_create: list[dict[str, Any]] = []
        for jproj, names in by_project.items():
            pid = op_project_ids.get(jproj)
            if not pid:
                continue
            existing_for = by_pid_name_to_id.get(str(pid), {})
            for name in names:
                if name not in existing_for:
                    to_create.append({"name": name, "project_id": int(pid)})

        created = 0
        if to_create:
            try:
                res = self.op_client.bulk_create_records("Category", to_create)
                created = int(res.get("created_count", 0)) if isinstance(res, dict) else len(to_create)
            except Exception:
                logger.exception("Failed to create Categories in bulk")

            # Refresh map
            try:
                query = f"Category.where(project_id: [{', '.join(str(x) for x in pids)}]).select(:id,:name,:project_id)"
                res2 = self.op_client.execute_json_query(query)
                if isinstance(res2, list):
                    by_pid_name_to_id.clear()
                    for r in res2:
                        try:
                            pid = int(r.get("project_id"))
                            name = str(r.get("name"))
                            cid = int(r.get("id"))
                            by_pid_name_to_id.setdefault(str(pid), {})[name] = cid
                        except Exception:
                            continue
            except Exception:
                logger.exception("Failed to refresh Category map after creation")

        # Persist mapping
        try:
            self.mappings.set_mapping("category", by_pid_name_to_id)
        except Exception:
            logger.exception("Failed to persist category mapping")

        return ComponentResult(
            success=True,
            created=created,
            data={"category_map": by_pid_name_to_id, "issues": cached_issues},
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        category_map: dict[str, dict[str, int]] = (mapped.data or {}).get("category_map", {}) if mapped.data else {}
        if not category_map:
            return ComponentResult(success=True, updated=0)

        wp_map = self.mappings.get_mapping("work_package") or {}
        proj_map = self.mappings.get_mapping("project") or {}

        # Use cached issues from extract phase to avoid second Jira API call.
        # Cache keys match the ``jira_key`` form used during ``_extract``
        # (inner ``jira_key`` when present, outer key otherwise).
        issues: dict[str, Any] = (mapped.data or {}).get("issues", {}) if mapped.data else {}
        if not issues:
            logger.warning("No cached issues available, fetching from Jira (this may cause timeout)")
            fallback_keys: list[str] = []
            for outer_key, raw_entry in wp_map.items():
                inner_jira_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
                fallback_keys.append(str(inner_jira_key or outer_key))
            issues = self._merge_batch_issues(fallback_keys)

        updates: list[dict[str, Any]] = []
        failed = 0
        for outer_key, raw_entry in wp_map.items():
            inner_jira_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
            jira_key = str(inner_jira_key or outer_key)
            try:
                entry = WorkPackageMappingEntry.from_legacy(jira_key, raw_entry)
            except ValueError:
                continue
            wp_id = int(entry.openproject_id)

            issue = issues.get(jira_key)
            if not issue:
                continue
            try:
                fields = JiraIssueFields.from_issue_any(issue)
            except Exception:
                failed += 1
                continue
            if not fields.components:
                continue
            # Choose first component name deterministically
            name = fields.components[0].name
            if not name:
                continue

            # ``project`` mapping uses an unrelated polymorphic shape
            # (dict/int) that is not the wp_map polymorphism; phase 7
            # targets ``wp_map`` only, so leave the ladder in place.
            jproj = self._issue_project_key(jira_key)
            proj_entry = proj_map.get(jproj)
            op_pid: int | None = None
            if isinstance(proj_entry, dict):
                pid_val = proj_entry.get("openproject_id")
                if pid_val is not None:
                    try:
                        op_pid = int(pid_val)
                    except (
                        TypeError,
                        ValueError,
                    ):
                        op_pid = None
            elif isinstance(proj_entry, int):
                op_pid = proj_entry
            if not op_pid:
                continue

            cat_id = category_map.get(str(op_pid), {}).get(str(name))
            if not cat_id:
                continue
            updates.append({"id": wp_id, "category_id": int(cat_id)})

        if not updates:
            return ComponentResult(success=failed == 0, updated=0, failed=failed)

        updated = 0
        try:
            res = self.op_client.batch_update_work_packages(updates)
            updated = int(res.get("updated", 0)) if isinstance(res, dict) else len(updates)
        except Exception:
            logger.exception("Failed to batch update category_id on work packages")
            failed += len(updates)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Execute component migration pipeline."""
        self.logger.info("Starting components migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error("Components extraction failed: %s", extracted.message or extracted.error)
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error("Components mapping failed: %s", mapped.message or mapped.error)
            return mapped

        loaded = self._load(mapped)
        if loaded.success:
            self.logger.info("Components migration completed (updated=%s, failed=%s)", loaded.updated, loaded.failed)
        else:
            self.logger.error("Components migration encountered failures (failed=%s)", loaded.failed)
        return loaded
