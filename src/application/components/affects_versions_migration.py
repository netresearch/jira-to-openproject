# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate unrestricted Jira affected versions as native observed-in links with source text."""

from __future__ import annotations

from itertools import batched

from src import config
from src.application.components import level_issues, level_versions
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.project_named_records import project_id
from src.application.components.security_level_projects_migration import LevelProjectError
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.observed_versions import write_observed_versions
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult

AFFECTS_VERSIONS_CF_NAME = "Affects Versions"


@register_entity_types("affects_versions")
class AffectsVersionsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict]:
        """Run through the scoped project and issue pipeline."""
        msg = f"Change detection is not available for {entity_type}"
        raise ValueError(msg)

    def _extract(self) -> ComponentResult:
        """Query only positively unrestricted issues from mapped projects selected for this run."""
        projects = self.mappings.get_mapping("project") or {}
        mapping = self.mappings.get_mapping("work_package") or {}
        selected = set(config.selected_jira_projects())
        targets = {}
        for key, entry in mapping.items():
            jira_key = self._inner_jira_key(key, entry)
            mapped = self._work_package_entry(jira_key, entry) if jira_key else None
            if mapped:
                targets[mapped.jira_key] = mapped.openproject_id
        level = getattr(self, "_level", None)
        if level is not None:
            targets = level.without_level_issues(targets)
        by_project = {}
        held = 0
        for key, entry in projects.items():
            pid = project_id(entry)
            if not pid or (selected and key.upper() not in selected):
                continue
            keys = [k for k in targets if self._issue_project_key(k) == key]
            issues = self.jira_client.projects.unrestricted_version_issues(key, keys)
            held += len(set(keys) - issues.keys())
            by_project[key] = {"project": pid, "issues": list(issues.values())}
        data = {"projects": by_project, "targets": targets, "held": held, "level": self._level_issues()}
        return ComponentResult(success=True, data=data)

    def _level_issues(self) -> dict[str, dict]:
        """Per level sub-project: its target, Jira project, Jira project ID and affected-version rows.

        One level search per Jira project; the project's version catalogue is read only when an
        issue there has an affected version, since each copy needs the version's dates and status.
        """
        level = getattr(self, "_level", None)
        groups: dict[str, dict] = {}
        if level is None:
            return groups
        fields = "project,security,versions"
        for project, wps in sorted(level.by_project().items()):
            found = level_versions.selected_level_issues(level, self.jira_client, project, wps, fields)
            rows = {key: issue.raw for key, issue in found.items() if "versions" in issue.raw["fields"]}
            catalogue = (
                self.jira_client.projects.get_versions(project)
                if any(row["fields"]["versions"] for row in rows.values())
                else []
            )
            for key, raw in rows.items():
                target = level.targets[key]
                group = groups.setdefault(
                    target.label, {"target": target, "key": project, "catalogue": catalogue, "issues": []}
                )
                group["issues"].append(self._affected_issue({**raw, "key": key, "id": str(found[key].id)}, wps[key]))
                group["jira_project_id"] = str(raw["fields"]["project"]["id"])
        return groups

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Retain immutable source identities and distinguish omitted fields from empty selections."""
        data = extracted.data or {}
        batches = []
        for key, group in data["projects"].items():
            rows = []
            project_ids = set()
            for issue in group["issues"]:
                fields = issue["fields"]
                if "versions" not in fields:
                    continue
                project_ids.add(str(fields["project"]["id"]))
                rows.append(self._affected_issue(issue, data["targets"][issue["key"]]))
            if not rows:
                continue
            if len(project_ids) != 1 or not next(iter(project_ids)).isdecimal():
                msg = "Inconsistent project identity on affected versions"
                raise ValueError(msg)
            project_origin = {
                "project": group["project"],
                "key": key,
                "jira_project_id": project_ids.pop(),
                "source": self.jira_client.base_url.rstrip("/"),
            }
            batches.extend({**project_origin, "issues": batch} for batch in batched(rows, 100, strict=False))
        return ComponentResult(
            success=True, data={"batches": batches, "held": data["held"], "level": data.get("level", {})}
        )

    @staticmethod
    def _affected_issue(issue: dict, target: int) -> dict:
        refs = [{"id": str(v["id"]), "name": v["name"]} for v in issue["fields"]["versions"]]
        if not str(issue["id"]).isdecimal() or any(
            not v["id"].isdecimal() or not isinstance(v["name"], str) or not v["name"].strip() for v in refs
        ):
            msg = "Invalid Jira affected version or issue identity"
            raise ValueError(msg)
        return {"wp": target, "id": str(issue["id"]), "key": issue["key"], "versions": refs}

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        result = ComponentResult(success=True, details={"unconfirmed_issue_mappings": data["held"]})
        level_batches = self._level_batches(data.get("level", {}))
        if not data["batches"] and not level_batches:
            return result
        field = self._ensure_wp_custom_field(AFFECTS_VERSIONS_CF_NAME, "text")
        if not field:
            return ComponentResult(success=False, failed=1)
        for batch, level in [(batch, False) for batch in data["batches"]] + [(batch, True) for batch in level_batches]:
            try:
                response = write_observed_versions(self.op_client, {**batch, "field": field}, level=level)
            except Exception:
                logger.exception("Failed to reconcile affected versions for %s", batch["key"])
                result.success = False
                result.failed += len(batch["issues"])
                continue
            result.updated += response["updated"]
            result.details["native_observed_versions"] = response["native"]
        return result

    def _level_batches(self, groups: dict[str, dict]) -> list[dict]:
        """The writes of each sub-project, after its version copies exist; a failed copy refuses its issues."""
        if not groups:
            return []
        copier = level_versions.LevelVersionCopies(self.op_client, self.mappings, self.jira_client.base_url, logger)
        batches = []
        for label, group in groups.items():
            target, rows = group["target"], group["issues"]
            # Observed-in links neither make a version visible nor count, so all link the static copy.
            wanted = {ref["id"]: {level_versions.STATIC} for row in rows for ref in row["versions"]}
            if wanted and copier.ensure(target, group["key"], group["catalogue"], wanted) is None:
                self._level.refused.extend(f"{row['key']}: no version copy in {label}" for row in rows)
                continue
            origin = {
                "project": target.project_id,
                "parent_id": target.parent_id,
                "key": group["key"],
                "jira_project_id": group["jira_project_id"],
                "source": self.jira_client.base_url.rstrip("/"),
            }
            batches.extend({**origin, "issues": list(batch)} for batch in batched(rows, 100, strict=False))
        return batches

    def run(self) -> ComponentResult:
        """Run Affects versions migration.

        With the level switch on, the level work packages of this run are resolved before any
        write (ADR-009); their affected versions link to the copies in their own sub-project
        (ADR-010), and the refusals are counted into the result on every return.
        """
        self._level: level_issues.LevelWorkPackages | None = None
        if level_issues.enabled():
            try:
                self._level = level_issues.level_work_packages(
                    self.jira_client, self.op_client, self.mappings, self.logger
                )
            except LevelProjectError as exc:
                self.logger.error("Level work package affected versions stopped: %s", exc)
                return ComponentResult(success=False, message=str(exc), errors=["security_level"])
        result = self._run_etl_pipeline("Affects versions")
        return result if self._level is None else self._level.report(result, self.logger)
