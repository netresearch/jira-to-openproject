# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate unrestricted Jira affected versions as native observed-in links with source text."""

from __future__ import annotations

from itertools import batched

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.project_named_records import project_id
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
        configured = config.migration_config.get("jira_project_filter") or []
        selected = {k.upper() for k in ([configured] if isinstance(configured, str) else configured)}
        targets = {}
        for key, entry in mapping.items():
            jira_key = self._inner_jira_key(key, entry)
            mapped = self._work_package_entry(jira_key, entry) if jira_key else None
            if mapped:
                targets[mapped.jira_key] = mapped.openproject_id
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
        return ComponentResult(success=True, data={"projects": by_project, "targets": targets, "held": held})

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
        return ComponentResult(success=True, data={"batches": batches, "held": data["held"]})

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
        if not data["batches"]:
            return result
        field = self._ensure_wp_custom_field(AFFECTS_VERSIONS_CF_NAME, "text")
        if not field:
            return ComponentResult(success=False, failed=1)
        for batch in data["batches"]:
            try:
                response = write_observed_versions(self.op_client, {**batch, "field": field})
            except Exception:
                logger.exception("Failed to reconcile affected versions for %s", batch["key"])
                result.success = False
                result.failed += len(batch["issues"])
                continue
            result.updated += response["updated"]
            result.details["native_observed_versions"] = response["native"]
        return result

    def run(self) -> ComponentResult:
        """Run Affects versions migration."""
        return self._run_etl_pipeline("Affects versions")
