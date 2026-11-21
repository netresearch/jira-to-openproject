# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Create one private, memberless OpenProject sub-project per Jira project and security level (ADR-009).

The input is the shared ``j2o-restricted inventory`` report (counts only, no reader names)
and the ``j2o-restricted admins`` report of the same archive. Nothing is wired to work
packages yet, and no member is added: access is granted in a later step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration
from src.application.components.project_migration import project_identifier
from src.models import ComponentResult
from src.restricted_issues import ADMIN_REPORT_FORMAT, REPORT_FORMAT

REPORT_SETTING = "security_level_report"
ADMIN_REPORT_SETTING = "security_level_admin_report"
MAPPING = "security_level_project"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LEVEL_ID = re.compile(r"[1-9][0-9]*")
_EDITIONS = {"community": ("static",), "enterprise": ("static", "per_issue")}
_MAX_IDENTIFIER = 100  # Projects::Identifier::CLASSIC_IDENTIFIER_MAX_LENGTH (v17.9.7)
_MAX_NAME = 255

# ``Member.where(project_id:)`` counts every membership row, locked users and work package
# shares included (``Project#members`` is scoped to unlocked users, app/models/project.rb:59).
_STATE = (
    'if p then {"found" => true, "id" => p.id, "identifier" => p.identifier, "public" => p.public, '
    '"parent_id" => p.parent_id, "members" => Member.where(project_id: p.id).count} '
    'else {"found" => false} end'
)
_FIND = 'p = Project.find_by(identifier: input.fetch("identifier"))\n' + _STATE
# Project.new instead of Projects::CreateService: the service adds the creating user as a
# member (app/services/projects/concerns/new_project_service.rb:44, 54-71).
_CREATE = (
    'parent = Project.find(input.fetch("parent_id"))\n'
    'p = Project.new(name: input.fetch("name"), identifier: input.fetch("identifier"), '
    'description: input.fetch("description"), public: false, parent: parent)\n'
    'p.workspace_type = "project" if p.respond_to?(:workspace_type=)\n'
    "p.types = parent.types\n"
    "Project.transaction do\n"
    "  p.save!\n"
    '  p.enabled_module_names = ["work_package_tracking", "wiki"]\n'
    "  p.save!\n"
    "end\n"
    "p.reload\n" + _STATE
)


