# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Preserve each ordered Jira component assignment and set the first native category."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import batched
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.issue_security import permits_unrestricted_result, unrestricted_project_jql
from src.application.components.project_named_records import project_id
from src.infrastructure.jira.issue_pagination import iter_issue_pages
from src.infrastructure.jira.jira_issue_service import fetch_issue_page_with_retry
from src.infrastructure.openproject.component_assignments import apply_component_assignments
from src.models import ComponentResult, WorkPackageMappingEntry


def _component_refs(values: Any, base: str) -> list[dict[str, str]]:
    if not isinstance(values, list):
        msg = "Jira did not supply its component list"
        raise ValueError(msg)
    found = {}
    for value in values:
        sid = str(value["id"])
        name = value["name"]
        if not sid.isdecimal() or sid in found or not isinstance(name, str) or not name.strip():
            msg = "Invalid or duplicate Jira component"
            raise ValueError(msg)
        url = f"{base}/rest/api/2/component/{sid}"
        if value.get("self", url) != url:
            msg = "Foreign component source URL"
            raise ValueError(msg)
        found[sid] = {"id": sid, "name": name, "url": url}
    return list(found.values())


@register_entity_types("components")
class ComponentsMigration(BaseMigration):
    """Reconcile source-confirmed component assignments on migrated work packages."""

    def _source_issues(self, key: str) -> Iterator:
        jql = f"{unrestricted_project_jql(key)} ORDER BY id"

        def fetch(offset: int, size: int) -> list:
            return fetch_issue_page_with_retry(
                self.jira_client,
                jql=jql,
                start_at=offset,
                max_results=size,
                fields="project,components,security",
                expand=None,
                project_key=key,
            )

        return iter_issue_pages(key, fetch, 100)

    def _mapped_targets(self, wanted: set[str]) -> dict[str, dict[str, tuple[int, str | None]]]:
        projects: dict[str, dict[str, tuple[int, str | None]]] = {}
        for origin, value in (self.mappings.get_mapping("work_package") or {}).items():
            key = self._inner_jira_key(str(origin), value)
            if not key or (wanted and self._issue_project_key(key) not in wanted):
                continue
            row = WorkPackageMappingEntry.from_legacy(key, value)
            targets = projects.setdefault(self._issue_project_key(key), {})
            if key in targets:
                msg = "Duplicate Jira work-package mapping"
                raise ValueError(msg)
            targets[key] = (int(row.openproject_id), str(origin) if str(origin).isdecimal() else None)
        return projects

    def _extract(self) -> ComponentResult:
        base = self.jira_client.base_url
        wanted = set(config.selected_jira_projects())
        mapped = self._mapped_targets(wanted)
        projects = {}
        skipped = 0
        for key, mapping in (self.mappings.get_mapping("project") or {}).items():
            if wanted and key not in wanted:
                continue
            pid = project_id(mapping)
            if not pid:
                continue
            targets = self._target_origins(key, pid, mapped.get(key, {}))
            if targets:
                rows, source_pid = self._project_rows(key, targets, base)
                skipped += len(targets) - len(rows)
                if rows:
                    projects[key] = {"project_id": pid, "jira_id": source_pid, "key": key, "jira": base, "issues": rows}
        warnings = []
        if skipped:
            warnings.append(f"Jira could not confirm {skipped} mapped work packages for ordinary component assignment.")
            self.logger.warning(warnings[0])
        return ComponentResult(
            success=True, data=projects, details={"unconfirmed_work_packages": skipped}, warnings=warnings
        )

    def _target_origins(self, key: str, pid: int, mapped: dict) -> dict:
        targets = dict(mapped)
        seen = set()
        for row in self.op_client.get_project_wp_cf_snapshot(pid):
            origin = row.get("jira_issue_key")
            if not origin or self._issue_project_key(origin) != key:
                continue
            wp_id, sid = row["id"], row.get("jira_source_id")
            previous, mapped_sid = targets.get(origin, (wp_id, None))
            if origin in seen or wp_id != previous or (mapped_sid and sid and mapped_sid != sid):
                msg = "Duplicate or conflicting target work-package origin"
                raise ValueError(msg)
            seen.add(origin)
            targets[origin] = (wp_id, sid or mapped_sid)
        return targets

    def _project_rows(self, key: str, targets: dict, base: str) -> tuple[list, str | None]:
        rows = []
        source_pid = None
        for issue in self._source_issues(key):
            if issue.key not in targets or not permits_unrestricted_result(issue):
                continue
            fields = issue.raw["fields"]
            project = fields["project"]
            if project.get("projectTypeKey") not in {"software", "business"}:
                continue  # Customer-facing and unknown project audiences require a separate access check.
            pid = str(project["id"])
            if project["key"] != key or not pid.isdecimal() or source_pid not in (None, pid):
                msg = "Jira returned inconsistent project origins"
                raise ValueError(msg)
            source_pid = pid
            wp_id, expected = targets[issue.key]
            if not str(issue.id).isdecimal() or (expected is not None and str(issue.id) != str(expected)):
                msg = "Mapped source ID differs from Jira"
                raise ValueError(msg)
            rows.append(
                {
                    "id": str(issue.id),
                    "key": issue.key,
                    "wp": wp_id,
                    "components": _component_refs(fields.get("components"), base),
                }
            )
        self._check_catalogue(key, rows, base)
        return rows, source_pid

    def _check_catalogue(self, key: str, rows: list, base: str) -> None:
        refs = [ref for row in rows for ref in row["components"]]
        if not refs:
            return
        catalogue = _component_refs([item.raw for item in self.jira_client.jira.project_components(key)], base)
        by_id = {ref["id"]: ref for ref in catalogue}
        if any(by_id.get(ref["id"]) != ref for ref in refs):
            msg = "Issue component differs from its Jira project catalogue"
            raise ValueError(msg)

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        categories = dict(self.mappings.get_mapping("category") or {})
        updated = failed = 0
        for project in (mapped.data or {}).values():
            pid = str(project["project_id"])
            completed = 0
            for batch in batched(project["issues"], 100, strict=False):
                try:
                    outcome = apply_component_assignments(
                        self.op_client, {**project, "issues": list(batch)}, categories.get(pid, {})
                    )
                    updated += outcome["updated"]
                    completed += len(batch)
                    self._save_categories(categories, pid, outcome["categories"])
                except Exception:
                    self.logger.exception("Could not reconcile component assignments for %s", project["key"])
                    failed += max(1, len(project["issues"]) - completed)
                    break
        return ComponentResult(
            success=not failed, updated=updated, failed=failed, details=mapped.details, warnings=mapped.warnings
        )

    def _save_categories(self, categories: dict, pid: str, verified: dict) -> None:
        current = categories.get(pid, {})
        targets = set(verified.values())
        merged = {name: identifier for name, identifier in current.items() if identifier not in targets}
        merged.update(verified)
        if merged != current:
            categories[pid] = merged
            self.mappings.set_mapping("category", categories)

    def run(self) -> ComponentResult:
        """Check the source and the target on every pass, including retries."""
        return self._run_etl_pipeline("Components")

    def run_with_change_detection(self, entity_type: str | None = None) -> ComponentResult:
        """Target category and origin changes can occur independently of Jira updates."""
        return self.run()
