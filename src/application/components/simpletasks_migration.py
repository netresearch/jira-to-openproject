# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Render audience-confirmed issue tasks and retain their source identities and editing metadata."""

from __future__ import annotations

import re
from decimal import Decimal
from functools import cached_property
from itertools import batched
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.issue_security import permits_unrestricted_result, unrestricted_project_jql
from src.application.components.project_named_records import project_id
from src.infrastructure.jira.description_tasks import description_tasks
from src.infrastructure.jira.issue_pagination import iter_issue_pages
from src.infrastructure.jira.jira_issue_service import fetch_issue_page_with_retry
from src.infrastructure.jira.sidebar_tasks import issue_task_records
from src.infrastructure.openproject.tasklist_origins import apply_tasklists
from src.models import ComponentResult, WorkPackageMappingEntry


def _property_ids(payload: Any) -> list[str | None]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        payload = payload.get("tasks", payload.get("value"))
    if not isinstance(payload, list):
        msg = "Unrecognised Jira tasklist property"
        raise ValueError(msg)
    return [str(task["id"]) if isinstance(task, dict) and task.get("id") is not None else None for task in payload]


def _checklist_row(task: dict) -> str:
    title = task["title"].replace("\r", " ").replace("\n", " ")
    fence = "`" * (1 + max(map(len, re.findall(r"`+", title)), default=0))
    mandatory = " (mandatory)" if task.get("mandatory") else ""
    return f"- [{'x' if task['checked'] else ' '}] {fence} {title} {fence}{mandatory}"


