"""Migrate Jira Sprint and Epic Link to OpenProject.

Approach:
- Epic Link: set parent-child hierarchy in OpenProject by assigning `parent_id`
  on child WPs when the Epic issue exists in the mapping (first-class mapping).
- Sprint: ensure CF "Sprint" (text) and store sprint name(s) as comma-separated
  text (fallback when no native sprint entity is modeled in OP).
"""

from __future__ import annotations

from typing import Any

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src.config import logger as logger  # type: ignore
    from src import config
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore  # noqa: PLC0415


SPRINT_CF_NAME = "Sprint"


@register_entity_types("sprint_epic")
class SprintEpicMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        import src.mappings as mappings  # noqa: PLC0415

        self.mappings = mappings.Mappings()

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
            "cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '%s'); "
            "if !cf; cf = CustomField.new(name: '%s', field_format: 'text', is_required: false, is_for_all: true, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
            % (SPRINT_CF_NAME, SPRINT_CF_NAME)
        )
        cf_id = self.op_client.execute_query(script)
        return int(cf_id) if isinstance(cf_id, int) else int(cf_id or 0)

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

    def _extract(self) -> ComponentResult:  # noqa: D401
        """Extract Sprint text and Epic parent links keyed by Jira key."""
        wp_map = self.mappings.get_mapping("work_package") or {}
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

    def _map(self, extracted: ComponentResult) -> ComponentResult:  # noqa: D401
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

    def _load(self, mapped: ComponentResult) -> ComponentResult:  # noqa: D401
        data = mapped.data or {}
        sprint_text: dict[str, str] = data.get("sprint_text", {}) if isinstance(data, dict) else {}
        parent_links: list[dict[str, int]] = data.get("parent_links", []) if isinstance(data, dict) else []

        updated = 0
        failed = 0

        # Apply parent links in batch chunks
        try:
            if parent_links:
                res = self.op_client.batch_update_work_packages(parent_links)
                if isinstance(res, dict):
                    updated += int(res.get("updated", 0))
                    failed += int(res.get("failed", 0))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to apply parent links in batch")
            failed += len(parent_links)

        # Ensure Sprint CF and set values via minimal Rails per record
        cf_id = 0
        try:
            cf_id = self._ensure_sprint_cf()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to ensure Sprint custom field")
        if not cf_id:
            return ComponentResult(success=False, failed=failed + len(sprint_text))

        wp_map = self.mappings.get_mapping("work_package") or {}
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
            except Exception:  # noqa: BLE001
                logger.exception("Failed to set Sprint CF for %s", jira_key)
                failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)



