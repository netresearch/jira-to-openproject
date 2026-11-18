# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Simple Tasklists checklist items into OpenProject work packages.

Source: Jira add-on 'Simple Tasklists' (lightweight inline tasks, not Jira subtasks).
Default behavior: render as Markdown checklist in a marked section of WP description.

Note on dict access patterns kept here
--------------------------------------
The ``payload`` returned by ``jira_client.get_issue_property`` carries
the raw Simple Tasklists add-on shape — a tenant-specific JSON blob
whose contents are defined by the add-on (and vary by add-on version
and configuration). It is not a Jira-modelled value, so we keep direct
dict access on it. The polymorphic work-package mapping is normalised
through :class:`WorkPackageMappingEntry.from_legacy`.
"""

from __future__ import annotations

import re
from decimal import Decimal
from functools import cached_property
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.description_tasks import description_tasks
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.jira.sidebar_tasks import sidebar_tasks
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry


@register_entity_types("simpletasks")
class SimpleTasksMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        # Import via module so tests can monkeypatch src.mappings.Mappings

        self.property_key = config.migration_config.get("simpletasks_property_key") or "com.topshelf.simple-tasklists"

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on issue properties
        rather than fetching directly from Jira. It operates on already-fetched
        work package data.

        Args:
            entity_type: The type of entities requested

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "SimpleTasksMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on data from other migrations."
        )
        raise ValueError(msg)

    @cached_property
    def _task_count_field(self) -> str | None:
        for field in self.jira_client.get_custom_fields():
            if field.get("schema", {}).get("custom") == "com.topshelfsolution.simpletasklists:all-tasks-count-cftype":
                return field["id"]
        return None

    def _issue_tasks(self, key: str) -> tuple[list[dict], int]:
        count_field = self._task_count_field
        fields = f"description,project,{count_field}" if count_field else "description"
        raw = self.jira_client.jira.issue(key, fields=fields, expand="renderedFields").raw
        tasks = description_tasks(key, raw)
        if not count_field or raw["fields"].get(count_field) is None:
            return tasks, 0
        count = Decimal(str(raw["fields"][count_field]))
        if not count.is_finite() or count != count.to_integral_value() or count < len(tasks):
            msg = "Inconsistent source task count."
            raise ValueError(msg)
        total = int(count)
        if total > len(tasks) and raw["fields"]["project"].get("projectTypeKey") == "software":
            rows = self.jira_client.get_issue_simpletasks(key)
            tasks.extend(sidebar_tasks(raw, tasks, rows, total))
        return tasks, total - len(tasks)

    def _extract(self) -> ComponentResult:
        """Extract checklist data for all migrated issues using work_package mapping."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        extracted: dict[str, Any] = {}
        held = 0
        for jira_key, raw_entry in wp_map.items():
            k = str(jira_key)
            try:
                WorkPackageMappingEntry.from_legacy(k, raw_entry)
            except ValueError:
                continue
            key = self._inner_jira_key(k, raw_entry)
            if not key:
                msg = "Cannot identify the Jira issue for a mapped work package."
                raise ValueError(msg)
            prop = self.jira_client.get_issue_property(key, self.property_key)
            if prop is not None:
                extracted[k] = prop
            else:
                tasks, unknown_origin = self._issue_tasks(key)
                held += unknown_origin
                if tasks:
                    extracted[k] = [{**task, "title": self._literal_title(task["title"])} for task in tasks]
        return ComponentResult(success=True, data={"extracted": extracted}, details={"unverified_task_audience": held})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Map extracted tasks to Markdown checklist per issue."""
        data = extracted.data or {}
        extracted_map: dict[str, Any] = data.get("extracted", {}) if isinstance(data, dict) else {}

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
            markdown_by_issue[jira_key] = "\n".join(self._checklist_row(t) for t in tasks)

        return ComponentResult(success=True, data={"markdown": markdown_by_issue})

    @staticmethod
    def _literal_title(title: str) -> str:
        """Keep markup, mentions and line breaks in source labels inside one Markdown code span."""
        title = title.replace("\r", " ").replace("\n", " ")
        fence = "`" * (1 + max(map(len, re.findall(r"`+", title)), default=0))
        return f"{fence} {title} {fence}"

    @staticmethod
    def _checklist_row(t: dict[str, Any]) -> str:
        """Format one task with its completion state and optional details."""
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
        return f"- [{'x' if checked else ' '}] {title}{suffix}"

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Upsert checklist section into WP descriptions."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        data = mapped.data or {}
        md_map: dict[str, str] = data.get("markdown", {}) if isinstance(data, dict) else {}

        updated = 0
        failed = 0
        for jira_key, md in md_map.items():
            if not md:
                continue
            raw_entry = wp_map.get(jira_key)
            if raw_entry is None:
                continue
            try:
                entry = WorkPackageMappingEntry.from_legacy(jira_key, raw_entry)
            except ValueError:
                continue
            wp_id = int(entry.openproject_id)
            try:
                if self.op_client.set_checklist_section(wp_id, md):
                    updated += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("Failed to set checklist for %s", jira_key)
                failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run simpletasks migration using ETL pattern."""
        logger.info("Starting simpletasks migration...")
        try:
            extracted = self._extract()
            if not extracted.success:
                return ComponentResult(
                    success=False,
                    message="Simpletasks extraction failed",
                    errors=extracted.errors or ["simpletasks extraction failed"],
                )

            mapped = self._map(extracted)
            if not mapped.success:
                return ComponentResult(
                    success=False,
                    message="Simpletasks mapping failed",
                    errors=mapped.errors or ["simpletasks mapping failed"],
                )

            result = self._load(mapped)
            held = extracted.details.get("unverified_task_audience", 0)
            if held:
                result.add_warning(f"Withheld {held} task(s) with an unverified source audience.")
                result.details["unverified_task_audience"] = held
            logger.info(
                "Simpletasks migration completed: success=%s, updated=%s, failed=%s",
                result.success,
                result.updated,
                result.failed,
            )
            return result
        except Exception as e:
            logger.exception("Simpletasks migration failed")
            return ComponentResult(
                success=False,
                message=f"Simpletasks migration failed: {e}",
                errors=[str(e)],
            )
