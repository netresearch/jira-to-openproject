# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Apply Jira sprint membership and epic parents to existing work packages."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from itertools import batched
from typing import Any

from src import config
from src.application.components import level_issues, level_versions
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.security_level_projects_migration import LevelProjectError
from src.models import ComponentResult, WorkPackageMappingEntry

SPRINT_CF_NAME = "Sprint"
SPRINT_HISTORY_CF_NAME = "Jira sprint membership history"
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


def _encode_sprint_history(sprints: list[tuple[str, str]]) -> str:
    records = [{"id": sid or None, "name": name} for sid, name in sprints]
    return json.dumps(records, ensure_ascii=False)


def _parent_sprint(sprints: list[tuple[str, str]], parent_id: int, versions: dict) -> tuple[str, int] | None:
    """The first sprint, by name, with an ID and a mapped version of the parent project."""
    for sprint_id, name in sorted(sprints, key=lambda ref: ref[1]):
        version = versions.get(sprint_id or name)
        if (
            sprint_id
            and isinstance(version, dict)
            and type(version.get("openproject_id")) is int
            and str(version.get("project_id")) == str(parent_id)
        ):
            return sprint_id, version["openproject_id"]
    return None


@register_entity_types("sprint_epic")
class SprintEpicMigration(BaseMigration):
    """Preserve epic hierarchy, sprint names and the sprint version fallback.

    With ``migration.security_level_work_packages`` on, an issue of a level search or of the
    archive loses its ordinary entry, also one the origin scan recovers (ADR-009), and no
    ordinary work package is linked to it. A selected level work package without fix versions
    gets the copy of its parent sprint version in its own sub-project (ADR-010), but no sprint
    field and no parent link.
    """

    def _recover_work_packages(self) -> dict[str, Any]:
        """Recover locally needed issue keys from their target projects' origin fields."""
        restored = {}
        selected = set(config.selected_jira_projects())
        for project, mapping in (self.mappings.get_mapping("project") or {}).items():
            if selected and project.upper() not in selected:
                continue
            project_id = mapping.get("openproject_id") if isinstance(mapping, dict) else mapping
            if not project_id:
                continue
            self._recover_project_issues(project, project_id, restored)
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
        selected = set(config.selected_jira_projects())
        if selected:
            keys = [key for key in keys if str(key).rsplit("-", 1)[0].upper() in selected]
        mappings, keys = self._without_level_issues(mappings, keys)
        self._work_package_mapping = mappings  # full mapping: parents may sit in other projects
        if not keys:
            return ComponentResult(
                success=False, failed=1, message="No mapped work packages. Run work_packages_skeleton first."
            )

        records, missing = self._extract_sprint_records(keys)
        self._level_sprints = self._level_sprint_plan()
        return ComponentResult(success=True, data=records, details={"missing_issues": missing})

    def _level_sprint_plan(self) -> dict[str, dict[str, Any]]:
        """Per level sub-project: the parent sprint versions its selected issues need, and those issues."""
        level = getattr(self, "_level", None)
        plan: dict[str, dict[str, Any]] = {}
        if level is None:
            return plan
        versions = self.mappings.get_mapping("sprint") or {}
        fields = f"project,security,fixVersions,{self._sprint_field}"
        for project, wps in sorted(level.by_project().items()):
            for key, issue in level_versions.selected_level_issues(
                level, self.jira_client, project, wps, fields
            ).items():
                self._plan_level_sprint(plan, level, versions, issue, wps[key])
        return plan

    def _plan_level_sprint(
        self, plan: dict, level: level_issues.LevelWorkPackages, versions: dict, issue: Any, wp_id: int
    ) -> None:
        """Add one issue; fix versions own the target version, as on the ordinary path."""
        fields = issue.raw["fields"]
        sprints = _sprint_references(fields.get(self._sprint_field))
        if not sprints or fields.get("fixVersions"):
            return
        target = level.targets[issue.key]
        sprint = _parent_sprint(sprints, target.parent_id, versions)
        if sprint is None or not str(issue.id).isdecimal():
            level.refused.append(f"{issue.key}: no sprint version of the parent project for its sprints")
            return
        group = plan.setdefault(
            target.label,
            {
                "target": target,
                "origin": (target.label.rpartition(":")[0], str(fields["project"]["id"])),
                "sprints": {},
            },
        )
        group["sprints"][sprint[0]] = sprint[1]
        group.setdefault("issues", []).append({"key": issue.key, "id": str(issue.id), "wp": wp_id, "sprint": sprint[0]})

    def _level_sprint_assignments(self) -> list[dict[str, Any]]:
        """Create or adopt the sprint copies of each sub-project; a failed one refuses its issues."""
        plan = getattr(self, "_level_sprints", {})
        if not plan:
            return []
        copier = level_versions.LevelVersionCopies(
            self.op_client, self.mappings, self.jira_client.base_url, self.logger
        )
        base = self.jira_client.base_url.rstrip("/")
        rows = []
        for label, group in plan.items():
            copies = copier.ensure_sprints(group["target"], group["origin"], group["sprints"])
            if copies is None:
                self._level.refused.extend(f"{row['key']}: no sprint copy in {label}" for row in group["issues"])
                continue
            for row in group["issues"]:
                origin = {"project": group["target"].project_id, "id": row["id"], "key": row["key"]}
                rows.append(
                    {
                        "id": row["wp"],
                        "target_version_ids": [copies[row["sprint"]]],
                        "jira_version_issue_origin": origin | {"url": f"{base}/browse/{row['key']}"},
                    }
                )
        return rows

    def _without_level_issues(self, mappings: dict[str, Any], keys: list[str]) -> tuple[dict[str, Any], list[str]]:
        """Drop every entry of an issue of a level search or of the archive; refuse those of this run."""
        level = getattr(self, "_level", None)
        if level is None:
            return mappings, keys
        level.without_level_issues({key: 0 for key in keys if key in level.level_keys})
        kept = {
            outer: raw for outer, raw in mappings.items() if self._inner_jira_key(outer, raw) not in level.level_keys
        }
        return kept, [key for key in keys if key not in level.level_keys]

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        wp_map = self._work_package_mapping
        entries, invalid = self._validated_work_packages(wp_map)

        versions = self.mappings.get_mapping("sprint") or {}
        projects = self.mappings.get_mapping("project") or {}
        parents, assignments, values, history = [], [], [], []
        unresolved_epics: dict[str, str] = {}
        unresolved_parents: dict[str, str] = {}
        unresolved_sprints = []
        for key, (sprints, direct_parent, epic, has_fix_version) in (extracted.data or {}).items():
            if key not in entries:
                continue  # reported in invalid_mappings
            entry = entries[key]
            wp_id = entry.openproject_id
            self._map_parent(
                key,
                direct_parent or epic,
                entries,
                wp_id,
                parents,
                unresolved=unresolved_parents if direct_parent else unresolved_epics,
            )
            if not sprints:
                continue
            names = sorted({name for _, name in sprints})
            values.append({"work_package_id": wp_id, "value": ", ".join(names)})
            history.append({"work_package_id": wp_id, "value": _encode_sprint_history(sprints)})
            if has_fix_version:
                continue  # Jira releases own version_id even on metadata-only reruns.
            version_id = self._sprint_version_id(key, entry, sprints, projects, versions)
            if version_id is None:
                unresolved_sprints.append(key)
                continue
            assignments.append({"id": wp_id, "version_id": version_id})
        return ComponentResult(
            success=True,
            data={"parents": parents, "assignments": assignments, "values": values, "history": history},
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
        assignments = data["assignments"] + self._level_sprint_assignments()
        for operation, rows in (("parent_links", data["parents"]), ("version_assignments", assignments)):
            count, errors = self._apply_batches(rows, self.op_client.batch_update_work_packages, operation)
            details[operation] = count
            updated += count
            failed += errors

        for field_name, rows, count_key in (
            (SPRINT_CF_NAME, data["values"], "sprint_cf_updates"),
            (SPRINT_HISTORY_CF_NAME, data["history"], "sprint_history_updates"),
        ):
            count, field_id = self._write_sprint_field(field_name, rows)
            details[count_key] = count
            if field_name == SPRINT_CF_NAME:
                details["cf_available"] = bool(field_id)
            updated += count
            failed += len(rows) - count
        return ComponentResult(
            success=failed == 0,
            message=f"Sprint and epic metadata: {updated} confirmed, {failed} unresolved or failed",
            updated=updated,
            failed=failed,
            details=details,
        )

    def _write_sprint_field(self, name: str, rows: list[dict]) -> tuple[int, int]:
        if not rows:
            return 0, 0
        try:
            field_id = self._ensure_wp_custom_field(name, "text")
        except Exception:
            self.logger.exception("Unable to prepare the %s field for %d work packages", name, len(rows))
            return 0, 0
        if not field_id:
            return 0, 0
        values = [dict(row, custom_field_id=field_id) for row in rows]
        count, _ = self._apply_batches(values, self.op_client.bulk_set_wp_custom_field_values, name)
        return count, field_id

    def run(self) -> ComponentResult:
        """Run on each pass: memberships also depend on mappings from earlier components.

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
                self.logger.error("Level work package components stopped: %s", exc)
                return ComponentResult(success=False, message=str(exc), errors=["security_level"])
        result = self._run_etl_pipeline("Sprint and epic")
        return result if self._level is None else self._level.report(result, self.logger)

    def _recover_project_issues(self, project: str, project_id: Any, restored: dict) -> None:
        """Collect unique issue origins for one project."""
        for row in self.op_client.get_project_wp_cf_snapshot(int(project_id)):
            key = row.get("jira_issue_key")
            if not key or self._issue_project_key(key) != project:
                continue
            if key in restored:
                msg = f"Multiple OpenProject work packages have Jira origin key {key}"
                raise ValueError(msg)
            restored[key] = {"jira_key": key, "openproject_id": row["id"], "openproject_project_id": project_id}

    def _extract_sprint_records(self, keys: list[str]) -> tuple[dict, list]:
        """Read the sprint and parent references in bounded Jira batches."""
        # Jira Software exposes tenant-specific field IDs; their display names can be translated.
        custom_fields = {(f.get("schema") or {}).get("custom"): f["id"] for f in self.jira_client.get_custom_fields()}
        sprint_field = self._sprint_field = custom_fields.get(_GREENHOPPER + "gh-sprint", "sprint")
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
        return records, missing

    def _validated_work_packages(self, wp_map: dict) -> tuple[dict, list]:
        """Index successfully validated work packages and retain invalid keys."""
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
        return entries, invalid

    @staticmethod
    def _map_parent(key: str, parent_key: Any, entries: dict, wp_id: int, parents: list, *, unresolved: dict) -> None:
        """Prefer the direct parent, preserving unresolved epic and parent diagnostics."""
        if parent_key:
            parent = entries.get(parent_key.strip()) if isinstance(parent_key, str) else None
            if parent and parent.openproject_id != wp_id:
                parents.append({"id": wp_id, "parent_id": parent.openproject_id})
            else:
                unresolved[key] = str(parent_key)

    def _sprint_version_id(
        self, key: str, entry: WorkPackageMappingEntry, sprints: list, projects: dict, versions: dict
    ) -> int | None:
        """Choose the first mapped sprint version that belongs to the work package project."""
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
            return int(version["openproject_id"])
        return None
