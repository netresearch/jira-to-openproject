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
from src.application.components import level_issues
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.issue_security import permits_unrestricted_result, unrestricted_project_jql
from src.application.components.project_named_records import project_id
from src.application.components.security_level_projects_migration import LevelProjectError
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


class _LevelChangedError(Exception):
    """A level issue whose re-read shows another level than it was selected for."""


@register_entity_types("simpletasks")
class SimpleTasksMigration(BaseMigration):
    """Transfer ordinary task origins through the selected projects' verified issue mappings."""

    @cached_property
    def _task_fields(self) -> dict[str, str]:
        fields = {}
        for field in self.jira_client.get_custom_fields():
            kind = (field.get("schema") or {}).get("custom") or ""
            if kind.startswith("com.topshelfsolution.simpletasklists:"):
                fields[kind] = field["id"]
        return fields

    @property
    def _task_count_field(self) -> str | None:
        return self._task_fields.get("com.topshelfsolution.simpletasklists:all-tasks-count-cftype")

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
        fields = "project,security,description"
        if self._task_count_field:
            fields += "," + self._task_count_field

        def fetch(offset: int, size: int) -> list:
            return fetch_issue_page_with_retry(
                self.jira_client,
                jql=jql,
                start_at=offset,
                max_results=size,
                fields=fields,
                expand=None,
                project_key=project,
            )

        rows = []
        held = 0
        source_pid = None
        confirmed = 0
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
            confirmed += 1
            row, unknown = self._issue_row(issue, targets[issue.key])
            if row:
                rows.append(row)
            held += unknown
        return (
            {"project": pid, "key": project, "source_id": source_pid, "base": self.jira_client.base_url, "rows": rows},
            held,
            len(targets) - confirmed,
        )

    def _issue_row(
        self, issue: Any, target: tuple[int, str | None], level_id: str | None = None
    ) -> tuple[dict | None, int]:
        target_id, source_id = target
        if source_id is not None and source_id != str(issue.id):
            msg = "Mapped Jira tasklist issue ID changed"
            raise ValueError(msg)
        ref = {"wp": target_id, "id": str(issue.id), "key": issue.key}
        source = issue.raw["fields"]
        count = source.get(self._task_count_field)
        explicit_property = bool(config.migration_config.get("simpletasks_property_key"))
        if not explicit_property and "{task" not in (source.get("description") or ""):
            if count is None:
                return None, 0  # Missing app data does not prove that a previously imported list is empty.
            if Decimal(str(count)) == 0:
                return {**ref, "tasks": [], "markdown": ""}, 0
        fields = "project,security,description"
        if self._task_count_field:
            fields += "," + self._task_count_field
        raw = self.jira_client.jira.issue(issue.key, fields=fields, expand="renderedFields").raw
        if (
            raw.get("key") != issue.key
            or str(raw["id"]) != str(issue.id)
            or raw["fields"]["project"]["id"] != issue.raw["fields"]["project"]["id"]
            or raw["fields"]["project"].get("projectTypeKey") != issue.raw["fields"]["project"].get("projectTypeKey")
            or (level_id is None and not permits_unrestricted_result(raw))
        ):
            msg = "Jira tasklist issue identity or visibility changed"
            raise ValueError(msg)
        if level_id is not None and level_issues.issue_level_id(raw) != level_id:
            raise _LevelChangedError(issue.key)
        tasks, held = self._confirmed_tasks(raw)
        return {
            **ref,
            "tasks": tasks,
            "markdown": "\n".join(_checklist_row(t) for t in tasks),
        }, held

    def _extract(self) -> ComponentResult:
        if not self._task_fields and not config.migration_config.get("simpletasks_property_key"):
            return ComponentResult(
                success=True,
                data=[],
                details={"source_tasklist_detected": False, "unverified_task_audience": 0, "unconfirmed_issues": 0},
            )
        selected = set(config.selected_jira_projects())
        mappings = self.mappings.get_mapping("project") or {}
        projects = []
        held = unavailable = 0
        for key, targets in self._without_level_issues(self._targets(selected)).items():
            pid = project_id(mappings.get(key))
            if not pid:
                msg = "Missing source project mapping for Jira tasklists"
                raise ValueError(msg)
            scope, unknown, missing = self._project_rows(key, targets, pid)
            if scope["rows"]:
                projects.append(scope)
            held += unknown
            unavailable += missing
        for scope, unknown, missing in self._level_projects():
            if scope["rows"]:
                projects.append(scope)
            held += unknown
            unavailable += missing
        return ComponentResult(
            success=True, data=projects, details={"unverified_task_audience": held, "unconfirmed_issues": unavailable}
        )

    def _without_level_issues(self, by_project: dict) -> dict:
        """With the level switch on, no issue of a level search or of the archive keeps an ordinary target."""
        level = getattr(self, "_level", None)
        if level is None:
            return by_project
        flat = {key: wp for targets in by_project.values() for key, (wp, _) in targets.items()}
        kept = level.without_level_issues(flat)
        return {project: {k: v for k, v in targets.items() if k in kept} for project, targets in by_project.items()}

    def _level_projects(self) -> list[tuple[dict, int, int]]:
        """One scope per level sub-project, for the selected issues resolved in it this run (ADR-009)."""
        level = getattr(self, "_level", None)
        if level is None:
            return []
        by_label: dict[str, tuple[level_issues.LevelTarget, dict[str, int]]] = {}
        for key, target in level.targets.items():
            by_label.setdefault(target.label, (target, {}))[1][key] = level.work_packages[key]
        return [self._level_rows(level, target, wps) for _, (target, wps) in sorted(by_label.items())]

    def _level_rows(
        self, level: level_issues.LevelWorkPackages, target: level_issues.LevelTarget, wps: dict[str, int]
    ) -> tuple[dict, int, int]:
        """Rows of one sub-project; an issue whose level changed or that the search omits is refused."""
        project = target.label.rpartition(":")[0]
        fields = "project,security,description"
        if self._task_count_field:
            fields += "," + self._task_count_field
        rows: list[dict] = []
        held = unconfirmed = 0
        source_pid = None
        seen = set()
        for issue in level_issues.iter_level_issues(self.jira_client, project, 100, fields):
            if issue.key not in wps or issue.key in seen:
                continue
            seen.add(issue.key)
            if level_issues.issue_level_id(issue) != target.level_id:
                level.refused.append(f"{issue.key}: changed its level after the selection")
                continue
            project_ref = issue.raw["fields"]["project"]
            if project_ref.get("projectTypeKey") not in {"software", "business"}:
                unconfirmed += 1
                continue
            source_id = str(project_ref["id"])
            if project_ref["key"] != project or source_pid not in (None, source_id):
                msg = "Jira tasklist project identity mismatch"
                raise ValueError(msg)
            source_pid = source_id
            try:
                row, unknown = self._issue_row(issue, (wps[issue.key], None), target.level_id)
            except _LevelChangedError:
                level.refused.append(f"{issue.key}: changed its level after the selection")
                continue
            if row:
                rows.append(row)
            held += unknown
        level.refused.extend(f"{key}: not returned by the level search" for key in sorted(set(wps) - seen))
        scope = {
            "project": target.project_id,
            "parent": target.parent_id,
            "key": project,
            "source_id": source_pid,
            "base": self.jira_client.base_url,
            "rows": rows,
        }
        return scope, held, unconfirmed

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
        """Reconcile Jira tasks and their source metadata.

        With the level switch on, the level work packages of this run are resolved before any
        write (ADR-009) and their refusals are counted into the result.
        """
        self._level: level_issues.LevelWorkPackages | None = None
        if level_issues.enabled():
            try:
                self._level = level_issues.level_work_packages(
                    self.jira_client, self.op_client, self.mappings, self.logger
                )
            except LevelProjectError as exc:
                self.logger.error("Level work package tasklists stopped: %s", exc)
                return ComponentResult(success=False, message=str(exc), errors=["security_level"])
        result = self._run_etl_pipeline("Simpletasks")
        return result if self._level is None else self._level.report(result, self.logger)

    def run_with_change_detection(self, entity_type: str | None = None) -> ComponentResult:
        """Task edits can occur independently of Jira's issue update timestamp."""
        return self.run()
