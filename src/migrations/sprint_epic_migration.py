"""Migrate Jira Sprint and Epic Link to OpenProject.

Approach:
- Epic Link: set parent-child hierarchy in OpenProject by assigning `parent_id`
  on child WPs when the Epic issue exists in the mapping (first-class mapping).
- Sprint: ensure CF "Sprint" (text) and store sprint name(s) as comma-separated
  text (fallback when no native sprint entity is modeled in OP).
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


SPRINT_CF_NAME = "Sprint"


@register_entity_types("sprint_epic")
class SprintEpicMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.mappings = config.mappings

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict]:
        """Get current entities for change detection.

        SprintEpicMigration is a transformation-only component that operates on
        already-migrated work packages. It doesn't fetch source data from Jira,
        so this returns an empty list to indicate no changes to detect.

        Args:
            entity_type: Type of entities (should be "sprint_epic")

        Returns:
            Empty list (transformation-only, no source entities)
        """
        return []

    # ---------- Sprint CF helpers ----------
    def _ensure_sprint_cf(self) -> int:
        try:
            cf = self.op_client.get_custom_field_by_name(SPRINT_CF_NAME)
            cf_id = int(cf.get("id")) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:  # noqa: BLE001
            logger.info("Sprint CF not found; will create")

        script = (
            f"cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{SPRINT_CF_NAME}'); "
            f"if !cf; cf = CustomField.new(name: '{SPRINT_CF_NAME}', field_format: 'text', is_required: false, is_for_all: true, type: 'WorkPackageCustomField'); cf.save; end; {{ id: cf.id }}.to_json"
        )
        result = self.op_client.execute_query_to_json_file(script)
        if isinstance(result, dict) and result.get("id"):
            return int(result["id"])
        if isinstance(result, int):
            return result
        try:
            return int(str(result).strip())
        except Exception:  # noqa: BLE001
            logger.debug("Unable to parse Sprint CF ID from result %r", result)
            return 0

    @staticmethod
    def _coerce_sprint_names(sprint_field_value: Any) -> list[str]:
        """Best-effort extract sprint names from Jira field values.

        Handles:
        - list of objects with `name`
        - list of strings
        - single object with `name`
        - single string
        """
        if sprint_field_value is None:
            return []
        out: list[str] = []
        try:
            if isinstance(sprint_field_value, list):
                for item in sprint_field_value:
                    if isinstance(item, str) and item.strip():
                        out.append(item.strip())
                    elif isinstance(item, dict):
                        name = item.get("name")
                        if isinstance(name, str) and name.strip():
                            out.append(name.strip())
                    else:
                        name = getattr(item, "name", None)
                        if isinstance(name, str) and name.strip():
                            out.append(name.strip())
            elif isinstance(sprint_field_value, str):
                if sprint_field_value.strip():
                    out.append(sprint_field_value.strip())
            elif isinstance(sprint_field_value, dict):
                name = sprint_field_value.get("name")
                if isinstance(name, str) and name.strip():
                    out.append(name.strip())
            else:
                name = getattr(sprint_field_value, "name", None)
                if isinstance(name, str) and name.strip():
                    out.append(name.strip())
        except Exception:  # noqa: BLE001
            return []
        return out

    @staticmethod
    def _get_attr_ci(obj: Any, *names: str) -> Any:
        """Get first attribute present on obj, case-insensitive among names."""
        for n in names:
            if hasattr(obj, n):
                return getattr(obj, n)
            # try common variations
            for cand in (n, n.lower(), n.upper(), n.replace(" ", ""), n.replace(" ", "_").lower()):
                if hasattr(obj, cand):
                    return getattr(obj, cand)
        # broad scan as last resort
        try:
            for attr in dir(obj):
                la = attr.lower()
                for n in names:
                    ln = n.lower().replace(" ", "")
                    if all(tok in la for tok in ln.split("_")):
                        return getattr(obj, attr)
        except Exception:  # noqa: BLE001
            return None
        return None

    def _extract(self) -> ComponentResult:
        """Extract Sprint text and Epic parent links keyed by Jira key."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        if not wp_map:
            self.logger.info("No work package mapping present; skipping Sprint/Epic adjustments")
            parent_links_count = 0
            return ComponentResult(
                success=True,
                message="Skipped sprint/epic migration (work package mapping unavailable)",
                details={
                    "sprint_cf_updates": 0,
                    "version_assignments": 0,
                    "parent_links": parent_links_count,
                    "cf_available": False,
                },
            )
        keys = [str(k) for k in wp_map.keys()]
        if not keys:
            return ComponentResult(success=True, data={"sprint": {}, "epic": []})

        issues = self.jira_client.batch_get_issues(keys)

        sprint_by_key: dict[str, list[str]] = {}
        epic_links: list[tuple[str, str]] = []  # (child_key, epic_key)

        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                if not fields:
                    continue
                # Sprint: look for common fields
                sprint_val = None
                for cand in ("sprint", "customfield_10020", "Sprints", "Sprint"):
                    if hasattr(fields, cand):
                        sprint_val = getattr(fields, cand)
                        break
                if sprint_val is None:
                    # wide scan for attr containing 'sprint'
                    sprint_val = self._get_attr_ci(fields, "sprint")
                names = self._coerce_sprint_names(sprint_val)
                if names:
                    sprint_by_key[k] = names

                # Epic Link: common field customfield_10008 or epicLink
                epic_key = None
                for cand in ("epicLink", "customfield_10008", "Epic Link"):
                    if hasattr(fields, cand):
                        epic_key = getattr(fields, cand)
                        break
                if epic_key is None:
                    epic_key = self._get_attr_ci(fields, "epic_link", "epicLink")
                if isinstance(epic_key, str) and epic_key.strip():
                    epic_links.append((k, epic_key.strip()))
            except Exception:  # noqa: BLE001
                continue

        return ComponentResult(success=True, data={"sprint": sprint_by_key, "epic": epic_links})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        sprint_raw: dict[str, list[str]] = data.get("sprint", {}) if isinstance(data, dict) else {}
        epic_pairs: list[tuple[str, str]] = data.get("epic", []) if isinstance(data, dict) else []

        # Normalize sprint names (unique, sorted, joined)
        sprint_text: dict[str, str] = {}
        for key, names in sprint_raw.items():
            uniq = sorted({n.strip() for n in names if n and isinstance(n, str)})
            if uniq:
                sprint_text[key] = ", ".join(uniq)

        # Resolve epic links into child/parent OP IDs
        wp_map = self.mappings.get_mapping("work_package") or {}
        parent_links: list[dict[str, int]] = []
        for child_key, epic_key in epic_pairs:
            child_entry = wp_map.get(child_key) if isinstance(wp_map, dict) else None
            parent_entry = wp_map.get(epic_key) if isinstance(wp_map, dict) else None
            if not (
                isinstance(child_entry, dict)
                and isinstance(parent_entry, dict)
                and child_entry.get("openproject_id")
                and parent_entry.get("openproject_id")
            ):
                continue
            child_id = int(child_entry["openproject_id"])  # type: ignore[arg-type]
            parent_id = int(parent_entry["openproject_id"])  # type: ignore[arg-type]
            if child_id == parent_id:
                continue
            parent_links.append({"id": child_id, "parent_id": parent_id})

        return ComponentResult(success=True, data={"sprint_text": sprint_text, "parent_links": parent_links})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        sprint_text: dict[str, str] = data.get("sprint_text", {}) if isinstance(data, dict) else {}
        parent_links: list[dict[str, int]] = data.get("parent_links", []) if isinstance(data, dict) else []

        updated = 0
        failed = 0

        wp_map = self.mappings.get_mapping("work_package") or {}

        # Apply parent links in batch chunks
        try:
            if parent_links:
                res = self.op_client.batch_update_work_packages(parent_links)
                if isinstance(res, dict):
                    updated += int(res.get("updated", 0))
                    failed += int(res.get("failed", 0))
        except Exception:
            logger.exception("Failed to apply parent links in batch")
            failed += len(parent_links)

        # Assign versions (sprints) when mappings exist
        sprint_mapping = config.mappings.get_mapping("sprint") or {}
        version_updates: list[dict[str, Any]] = []
        for jira_key, text in sprint_text.items():
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            sprint_names = [
                item.strip()
                for item in str(text or "").split(",")
                if item and isinstance(item, str) and item.strip()
            ]
            version_entry = None
            for candidate in sprint_names:
                version_entry = sprint_mapping.get(candidate) or sprint_mapping.get(str(candidate))
                if version_entry:
                    break
            if version_entry and version_entry.get("openproject_id"):
                version_id = int(version_entry.get("openproject_id", 0) or 0)
                if version_id <= 0:
                    continue
                version_updates.append(
                    {
                        "id": int(entry["openproject_id"]),  # type: ignore[arg-type]
                        "version_id": version_id,
                    },
                )

        if version_updates:
            try:
                res = self.op_client.batch_update_work_packages(version_updates)
                if isinstance(res, dict):
                    updated += int(res.get("updated", 0))
                    failed += int(res.get("failed", 0))
            except Exception:
                logger.exception("Failed to assign versions for sprint mapping")
                failed += len(version_updates)

        # Ensure Sprint CF and set values via minimal Rails per record
        cf_id = 0
        try:
            cf_id = self._ensure_sprint_cf()
        except Exception:
            logger.exception("Failed to ensure Sprint custom field")
        if not cf_id:
            self.logger.warning("Sprint custom field unavailable; skipping custom field hydration")
            return ComponentResult(
                success=failed == 0,
                message="Sprint and epic metadata synchronised (custom field unavailable)",
                updated=updated,
                failed=failed,
                details={
                    "sprint_cf_updates": 0,
                    "version_assignments": len(version_updates),
                    "parent_links": len(parent_links),
                    "cf_available": False,
                },
            )

        for jira_key, text in sprint_text.items():
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            try:
                val = str(text).replace("'", "\\'")
                script = (
                    "wp = WorkPackage.find(%d); cf = CustomField.find(%d); "
                    "cv = wp.custom_value_for(cf); if cv; cv.value = '%s'; cv.save; else; wp.custom_field_values = { cf.id => '%s' }; end; wp.save!; true"
                    % (wp_id, cf_id, val, val)
                )
                ok = self.op_client.execute_query(script)
                if ok:
                    updated += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("Failed to set Sprint CF for %s", jira_key)
                failed += 1

        message = "Sprint and epic metadata synchronised"
        return ComponentResult(
            success=failed == 0,
            message=message,
            updated=updated,
            failed=failed,
            details={
                "sprint_cf_updates": len(sprint_text),
                "version_assignments": len(version_updates),
                "parent_links": len(parent_links),
                "cf_available": True,
            },
        )

    def run(self) -> ComponentResult:
        """Execute the sprint/epic migration pipeline."""
        self.logger.info("Starting sprint and epic migration adjustments")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error(
                "Sprint/Epic extraction failed: %s",
                extracted.message or extracted.error,
            )
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error(
                "Sprint/Epic mapping failed: %s",
                mapped.message or mapped.error,
            )
            return mapped

        result = self._load(mapped)
        if result.success:
            self.logger.info(
                "Sprint/Epic migration completed (updated=%s failed=%s)",
                result.updated,
                result.failed,
            )
        else:
            self.logger.error(
                "Sprint/Epic migration encountered failures (failed=%s)",
                result.failed,
            )
        return result