@register_entity_types("simpletasks")
class SimpleTasksMigration(BaseMigration):
    """Transfer ordinary task origins through the selected projects' verified issue mappings."""

    @cached_property
    def _task_count_field(self) -> str | None:
        for field in self.jira_client.get_custom_fields():
            if field.get("schema", {}).get("custom") == "com.topshelfsolution.simpletasklists:all-tasks-count-cftype":
                return field["id"]
        return None

    @cached_property
    def _server_tasklist(self) -> bool:
        """Cloud supports personal tasks. A sidebar is ordinary only for the Server/DC add-on."""
        info = self.jira_client.jira.server_info()
        deployment = str(info.get("deploymentType", "")).casefold().replace(" ", "")
        return deployment in {"server", "datacenter"}

    def _confirmed_tasks(self, raw: dict) -> tuple[list[dict], int]:
        key = raw["key"]
        tasks = [{**t, "origin": "description"} for t in description_tasks(key, raw)]
        count = raw["fields"].get(self._task_count_field)
        held = 0
        if count is not None:
            value = Decimal(str(count))
            if not value.is_finite() or value != value.to_integral_value() or value < len(tasks):
                msg = "Inconsistent source task count."
                raise ValueError(msg)
            total = int(value)
            if total and raw["fields"]["project"]["projectTypeKey"] == "software" and self._server_tasklist:
                tasks = issue_task_records(raw, tasks, self.jira_client.get_issue_simpletasks(key), total)
            held = total - len(tasks)
        prop = config.migration_config.get("simpletasks_property_key") or "com.topshelf.simple-tasklists"
        ids = _property_ids(self.jira_client.get_issue_property(key, prop))
        confirmed = {t["id"] for t in tasks}
        # Properties can aggregate comment or private-field tasks too. Never copy their content by itself.
        return tasks, max(held, sum(identifier not in confirmed for identifier in ids))

    def _targets(self, selected: set[str]) -> dict:
        by_project: dict[str, dict] = {}
        for origin, value in (self.mappings.get_mapping("work_package") or {}).items():
            key = self._inner_jira_key(str(origin), value)
            if not key:
                msg = "Unidentified mapped Jira issue for tasklists"
                raise ValueError(msg)
            project = self._issue_project_key(key)
            if selected and project not in selected:
                continue
            targets = by_project.setdefault(project, {})
            if key in targets:
                msg = "Duplicate Jira work-package mapping for tasklists"
                raise ValueError(msg)
            entry = WorkPackageMappingEntry.from_legacy(key, value)
            targets[key] = (int(entry.openproject_id), str(origin) if str(origin).isdecimal() else None)
        return by_project

    def _project_rows(self, project: str, targets: dict, pid: int) -> tuple[dict, int, int]:
        jql = f"{unrestricted_project_jql(project)} ORDER BY id"

        def fetch(offset: int, size: int) -> list:
            return fetch_issue_page_with_retry(
                self.jira_client,
                jql=jql,
                start_at=offset,
                max_results=size,
                fields="project,security",
                expand=None,
                project_key=project,
            )

        rows = []
        held = 0
        source_pid = None
        for issue in iter_issue_pages(project, fetch, 100):
            if issue.key not in targets or not permits_unrestricted_result(issue):
                continue
            project_ref = issue.raw["fields"]["project"]
            if project_ref.get("projectTypeKey") not in {"software", "business"}:
                continue
            source_id = str(project_ref["id"])
            if project_ref["key"] != project or source_pid not in (None, source_id):
                msg = "Jira tasklist project identity mismatch"
                raise ValueError(msg)
            source_pid = source_id
            row, unknown = self._issue_row(issue, targets[issue.key])
            rows.append(row)
            held += unknown
        return (
            {"project": pid, "key": project, "source_id": source_pid, "base": self.jira_client.base_url, "rows": rows},
            held,
            len(targets) - len(rows),
        )

    def _issue_row(self, issue: Any, target: tuple[int, str | None]) -> tuple[dict, int]:
        fields = "project,security,description"
        if self._task_count_field:
            fields += "," + self._task_count_field
        raw = self.jira_client.jira.issue(issue.key, fields=fields, expand="renderedFields").raw
        target_id, source_id = target
        if (
            raw.get("key") != issue.key
            or str(raw["id"]) != str(issue.id)
            or source_id not in (None, str(issue.id))
            or raw["fields"]["project"]["id"] != issue.raw["fields"]["project"]["id"]
            or raw["fields"]["project"].get("projectTypeKey") != issue.raw["fields"]["project"].get("projectTypeKey")
            or not permits_unrestricted_result(raw)
        ):
            msg = "Jira tasklist issue identity or visibility changed"
            raise ValueError(msg)
        tasks, held = self._confirmed_tasks(raw)
        return {
            "wp": target_id,
            "id": str(issue.id),
            "key": issue.key,
            "tasks": tasks,
            "markdown": "\n".join(_checklist_row(t) for t in tasks),
        }, held

    def _extract(self) -> ComponentResult:
        configured = config.jira_config.get("projects") or []
        selected = {k.strip() for k in (configured.split(",") if isinstance(configured, str) else configured)}
        mappings = self.mappings.get_mapping("project") or {}
        projects = []
        held = unavailable = 0
        for key, targets in self._targets(selected).items():
            pid = project_id(mappings.get(key))
            if not pid:
                msg = "Missing source project mapping for Jira tasklists"
                raise ValueError(msg)
            scope, unknown, missing = self._project_rows(key, targets, pid)
            if scope["rows"]:
                projects.append(scope)
            held += unknown
            unavailable += missing
        return ComponentResult(
            success=True, data=projects, details={"unverified_task_audience": held, "unconfirmed_issues": unavailable}
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        counts = {"updated": 0, "tasks": 0}
        for project in mapped.data or []:
            for batch in batched(project["rows"], 100, strict=False):
                actual = apply_tasklists(self.op_client, {**project, "rows": list(batch)})
                for key in counts:
                    counts[key] += actual[key]
        details = {**mapped.details, **counts}
        warnings = []
        if n := details["unverified_task_audience"]:
            warnings.append(f"Withheld {n} task(s) with an unverified source audience.")
        if n := details["unconfirmed_issues"]:
            warnings.append(f"Jira could not confirm {n} mapped issue(s) for ordinary checklist import.")
        return ComponentResult(success=True, updated=counts["updated"], details=details, warnings=warnings)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Use the component's own source and target reconciliation on every run."""
        msg = "Tasklists validate their source and destination in the normal migration."
        raise ValueError(msg)

    def run(self) -> ComponentResult:
        """Reconcile Jira tasks and their source metadata."""
        return self._run_etl_pipeline("Simpletasks")

    def run_with_change_detection(self, entity_type: str | None = None) -> ComponentResult:
        """Task edits can occur independently of Jira's issue update timestamp."""
        return self.run()
