"""Migrate Jira Story Points to OpenProject via numeric CF fallback.

Creates/ensures a WorkPackage custom field "Story Points" (float) and writes the
Jira story points value per issue.

Detection strategy:
- Prefer `fields.storyPoints` if present
- Fallback to common custom field key `fields.customfield_10016`
- As last resort, scan `fields` attributes for a numeric value where the
  attribute name contains both 'story' and 'point' (case-insensitive)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.clients.openproject_client import escape_ruby_single_quoted
from src.config import logger
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

STORY_POINTS_CF_NAME = "Story Points"


@register_entity_types("story_points")
class StoryPointsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict]:
        """Get current entities for change detection.

        StoryPointsMigration is a transformation-only component that operates on
        already-migrated work packages. It doesn't fetch source data from Jira,
        so change detection is not supported.

        Args:
            entity_type: Type of entities

        Raises:
            ValueError: Always, as this migration is transformation-only

        """
        msg = f"{type(self).__name__} is transformation-only and does not support change detection for entity type: {entity_type}"
        raise ValueError(msg)

    @staticmethod
    def _coerce_number(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except Exception:
                return None
        return None

    @staticmethod
    def _extract_story_points_from_fields(fields: Any) -> float | None:
        # Preferred explicit attributes
        for attr in ("storyPoints", "customfield_10016", "story_points"):
            if hasattr(fields, attr):
                num = StoryPointsMigration._coerce_number(getattr(fields, attr, None))
                if num is not None:
                    return num

        # Fallback: scan attributes for name containing both story and point
        try:
            for name in dir(fields):
                lname = name.lower()
                if "story" in lname and "point" in lname:
                    num = StoryPointsMigration._coerce_number(getattr(fields, name, None))
                    if num is not None:
                        return num
        except Exception:
            return None
        return None

    def _extract(self) -> ComponentResult:
        """Extract Jira story points per issue mapped to a WP."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map]
        if not keys:
            return ComponentResult(success=True, data={"sp": {}})

        issues = self._merge_batch_issues(keys)

        sp_by_key: dict[str, float] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                num = self._extract_story_points_from_fields(fields) if fields else None
                if isinstance(num, (int, float)):
                    sp_by_key[k] = float(num)
            except Exception:
                continue
        return ComponentResult(success=True, data={"sp": sp_by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        raw: dict[str, float] = data.get("sp", {}) if isinstance(data, dict) else {}
        # Normalize to strings suitable for CF
        norm: dict[str, str] = {k: (f"{v:g}") for k, v in raw.items()}
        return ComponentResult(success=True, data={"sp_text": norm})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        cf_id = self._ensure_wp_custom_field(STORY_POINTS_CF_NAME, "float")
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        data = mapped.data or {}
        text_by_key: dict[str, str] = data.get("sp_text", {}) if isinstance(data, dict) else {}

        updated = 0
        failed = 0
        projects_with_values: set[int] = set()

        for jira_key, text in text_by_key.items():
            if text is None or text == "0":
                continue
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            # Track project for selective enablement
            project_id = entry.get("openproject_project_id")
            if project_id:
                projects_with_values.add(int(project_id))
            try:
                val = escape_ruby_single_quoted(str(text))
                set_script = (
                    "wp = WorkPackage.find(%d); cf = CustomField.find(%d); "
                    "cv = wp.custom_value_for(cf); if cv; cv.value = '%s'; cv.save; else; wp.custom_field_values = { cf.id => '%s' }; end; wp.save!; true"
                    % (wp_id, cf_id, val, val)
                )
                ok = self.op_client.execute_query(set_script)
                if ok:
                    updated += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("Failed to apply Story Points for %s", jira_key)
                failed += 1

        # Enable CF only for projects that have values
        if projects_with_values:
            self._enable_cf_for_projects(cf_id, projects_with_values, cf_name=STORY_POINTS_CF_NAME)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Story points migration."""
        return self._run_etl_pipeline("Story points")
