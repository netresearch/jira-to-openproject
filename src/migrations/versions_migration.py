"""Migrate Jira fixVersions to OpenProject Versions and assign to work packages.

Strategy:
- Extract: collect fixVersion names per Jira project from issues in work_package mapping.
- Map: ensure Versions exist per OP project (create missing via bulk_create_records('Version')).
- Load: set work package `version_id` from first fixVersion, idempotently, in batch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

try:
    from src import config
    from src.config import logger  # type: ignore
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore


@register_entity_types("versions")
class VersionsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.mappings = config.mappings

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on work package mappings
        rather than fetching directly from Jira. It operates on already-migrated
        work packages to extract and assign fixVersions to Versions.

        Args:
            entity_type: The type of entities requested (e.g., "versions")

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "VersionsMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on work package mappings."
        )
        raise ValueError(msg)

    @staticmethod
    def _issue_project_key(issue_key: str) -> str:
        try:
            return str(issue_key).split("-", 1)[0]
        except Exception:
            return str(issue_key)

    def _extract(self) -> ComponentResult:
        """Extract fixVersion names per Jira project from known work package issues."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        jira_keys = [str(k) for k in wp_map.keys()]
        if not jira_keys:
            return ComponentResult(success=True, extracted=0, data={"by_project": {}})

        issues = {}
        try:
            batch_get = getattr(self.jira_client, "batch_get_issues", None)
            if callable(batch_get):
                issues = batch_get(jira_keys)
        except Exception:
            logger.exception("Failed to batch-get Jira issues for versions extraction")
            issues = {}

        by_project: dict[str, set[str]] = {}
        for key, issue in issues.items():
            pj = self._issue_project_key(key)
            try:
                fields = getattr(issue, "fields", None)
                fxs = getattr(fields, "fixVersions", None)
                if not isinstance(fxs, list) or not fxs:
                    continue
                for v in fxs:
                    name = None
                    if isinstance(v, dict):
                        name = v.get("name")
                    else:
                        name = getattr(v, "name", None)
                    if name and isinstance(name, str) and name.strip():
                        by_project.setdefault(pj, set()).add(name.strip())
            except Exception:  # noqa: BLE001
                continue

        materialized = {k: sorted(list(v)) for k, v in by_project.items() if v}
        return ComponentResult(success=True, extracted=sum(len(v) for v in materialized.values()), data={"by_project": materialized})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        by_project: dict[str, list[str]] = data.get("by_project", {}) if isinstance(data, dict) else {}
        if not by_project:
            return ComponentResult(success=True, created=0, data={"version_map": {}})

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
                query = f"Version.where(project_id: [{', '.join(str(x) for x in pids)}]).select(:id,:name,:project_id)"
                res = self.op_client.execute_json_query(query)
                if isinstance(res, list):
                    existing = [r for r in res if isinstance(r, dict)]
            except Exception:
                logger.exception("Failed to list existing Versions")
                existing = []

        by_pid_name_to_id: dict[str, dict[str, int]] = {}
        for r in existing:
            try:
                pid = int(r.get("project_id"))
                name = str(r.get("name"))
                vid = int(r.get("id"))
                by_pid_name_to_id.setdefault(str(pid), {})[name] = vid
            except Exception:  # noqa: BLE001
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
                res = self.op_client.bulk_create_records("Version", to_create)
                created = int(res.get("created_count", 0)) if isinstance(res, dict) else len(to_create)
            except Exception:
                logger.exception("Failed to create Versions in bulk")

            # Refresh existing map after creation
            try:
                query = f"Version.where(project_id: [{', '.join(str(x) for x in pids)}]).select(:id,:name,:project_id)"
                res2 = self.op_client.execute_json_query(query)
                if isinstance(res2, list):
                    by_pid_name_to_id.clear()
                    for r in res2:
                        try:
                            pid = int(r.get("project_id"))
                            name = str(r.get("name"))
                            vid = int(r.get("id"))
                            by_pid_name_to_id.setdefault(str(pid), {})[name] = vid
                        except Exception:  # noqa: BLE001
                            continue
            except Exception:
                logger.exception("Failed to refresh Version map after creation")

        # Persist
        try:
            self.mappings.set_mapping("version", by_pid_name_to_id)
        except Exception:
            logger.exception("Failed to persist version mapping")

        return ComponentResult(success=True, created=created, data={"version_map": by_pid_name_to_id})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        version_map: dict[str, dict[str, int]] = (mapped.data or {}).get("version_map", {}) if mapped.data else {}
        if not version_map:
            return ComponentResult(success=True, updated=0)

        wp_map = self.mappings.get_mapping("work_package") or {}
        proj_map = self.mappings.get_mapping("project") or {}

        # Build Jira issues index again
        jira_keys = [str(k) for k in wp_map.keys()]
        issues: dict[str, Any] = {}
        try:
            batch_get = getattr(self.jira_client, "batch_get_issues", None)
            if callable(batch_get):
                issues = batch_get(jira_keys)
        except Exception:
            logger.exception("Failed to batch-get Jira issues for version assignment")
            issues = {}

        updates: list[dict[str, Any]] = []
        failed = 0
        for key, wp_entry in wp_map.items():
            try:
                wp_id = None
                if isinstance(wp_entry, dict):
                    wp_id = wp_entry.get("openproject_id")
                elif isinstance(wp_entry, int):
                    wp_id = wp_entry
                if not wp_id:
                    continue

                issue = issues.get(key)
                if not issue:
                    continue
                fields = getattr(issue, "fields", None)
                fxs = getattr(fields, "fixVersions", None)
                if not isinstance(fxs, list) or not fxs:
                    continue
                # Choose first fixVersion name deterministically
                first = fxs[0]
                name = None
                if isinstance(first, dict):
                    name = first.get("name")
                else:
                    name = getattr(first, "name", None)
                if not name:
                    continue

                jproj = self._issue_project_key(key)
                proj_entry = proj_map.get(jproj)
                op_pid = None
                if isinstance(proj_entry, dict):
                    op_pid = proj_entry.get("openproject_id")
                elif isinstance(proj_entry, int):
                    op_pid = proj_entry
                if not op_pid:
                    continue

                ver_id = version_map.get(str(int(op_pid)), {}).get(str(name))
                if not ver_id:
                    continue
                updates.append({"id": int(wp_id), "version_id": int(ver_id)})
            except Exception:  # noqa: BLE001
                failed += 1

        if not updates:
            return ComponentResult(success=failed == 0, updated=0, failed=failed)

        updated = 0
        try:
            res = self.op_client.batch_update_work_packages(updates)
            updated = int(res.get("updated", 0)) if isinstance(res, dict) else len(updates)
        except Exception:
            logger.exception("Failed to batch update version_id on work packages")
            failed += len(updates)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)
    def run(self) -> ComponentResult:
        """Execute versions migration pipeline."""
        self.logger.info("Starting versions migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error("Versions extraction failed: %s", extracted.message or extracted.error)
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error("Versions mapping failed: %s", mapped.message or mapped.error)
            return mapped

        loaded = self._load(mapped)
        if loaded.success:
            self.logger.info("Versions migration completed (updated=%s, failed=%s)", loaded.updated, loaded.failed)
        else:
            self.logger.error("Versions migration encountered failures (failed=%s)", loaded.failed)
        return loaded
