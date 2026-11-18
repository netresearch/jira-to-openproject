# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Apply Jira sprint membership and epic parents to existing work packages."""

from __future__ import annotations

import re
from collections.abc import Callable
from itertools import batched
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult, WorkPackageMappingEntry

SPRINT_CF_NAME = "Sprint"
_BATCH_SIZE = 100
_GREENHOPPER = "com.pyxis.greenhopper.jira:"
_LEGACY_SPRINT = re.compile(r"\.Sprint@[^[]+\[id=(\d+),.*?\bname=(.*?),(?:startDate|endDate|completeDate)=")


def _field(value: Any, name: str) -> Any:
    """Read one field from Jira's REST or SDK representation."""
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _sprint_references(value: Any) -> list[tuple[str, str]]:
    """Keep the Jira sprint ID alongside its display name, including legacy Server values."""
    references = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str):
            match = _LEGACY_SPRINT.search(item)
            sprint_id, name = match.groups() if match else ("", item)
        else:
            sprint_id, name = str(_field(item, "id") or ""), _field(item, "name")
        if isinstance(name, str) and name.strip():
            references.append((sprint_id, name.strip()))
    return references


@register_entity_types("sprint_epic")
class SprintEpicMigration(BaseMigration):
    """Preserve epic hierarchy, sprint names and the sprint version fallback."""

    def _recover_work_packages(self) -> dict[str, Any]:
        """Recover locally needed issue keys from their target projects' origin fields."""
        restored = {}
        project_filter = config.jira_config.get("projects") or []
        if isinstance(project_filter, str):
            project_filter = project_filter.split(",")
        selected = {str(key).strip().upper() for key in project_filter}
        for project, mapping in (self.mappings.get_mapping("project") or {}).items():
            if selected and project.upper() not in selected:
                continue
            project_id = mapping.get("openproject_id") if isinstance(mapping, dict) else mapping
            if not project_id:
                continue
            for row in self.op_client.get_project_wp_cf_snapshot(int(project_id)):
                key = row.get("jira_issue_key")
                if not key or self._issue_project_key(key) != project:
                    continue
                if key in restored:
                    msg = f"Multiple OpenProject work packages have Jira origin key {key}"
                    raise ValueError(msg)
                restored[key] = {"jira_key": key, "openproject_id": row["id"], "openproject_project_id": project_id}
        return restored

    def _extract(self) -> ComponentResult:
        mappings = self.mappings.get_mapping("work_package") or {}
        keys = self._jira_keys_from_wp_map(mappings)
        if not keys:
            try:
                mappings = self._recover_work_packages()
            except Exception as exc:
                return ComponentResult(
                    success=False, failed=1, message=f"Could not read work-package provenance: {exc}"
                )
            keys = list(mappings)
        self._work_package_mapping = mappings
        if not keys:
            return ComponentResult(
                success=False, failed=1, message="No mapped work packages. Run work_packages_skeleton first."
            )

        # Jira Software exposes tenant-specific field IDs; their display names can be translated.
        custom_fields = {(f.get("schema") or {}).get("custom"): f["id"] for f in self.jira_client.get_custom_fields()}
        sprint_field = custom_fields.get(_GREENHOPPER + "gh-sprint", "sprint")
        epic_field = custom_fields.get(_GREENHOPPER + "gh-epic-link", "epicLink")
        records: dict[str, Any] = {}
        missing = []
        for batch in batched(keys, _BATCH_SIZE, strict=False):
            issues = self._merge_batch_issues(list(batch))
            for key in batch:
                issue = issues.get(key)
                fields = issue.get("fields", issue) if isinstance(issue, dict) else getattr(issue, "fields", None)
                if fields is None:
                    missing.append(key)
                    continue
                sprints = _sprint_references(_field(fields, sprint_field))
                epic = _field(fields, epic_field)
                parent = _field(_field(fields, "parent"), "key")
                if sprints or epic or parent:
                    records[key] = (sprints, parent, epic, bool(_field(fields, "fixVersions")))
        return ComponentResult(success=True, data=records, details={"missing_issues": missing})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        wp_map = self._work_package_mapping
        entries = {}
        invalid = []
        for outer, raw in wp_map.items():
            key = self._inner_jira_key(outer, raw)
            try:
                if key:
                    entry = WorkPackageMappingEntry.from_legacy(key, raw)
                    if entry.openproject_id > 0:
                        entries[key] = entry
                        continue
            except ValueError:
                pass
            invalid.append(key or outer)

        versions = self.mappings.get_mapping("sprint") or {}
        projects = self.mappings.get_mapping("project") or {}
        parents, assignments, values = [], [], []
        unresolved_epics: dict[str, str] = {}
        unresolved_parents: dict[str, str] = {}
        unresolved_sprints = []
        for key, (sprints, direct_parent, epic, has_fix_version) in (extracted.data or {}).items():
            if key not in entries:
                continue  # reported in invalid_mappings
            entry = entries[key]
            wp_id = entry.openproject_id
            parent_key = direct_parent or epic
            if parent_key:
                parent = entries.get(parent_key.strip()) if isinstance(parent_key, str) else None
                if parent and parent.openproject_id != wp_id:
                    parents.append({"id": wp_id, "parent_id": parent.openproject_id})
                else:
                    unresolved = unresolved_parents if direct_parent else unresolved_epics
                    unresolved[key] = str(parent_key)
            if not sprints:
                continue
            names = sorted({name for _, name in sprints})
            values.append({"work_package_id": wp_id, "value": ", ".join(names)})
            if has_fix_version:
                continue  # Jira releases own version_id even on metadata-only reruns.
            project = projects.get(self._issue_project_key(key))
            project_id = entry.openproject_project_id or (
                project.get("openproject_id") if isinstance(project, dict) else project
            )
            for sprint_id, name in sorted(sprints, key=lambda ref: ref[1]):
                version = versions.get(sprint_id or name)
                if not isinstance(version, dict) or not version.get("openproject_id"):
                    continue
                if project_id and version.get("project_id") and str(version["project_id"]) != str(project_id):
                    continue
                assignments.append({"id": wp_id, "version_id": int(version["openproject_id"])})
                break
            else:
                unresolved_sprints.append(key)
        return ComponentResult(
            success=True,
            data={"parents": parents, "assignments": assignments, "values": values},
            details={
                **extracted.details,
                "invalid_mappings": invalid,
                "unresolved_epics": unresolved_epics,
                "unresolved_parents": unresolved_parents,
                "unresolved_sprints": unresolved_sprints,
            },
        )

    def _apply_batches(self, rows: list[dict], write: Callable, operation: str) -> tuple[int, int]:
        """Count confirmed rows and treat every unacknowledged row as failed."""
        updated = 0
        for batch in batched(rows, _BATCH_SIZE, strict=False):
            try:
                result = write(list(batch))
                count = result["updated"]
                if type(count) is not int or not 0 <= count <= len(batch):
                    self.logger.error("Invalid %s update count: %r", operation, count)
                    continue
                updated += count
                if count != len(batch):
                    self.logger.error("Partial %s batch: %s", operation, result)
            except Exception:
                self.logger.exception("Failed %s batch of %d", operation, len(batch))
        return updated, len(rows) - updated

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        details = dict(mapped.details)
        failed = sum(len(value) for value in mapped.details.values())
        updated = 0
        for operation, rows in (("parent_links", data["parents"]), ("version_assignments", data["assignments"])):
            count, errors = self._apply_batches(rows, self.op_client.batch_update_work_packages, operation)
            details[operation] = count
            updated += count
            failed += errors

        values = data["values"]
        cf_id = 0
        if values:
            try:
                cf_id = self._ensure_wp_custom_field(SPRINT_CF_NAME, "text")
            except Exception:
                self.logger.exception("Unable to prepare the Sprint field for %d work packages", len(values))
        details["cf_available"] = bool(cf_id)
        count = 0
        if cf_id:
            for row in values:
                row["custom_field_id"] = cf_id
            count, _ = self._apply_batches(values, self.op_client.bulk_set_wp_custom_field_values, "sprint fields")
        details["sprint_cf_updates"] = count
        failed += len(values) - count
        return ComponentResult(
            success=failed == 0,
            message=f"Sprint and epic metadata: {updated + count} confirmed, {failed} unresolved or failed",
            updated=updated + count,
            failed=failed,
            details=details,
        )

    def run(self) -> ComponentResult:
        """Run on each pass: memberships also depend on mappings from earlier components."""
        return self._run_etl_pipeline("Sprint and epic")