class LevelProjectError(ValueError):
    """An input or an OpenProject answer that does not allow a safe sub-project."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise LevelProjectError(message)


def level_identifier(jira_key: str, level_id: str) -> str:
    """``<project identifier>_level_<level id>``; a key-derived identifier never contains ``_``."""
    _require(_LEVEL_ID.fullmatch(level_id), "Security level ID is not a positive number")
    identifier = f"{project_identifier(jira_key)}_level_{level_id}"
    _require(len(identifier) <= _MAX_IDENTIFIER, f"Sub-project identifier for {jira_key} is too long")
    return identifier


def _script(body: str, values: dict[str, Any]) -> str:
    return f"input = JSON.parse(<<'J2O_INPUT')\n{json.dumps(values)}\nJ2O_INPUT\n{body}"


def _load(path: object, fmt: str) -> dict[str, Any]:
    _require(isinstance(path, str) and path, "Report path is not configured")
    try:
        value = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"Cannot read {path}: {type(exc).__name__}"
        raise LevelProjectError(msg) from exc
    _require(isinstance(value, dict) and value.get("format") == fmt, f"{path} is not a {fmt} report")
    _require(
        isinstance(value.get("archive_sha256"), str) and _SHA256.fullmatch(value["archive_sha256"]),
        f"{path} names no archive",
    )
    return value


def migratable_levels(report: dict[str, Any], edition: str) -> list[dict[str, Any]]:
    """Levels with at least one issue that goes to OpenProject on this edition; unexplained never."""
    kinds = _EDITIONS[edition]
    rows = report.get("levels")
    _require(isinstance(rows, list), "Malformed inventory report")
    selected = []
    for row in rows:
        _require(
            isinstance(row, dict)
            and isinstance(row.get("project"), str)
            and isinstance(row.get("level_id"), str)
            and isinstance(row.get("level"), str)
            and all(type(row.get(kind)) is int and row[kind] >= 0 for kind in ("static", "per_issue", "unexplained")),
            "Malformed level in the inventory report",
        )
        if sum(row[kind] for kind in kinds):
            selected.append(row)
    return selected


class SecurityLevelProjectsMigration(BaseMigration):
    """Create or verify the memberless private sub-project of every migratable level."""

    def run(self) -> ComponentResult:
        """Check both reports, then create each missing sub-project and record it in the mapping."""
        paths = (config.migration_config.get(REPORT_SETTING), config.migration_config.get(ADMIN_REPORT_SETTING))
        if not any(paths):
            return ComponentResult(
                success=True,
                message="Security level sub-projects skipped: no inventory report configured",
                details={"status": "skipped", "reason": "security_level_report_not_configured"},
            )
        try:
            report = _load(paths[0], REPORT_FORMAT)
            admins = _load(paths[1], ADMIN_REPORT_FORMAT)
            _require(
                admins["archive_sha256"] == report["archive_sha256"],
                "The administrator report belongs to another archive; run j2o-restricted admins first",
            )
            edition = admins.get("edition")
            _require(edition in _EDITIONS, "The administrator report names no edition")
            levels = migratable_levels(report, str(edition))
        except LevelProjectError as exc:
            self.logger.error("Security level sub-projects stopped: %s", exc)
            return ComponentResult(success=False, message=str(exc), errors=[str(exc)])
        return self._create_all(levels, str(edition))

    def _create_all(self, levels: list[dict[str, Any]], edition: str) -> ComponentResult:
        projects = self.mappings.get_mapping("project") or {}
        taken_ids = {project_identifier(key) for key in projects} | {
            entry.get("openproject_identifier") for entry in projects.values() if isinstance(entry, dict)
        }
        taken_names = {
            str(entry.get(field)).casefold()
            for entry in projects.values()
            if isinstance(entry, dict)
            for field in ("jira_name", "openproject_name")
            if entry.get(field)
        }
        selected = set(config.selected_jira_projects())
        mapping = dict(self.mappings.get_mapping(MAPPING) or {})
        counts = {"created": 0, "verified": 0}
        errors: list[str] = []
        for row in levels:
            key = row["project"]
            if selected and key not in selected:
                continue
            label = f"{key}:{row['level_id']}"
            try:
                entry, created = self._ensure(row, projects.get(key), taken_ids, taken_names)
            except LevelProjectError as exc:
                self.logger.error("Security level sub-project %s refused: %s", label, exc)
                errors.append(f"{label}: {exc}")
                continue
            mapping[label] = entry
            counts["created" if created else "verified"] += 1
        self.mappings.set_mapping(MAPPING, mapping)
        done = counts["created"] + counts["verified"]
        return ComponentResult(
            success=not errors,
            message=f"Security level sub-projects: {counts['created']} created, {counts['verified']} verified, "
            f"{len(errors)} refused",
            details={**counts, "edition": edition},
            errors=errors,
            total_count=done + len(errors),
            success_count=done,
            failed_count=len(errors),
        )

    def _ensure(
        self, row: dict[str, Any], parent: object, taken_ids: set[Any], taken_names: set[str]
    ) -> tuple[dict[str, Any], bool]:
        key, level_id = row["project"], row["level_id"]
        _require(
            isinstance(parent, dict) and type(parent.get("openproject_id")) is int and parent.get("openproject_name"),
            "Jira project is not in the project mapping",
        )
        identifier = level_identifier(key, level_id)
        name = f"{parent['openproject_name']} ({row['level']})"
        _require(identifier not in taken_ids, "identifier belongs to a migrated Jira project")
        _require(name.casefold() not in taken_names and len(name) <= _MAX_NAME, "name is taken or too long")
        values = {"identifier": identifier}
        state = self._state(_script(_FIND, values))
        created = not state["found"]
        if created:
            description = f"Issues of {key} with the Jira security level {row['level']} (ID {level_id})."
            values |= {"name": name, "parent_id": parent["openproject_id"], "description": description}
            state = self._state(_script(_CREATE, values))
            _require(state["found"], "OpenProject did not create the sub-project")
        _require(state["identifier"] == identifier, "OpenProject answered for another identifier")
        _require(state["public"] is False, "existing project is public")
        _require(state["members"] == 0, "existing project has members")
        _require(state["parent_id"] == parent["openproject_id"], "existing project has another parent")
        entry = {
            "jira_project_key": key,
            "level_id": level_id,
            "level": row["level"],
            "openproject_id": state["id"],
            "openproject_identifier": identifier,
            "parent_openproject_id": parent["openproject_id"],
        }
        return entry, created

    def _state(self, script: str) -> dict[str, Any]:
        state = self.op_client.execute_query_to_json_file(script)
        _require(isinstance(state, dict) and type(state.get("found")) is bool, "Malformed OpenProject answer")
        if state["found"]:
            _require(
                type(state.get("id")) is int
                and type(state.get("members")) is int
                and type(state.get("public")) is bool
                and (state.get("parent_id") is None or type(state.get("parent_id")) is int),
                "Malformed OpenProject answer",
            )
        return state
