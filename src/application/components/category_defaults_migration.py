# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Preserve Jira's configured and effective component assignment policies."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.project_named_records import project_id
from src.infrastructure.openproject.category_defaults import apply_category_defaults
from src.models import ComponentResult


def _actor(raw: dict | None) -> dict | None:
    if raw is None:
        return None
    return {key: raw[key] for key in ("key", "name", "accountId", "active") if key in raw}


def _candidate(actor: dict, mapping: dict, base: str) -> dict | None:
    key, account, name = (actor.get(k) for k in ("key", "accountId", "name"))
    if actor.get("active") is not True or not (key or account):
        return None
    identity = key or name or account
    mapped = mapping.get(identity) or mapping.get(account)
    if not isinstance(mapped, dict) or not (target := project_id(mapped)):
        return None
    url = f"{base}/secure/ViewProfile.jspa?"
    url += f"accountId={quote(account)}" if account else f"name={quote(name or key)}"
    return {"id": target, "user_key": identity, "user_id": account or name or key, "url": url}


def _rule(raw: dict, project: dict, base: str, mapping: dict) -> dict:
    sid = str(raw["id"])
    if (
        not re.fullmatch(r"[1-9]\d*", sid, re.ASCII)
        or str(raw["projectId"]) != str(project["id"])
        or raw["project"] != project["key"]
        or raw["self"] != f"{base}/rest/api/2/component/{sid}"
        or not isinstance(raw["name"], str)
        or not raw["name"].strip()
    ):
        msg = "Invalid Jira component identity"
        raise ValueError(msg)
    policy = {k: raw.get(k) for k in ("assigneeType", "realAssigneeType", "isAssigneeTypeValid", "archived", "deleted")}
    policy.update({k: _actor(raw.get(k)) for k in ("lead", "assignee", "realAssignee")})
    source = {"id": sid, "name": raw["name"], "url": raw["self"]}
    requested = policy["assigneeType"]
    lead = {"COMPONENT_LEAD": policy["lead"], "PROJECT_LEAD": _actor(project.get("lead"))}.get(requested)
    candidate = None
    if (
        project.get("projectTypeKey") in {"software", "business"}
        and policy["isAssigneeTypeValid"] is True
        and requested == policy["realAssigneeType"]
        and lead
        and lead == policy["assignee"] == policy["realAssignee"]
        and not policy["archived"]
        and not policy["deleted"]
    ):
        candidate = _candidate(lead, mapping, base)
    return {"source": source, "policy": policy, "candidate": candidate}


@register_entity_types("category_defaults")
class CategoryDefaultsMigration(BaseMigration):
    """Translate fixed assignees after confirming the category and user in the target."""

    def _extract(self) -> ComponentResult:
        wanted = set(config.selected_jira_projects())
        users = self.mappings.get_mapping("user") or {}
        base = self.jira_client.base_url
        projects = []
        for key, value in (self.mappings.get_mapping("project") or {}).items():
            if wanted and key not in wanted:
                continue
            pid = project_id(value)
            detail = self.jira_client.jira.project(key).raw
            if not pid or detail["key"] != key or not re.fullmatch(r"[1-9]\d*", str(detail["id"]), re.ASCII):
                msg = "Unverified project for Jira category defaults"
                raise ValueError(msg)
            rules = [_rule(item.raw, detail, base, users) for item in self.jira_client.jira.project_components(key)]
            if len({r["source"]["id"] for r in rules}) != len(rules):
                msg = "Duplicate Jira component identity"
                raise ValueError(msg)
            projects.append(
                {
                    "project_id": pid,
                    "source": {"Jira Base URL": base, "Jira Project Key": key, "Jira Project ID": str(detail["id"])},
                    "project_policy": {
                        "projectTypeKey": detail.get("projectTypeKey"),
                        "assigneeType": detail.get("assigneeType"),
                        "lead": _actor(detail.get("lead")),
                    },
                    "rules": rules,
                }
            )
        return ComponentResult(success=True, data=projects)

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        counts = {"updated": 0, "native": 0, "held": 0, "preserved": 0}
        for project in mapped.data or []:
            totals = apply_category_defaults(self.op_client, project)
            for key in counts:
                counts[key] += totals[key]
        held = counts["held"]
        return ComponentResult(
            success=True,
            updated=counts["updated"],
            details=counts,
            warnings=[f"{held} component policies need administrator review."] if held else [],
        )

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Run against current source policies and independently verify the target on every invocation."""
        msg = "Category defaults use their own target-side reconciliation."
        raise ValueError(msg)

    def run(self) -> ComponentResult:
        """Reconcile project component assignment policies."""
        return self._run_etl_pipeline("Category defaults")
