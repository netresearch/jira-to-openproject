# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Jira project release catalogues and assign native target versions to work packages."""

from __future__ import annotations

from typing import Any

from src import config
from src.application.components import level_issues, level_versions
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.project_named_records import apply_work_package_updates, project_id
from src.application.components.security_level_projects_migration import LevelProjectError
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.version_catalog import level_copy_id, sync_project_versions
from src.models import ComponentResult
from src.models.jira import JiraIssueFields


@register_entity_types("versions")
class VersionsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Run the catalogue and issue assignment together through the regular ETL path."""
        msg = "Version migration requires project catalogues and issue assignments together"
        raise ValueError(msg)

    def _extract(self) -> ComponentResult:
        """Read every selected, mapped project, including projects with no migrated issues."""
        mapping = self.mappings.get_mapping("project") or {}
        selected = set(config.selected_jira_projects())
        catalogues = {}
        for key, entry in mapping.items():
            pid = project_id(entry)
            if not pid or (selected and key.upper() not in selected):
                continue
            try:
                catalogues[key] = (pid, self.jira_client.projects.get_versions(key))
            except Exception:
                logger.exception("Could not read the Jira version catalogue for %s", key)
                return ComponentResult(success=False, failed=1)
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [key for key in self._jira_keys_from_wp_map(wp_map) if self._issue_project_key(key) in catalogues]
        keys = self._without_level_issues(keys)
        issues = {}
        for key in catalogues:
            project_keys = [issue_key for issue_key in keys if self._issue_project_key(issue_key) == key]
            try:
                issues.update(self.jira_client.projects.unrestricted_version_issues(key, project_keys))
            except Exception:
                logger.exception("Could not verify unrestricted Jira issues for %s", key)
                return ComponentResult(success=False, failed=1)
        held = len(set(keys) - issues.keys())
        try:
            plan = self._level_plan(catalogues)
        except Exception:
            logger.exception("Could not read the fix versions of level issues")
            return ComponentResult(success=False, failed=1)
        return ComponentResult(
            success=True,
            extracted=sum(len(v) for _, v in catalogues.values()),
            data={"catalogues": catalogues, "issues": issues, "level": plan},
            details={"unconfirmed_issue_mappings": held},
        )

    def _without_level_issues(self, keys: list[str]) -> list[str]:
        """With the level switch on, an issue of a level search or of the archive loses its ordinary entry."""
        level = getattr(self, "_level", None)
        if level is None:
            return keys
        kept = level.without_level_issues(dict.fromkeys(keys, 0))
        return [key for key in keys if key in kept]

    def _level_plan(self, catalogues: dict[str, tuple[int, list]]) -> dict[str, dict[str, Any]]:
        """Per level sub-project: its target, Jira project, catalogue and the issues with fix versions."""
        level = getattr(self, "_level", None)
        plan: dict[str, dict[str, Any]] = {}
        if level is None:
            return plan
        for project, wps in sorted(level.by_project().items()):
            if project not in catalogues:
                level.refused.extend(f"{key}: no version catalogue of {project} in this run" for key in sorted(wps))
                continue
            fields = "project,security,fixVersions"
            for key, issue in level_versions.selected_level_issues(
                level, self.jira_client, project, wps, fields
            ).items():
                refs = level_versions.version_ids(issue.raw["fields"].get("fixVersions", []))
                audience = level_versions.copy_audience(level, key)
                if refs and audience is None:
                    self._versions_withheld.append(key)
                elif refs is None or not str(issue.id).isdecimal():
                    level.refused.append(f"{key}: malformed fix versions or issue ID")
                elif refs:
                    target = level.targets[key]
                    entry = plan.setdefault(
                        target.label,
                        {"target": target, "project": project, "catalogue": catalogues[project][1], "issues": []},
                    )
                    entry["issues"].append(
                        {"key": key, "id": str(issue.id), "wp": wps[key], "versions": refs, "audience": audience}
                    )
        return plan

    def _level_copies(self, plan: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
        """Create or adopt each sub-project's copies; a sub-project that fails refuses its issues."""
        copies: dict[str, dict[str, int]] = {}
        if not plan:
            return copies
        copier = level_versions.LevelVersionCopies(self.op_client, self.mappings, self.jira_client.base_url, logger)
        for label, entry in plan.items():
            wanted: dict[str, set[tuple[int, ...]]] = {}
            for row in entry["issues"]:
                for sid in row["versions"]:
                    wanted.setdefault(sid, set()).add(row["audience"])
            made = copier.ensure(entry["target"], entry["project"], entry["catalogue"], wanted)
            if made is None:
                self._level.refused.extend(f"{row['key']}: no version copy in {label}" for row in entry["issues"])
            else:
                copies[label] = made
        return copies

    def _level_assignments(self, plan: dict[str, dict[str, Any]], copies: dict[str, dict[str, int]]) -> list[dict]:
        """The target versions of each level work package: its sub-project's copies, in Jira order."""
        base = self.jira_client.base_url.rstrip("/")
        updates = []
        for label, made in copies.items():
            entry = plan[label]
            for row in entry["issues"]:
                origin = {
                    "project": entry["target"].project_id,
                    "id": row["id"],
                    "key": row["key"],
                    "url": f"{base}/browse/{row['key']}",
                }
                ids = [made[level_copy_id(sid, row["audience"])] for sid in row["versions"]]
                updates.append({"id": row["wp"], "target_version_ids": ids, "jira_version_issue_origin": origin})
        return updates

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        versions = dict(self.mappings.get_mapping("version") or {})
        indexed = {}
        changed = 0
        for key, (pid, rows) in data["catalogues"].items():
            try:
                records, count = sync_project_versions(self.op_client, pid, self.jira_client.base_url, key, rows)
            except Exception:
                logger.exception("Could not reconcile the Jira version catalogue for %s", key)
                return ComponentResult(success=False, failed=1)
            indexed[str(pid)] = records
            versions[str(pid)] = records
            changed += count
        self.mappings.set_mapping("version", versions)
        plan = data.get("level", {})
        return ComponentResult(
            success=True,
            updated=changed,
            data={"version_map": indexed, "issues": data["issues"], "level": plan, "copies": self._level_copies(plan)},
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        version_map: dict[str, dict[str, int]] = data.get("version_map", {})
        issues: dict[str, Any] = data.get("issues", {})
        level_updates = self._level_assignments(data.get("level", {}), data.get("copies", {}))
        if not version_map and not issues:
            return ComponentResult(success=True, updated=0)

        wp_map = self.mappings.get_mapping("work_package") or {}
        proj_map = self.mappings.get_mapping("project") or {}

        updates: list[dict[str, Any]] = []
        failed = 0
        for key, wp_entry in wp_map.items():
            try:
                assignment = self._version_assignment(key, wp_entry, issues, proj_map, version_map)
                if assignment:
                    updates.append(assignment)
            except Exception:
                logger.exception(
                    "Could not resolve all fix versions for Jira issue %s", self._inner_jira_key(key, wp_entry) or key
                )
                failed += 1

        return apply_work_package_updates(self, updates + level_updates, failed)

    def _version_assignment(self, key: str, entry: Any, issues: dict, projects: dict, versions: dict) -> dict | None:
        if not isinstance(entry, (dict, int)):
            return None
        wp_id = entry.get("openproject_id") if isinstance(entry, dict) else entry
        if not wp_id:
            return None
        jira_key = self._inner_jira_key(key, entry) or key
        issue = issues.get(jira_key)
        if not issue:
            return None
        fields = JiraIssueFields.from_issue_any(issue)
        if not fields.fix_versions:
            return None
        proj_entry = projects.get(self._issue_project_key(jira_key))
        op_pid = project_id(proj_entry)
        records = versions.get(str(op_pid), {})
        # All references must resolve before writing anything for this work package.
        names = [ref.name or "" for ref in fields.fix_versions]
        target_ids = list(dict.fromkeys(int(records[name if name in records else name.strip()]) for name in names))
        source_id = str(issue["id"])
        if not source_id.isdecimal():
            msg = "Missing Jira issue ID for version assignment"
            raise ValueError(msg)
        origin = {
            "project": op_pid,
            "id": source_id,
            "key": jira_key,
            "url": f"{self.jira_client.base_url.rstrip('/')}/browse/{jira_key}",
        }
        return {"id": int(wp_id), "target_version_ids": target_ids, "jira_version_issue_origin": origin}

    def run(self) -> ComponentResult:
        """Execute versions migration pipeline.

        With the level switch on, the level work packages of this run are resolved before any
        write (ADR-009); each gets the copies of its fix versions in its own sub-project
        (ADR-010), and the refusals are counted into the result on every return.
        """
        self._level: level_issues.LevelWorkPackages | None = None
        self._versions_withheld: list[str] = []
        if level_issues.enabled():
            try:
                self._level = level_issues.level_work_packages(
                    self.jira_client, self.op_client, self.mappings, self.logger
                )
            except LevelProjectError as exc:
                self.logger.error("Level work package versions stopped: %s", exc)
                return ComponentResult(success=False, message=str(exc), errors=["security_level"])
        result = self._run_versions()
        if self._level is None:
            return result
        level_versions.report_withheld(result, self._versions_withheld, self.logger)
        return self._level.report(result, self.logger)

    def _run_versions(self) -> ComponentResult:
        self.logger.info("Starting versions migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error("Versions extraction failed: %s", extracted.message or extracted.error)
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error("Versions mapping failed: %s", mapped.message or mapped.error)
            return mapped

        loaded = self._load(mapped)
        loaded.details.update(extracted.details, version_catalogue_changes=mapped.updated)
        if loaded.success:
            self.logger.info("Versions migration completed (updated=%s, failed=%s)", loaded.updated, loaded.failed)
        else:
            self.logger.error("Versions migration encountered failures (failed=%s)", loaded.failed)
        return loaded
