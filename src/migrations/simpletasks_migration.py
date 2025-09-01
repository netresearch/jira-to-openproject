"""Migrate Simple Tasklists checklist items into OpenProject work packages.

Source: Jira add-on 'Simple Tasklists' (lightweight inline tasks, not Jira subtasks).
Default behavior: render as Markdown checklist in a marked section of WP description.
"""

from __future__ import annotations

from typing import Any

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.mappings import Mappings
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src.config import logger as logger  # type: ignore
    from src import config
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore  # noqa: PLC0415


@register_entity_types("simpletasks")
class SimpleTasksMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        # Import via module so tests can monkeypatch src.mappings.Mappings
        import src.mappings as mappings  # noqa: PLC0415

        self.mappings = mappings.Mappings()
        self.property_key = (
            config.migration_config.get("simpletasks_property_key")
            or "com.topshelf.simple-tasklists"
        )

    def _extract(self) -> ComponentResult:  # noqa: D401
        """Extract checklist data for all migrated issues using work_package mapping."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        extracted: dict[str, Any] = {}
        for jira_key, entry in wp_map.items():
            k = str(jira_key)
            if isinstance(entry, dict) and entry.get("openproject_id"):
                prop = self.jira_client.get_issue_property(k, self.property_key)
                if prop:
                    extracted[k] = prop
        return ComponentResult(success=True, data={"extracted": extracted})

    def _map(self, extracted: ComponentResult) -> ComponentResult:  # noqa: D401
        """Map extracted tasks to Markdown checklist per issue."""
        data = extracted.data or {}
        extracted_map: dict[str, Any] = data.get("extracted", {}) if isinstance(data, dict) else {}

        def to_markdown(tasks: list[dict[str, Any]]) -> str:
            lines: list[str] = []
            for t in tasks or []:
                title = str(t.get("title") or t.get("text") or "").strip()
                checked = bool(t.get("checked") or t.get("done") or False)
                due = t.get("dueDate") or t.get("due")
                labels = t.get("labels") or t.get("tags")
                mandatory = t.get("mandatory")
                suffix_parts: list[str] = []
                if due:
                    suffix_parts.append(f"due: {due}")
                if labels:
                    suffix_parts.append(f"labels: {', '.join(labels) if isinstance(labels, list) else labels}")
                if mandatory:
                    suffix_parts.append("mandatory")
                suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
                lines.append(f"- [{'x' if checked else ' '}] {title}{suffix}")
            return "\n".join(lines)

        markdown_by_issue: dict[str, str] = {}
        for jira_key, payload in extracted_map.items():
            tasks = []
            if isinstance(payload, dict):
                # common shapes: {"tasks": [...]} or the list directly
                if isinstance(payload.get("tasks"), list):
                    tasks = payload["tasks"]  # type: ignore[assignment]
                elif isinstance(payload.get("value"), list):
                    tasks = payload["value"]  # type: ignore[assignment]
            elif isinstance(payload, list):
                tasks = payload
            markdown_by_issue[jira_key] = to_markdown(tasks)

        return ComponentResult(success=True, data={"markdown": markdown_by_issue})

    def _load(self, mapped: ComponentResult) -> ComponentResult:  # noqa: D401
        """Upsert checklist section into WP descriptions."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        data = mapped.data or {}
        md_map: dict[str, str] = data.get("markdown", {}) if isinstance(data, dict) else {}

        updated = 0
        failed = 0
        for jira_key, md in md_map.items():
            if not md:
                continue
            entry = wp_map.get(jira_key)
            if isinstance(entry, dict) and entry.get("openproject_id"):
                wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
                try:
                    if self.op_client.set_checklist_section(wp_id, md):
                        updated += 1
                    else:
                        failed += 1
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to set checklist for %s", jira_key)
                    failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)


