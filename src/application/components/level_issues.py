# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Select the Jira issues of a security level for its private sub-project (ADR-009, step 4).

Off unless ``migration.security_level_work_packages`` is ``true``. One search per project,
``level IS NOT EMPTY``, partitioned by the issue's ``fields.security.id``: Jira 9.12 answers
``level = <id>`` with HTTP 400 for a level of which the searching user sees no issue. The
shared inventory report lists the keys of every level's static issues and of every archived
issue; a level whose search does not return exactly the keys the report lists for it is refused
as a whole, so an issue created, deleted or moved since the inventory never goes anywhere. Level work packages are kept in their own mapping,
``security_level_work_package``, so that no component that reads the ``work_package``
mapping picks them up before it is adapted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from src import config
from src.application.components.level_access import Audience, staging_audience
from src.application.components.security_level_projects_migration import (
    _EDITIONS,
    MAPPING,
    REPORT_SETTING,
    LevelProjectError,
    _script,
    level_identifier,
    load_reports,
    migratable_levels,
)
from src.domain import provenance_fields
from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.jira.issue_pagination import iter_issue_pages
from src.infrastructure.openproject.rails_console_client import RubyError

SWITCH = "security_level_work_packages"
WORK_PACKAGE_MAPPING = "security_level_work_package"
_KINDS = ("static", "per_issue", "unexplained")
_LEVEL_ID = re.compile(r"[1-9]\d*", re.ASCII)


def enabled() -> bool:
    """The level path runs only when the switch is the boolean ``true`` and an inventory is configured.

    Without ``security_level_report`` there is no inventory and no sub-project, so nothing can go
    into one, and issues with a level stay out of the migration as with the switch off; the
    ordinary path never selects them. A configured report that is missing or does not match
    still stops each level component before any write.
    """
    return config.migration_config.get(SWITCH) is True and bool(config.migration_config.get(REPORT_SETTING))


def level_project_jql(project_key: str) -> str:
    """Every issue of the project that has a security level."""
    return f"project = {json.dumps(project_key)} AND level IS NOT EMPTY"


def iter_level_issues(
    jira_client: Any, project_key: str, page_size: int, fields: str, expand: str | None = None
) -> Iterator[Any]:
    """Page through the level search; no ``J2O_MAX_ISSUES`` limit, the selection needs every issue."""
    jql = f"{level_project_jql(project_key)} ORDER BY created ASC, key ASC"
    options = {"fields": fields} | ({"expand": expand} if expand else {})

    def fetch_page(offset: int, size: int) -> list:
        return jira_client.jira.search_issues(jql, startAt=offset, maxResults=size, **options)

    return iter_issue_pages(project_key, fetch_page, page_size)


def issue_level_id(issue: Any) -> str | None:
    """The level ID the search returned for the issue, or None when it is missing or malformed."""
    raw = issue if isinstance(issue, Mapping) else getattr(issue, "raw", None)
    fields = raw.get("fields") if isinstance(raw, Mapping) else None
    security = fields.get("security") if isinstance(fields, Mapping) else None
    value = security.get("id") if isinstance(security, Mapping) else None
    text = str(value) if type(value) in (str, int) else ""
    return text if _LEVEL_ID.fullmatch(text) else None


def _issue_key(issue: Any) -> str:
    return str(issue.get("key") if isinstance(issue, Mapping) else getattr(issue, "key", ""))


@dataclass(frozen=True)
class LevelTarget:
    """The level sub-project one selected issue goes to."""

    label: str
    level_id: str
    project_id: int
    parent_id: int
    identifier: str
    kind: str
    # Who may already be a member of the sub-project (ADR-012): the Ruby input of the staging checks.
    audience: dict[str, Any] = field(default_factory=lambda: {"members": [], "shares": {}}, compare=False)

    def mapping_fields(self) -> dict[str, Any]:
        """What a level work-package mapping entry adds to an ordinary one."""
        return {
            "openproject_project_id": self.project_id,
            "security_level_id": self.level_id,
            "security_level_project": self.label,
            "security_class": self.kind,
        }

    def matches(self, entry: object) -> bool:
        """A mapping entry written for this level and sub-project."""
        return (
            isinstance(entry, Mapping)
            and type(entry.get("openproject_id")) is int
            and entry.get("security_level_project") == self.label
            and entry.get("security_level_id") == self.level_id
            and entry.get("openproject_project_id") == self.project_id
        )


@dataclass
class Selection:
    """Selected issues and the counts of those that go nowhere."""

    selected: list[tuple[Any, LevelTarget]] = field(default_factory=list)
    unmapped: int = 0
    withheld: int = 0
    refused: list[str] = field(default_factory=list)


class LevelScope:
    """The inventory report, the edition and the sub-project mapping of one run."""

    def __init__(
        self, report: dict[str, Any], edition: str, projects: Mapping[str, Any], audience: Audience | None = None
    ) -> None:
        """Check the report rows; the archived rows carry key, level ID and a non-static class."""
        migratable_levels(report, edition)
        archived = report.get("archived")
        if not isinstance(archived, list) or not all(
            isinstance(row, dict)
            and isinstance(row.get("key"), str)
            and isinstance(row.get("level_id"), str)
            and row.get("class") in _KINDS[1:]
            for row in archived
        ):
            msg = "Malformed archived issues in the inventory report"
            raise LevelProjectError(msg)
        self.archived = {row["key"]: row for row in archived}
        if len(self.archived) != len(archived):
            msg = "Duplicate archived issue in the inventory report"
            raise LevelProjectError(msg)
        self.levels = report["levels"]
        labels = [f"{row['project']}:{row['level_id']}" for row in self.levels]
        if len(set(labels)) != len(labels):
            msg = "Duplicate level in the inventory report"
            raise LevelProjectError(msg)
        self.expected = {label: _expected_keys(row, archived) for label, row in zip(labels, self.levels, strict=True)}
        listed = [key for keys in self.expected.values() for key in keys]
        owned = {key for keys in self.expected.values() for key, kind in keys.items() if kind != "static"}
        if len(set(listed)) != len(listed) or owned != set(self.archived):
            msg = "An issue key belongs to more than one level in the inventory report"
            raise LevelProjectError(msg)
        self.kinds = _EDITIONS[edition]
        self.projects = projects
        self.audience = audience or Audience()

    @classmethod
    def load(cls, mappings: Any) -> LevelScope:
        """Both reports (see ``load_reports``), the ``security_level_project`` mapping and the staging audience."""
        report, edition = load_reports()
        return cls(report, edition, mappings.get_mapping(MAPPING) or {}, staging_audience(mappings, report, edition))

    def select(self, project_key: str, issues: Iterable[Any]) -> Selection:
        """Partition one project's level search by level and keep what goes to a sub-project."""
        result = Selection()
        found: dict[str, list[Any]] = {}
        for issue in issues:
            level_id = issue_level_id(issue)
            if level_id is None:
                result.refused.append(f"{_issue_key(issue)}: the search returned no security level ID")
            else:
                found.setdefault(level_id, []).append(issue)
        rows = {row["level_id"]: row for row in self.levels if row["project"] == project_key}
        for level_id in sorted(set(found) | {k for k, row in rows.items() if any(row[kind] for kind in _KINDS)}):
            self._select_level(project_key, level_id, found.get(level_id, []), rows.get(level_id), result)
        return result

    def _select_level(
        self, project_key: str, level_id: str, issues: list[Any], row: dict | None, result: Selection
    ) -> None:
        label = f"{project_key}:{level_id}"
        keys = [_issue_key(i) for i in issues]
        moved = [key for key in keys if key in self.archived and self.archived[key]["level_id"] != level_id]
        if moved:
            result.refused.append(f"{label}: {len(moved)} issues have another level than in the inventory")
            return
        expected = self.expected.get(label) if row is not None else None
        if expected is None or len(set(keys)) != len(keys) or set(keys) != set(expected):
            result.refused.append(f"{label}: the search does not match the inventory; run the inventory again")
            return
        classes = {key: expected[key] for key in keys}
        keep = [i for i in issues if classes[_issue_key(i)] in self.kinds]
        result.withheld += len(issues) - len(keep)
        if not keep:
            return
        entry = self.projects.get(label)
        if entry is None:
            result.unmapped += len(keep)
            return
        try:
            target = _target(project_key, level_id, entry)
        except LevelProjectError as exc:
            result.refused.append(f"{label}: {exc}")
            return
        audience = self.audience.of_level(label)
        result.selected.extend(
            (issue, replace(target, kind=classes[_issue_key(issue)], audience=audience)) for issue in keep
        )


def _expected_keys(row: Mapping[str, Any], archived: list[dict[str, Any]]) -> dict[str, str]:
    """Key to class for every issue the report lists for one level; a malformed level raises."""
    static = row.get("static_keys")
    mine = {
        r["key"]: r["class"]
        for r in archived
        if r["level_id"] == row["level_id"] and r["key"].startswith(f"{row['project']}-")
    }
    ok = (
        isinstance(static, list)
        and all(isinstance(key, str) for key in static)
        and len(set(static)) == len(static) == row["static"]
        and not set(static) & set(mine)
        and all(list(mine.values()).count(kind) == row[kind] for kind in _KINDS[1:])
    )
    if not ok:
        msg = f"Malformed issue keys for level {row['project']}:{row['level_id']} in the inventory report"
        raise LevelProjectError(msg)
    return dict.fromkeys(static, "static") | mine


# Every work package of a sub-project with its J2O Origin Key values; read before the first write
# into the sub-project, so that a lost mapping or batch answer never yields a second work package.
_EXISTING = (
    'field = WorkPackageCustomField.find_by(name: input.fetch("field"))\n'
    'ids = WorkPackage.where(project_id: input.fetch("project_id")).order(:id).pluck(:id)\n'
    'keys = field ? CustomValue.where(customized_type: "WorkPackage", custom_field_id: field.id, '
    "customized_id: ids).order(:customized_id).pluck(:customized_id, :value) : []\n"
    '{"field" => !field.nil?, "work_packages" => ids, "keys" => keys}'
)


def existing_work_packages(op_client: Any, project_id: int) -> dict[str, int]:
    """Issue key to work package ID of every work package in the sub-project; anything unclear raises."""
    state = op_client.execute_query_to_json_file(
        _script(_EXISTING, {"project_id": project_id, "field": provenance_fields.ORIGIN_KEY})
    )
    ok = (
        isinstance(state, dict)
        and type(state.get("field")) is bool
        and isinstance(state.get("work_packages"), list)
        and all(type(wp) is int for wp in state["work_packages"])
        and isinstance(state.get("keys"), list)
        and all(
            isinstance(pair, list) and len(pair) == 2 and type(pair[0]) is int and isinstance(pair[1], str)
            for pair in state["keys"]
        )
    )
    if not ok:
        msg = "malformed answer for the work packages of the sub-project"
        raise LevelProjectError(msg)
    ids = state["work_packages"]
    keyed = [wp for wp, _ in state["keys"]]
    if sorted(keyed) != sorted(ids):
        msg = "holds a work package without exactly one J2O Origin Key"
        raise LevelProjectError(msg)
    found = {key: wp for wp, key in state["keys"]}
    if len(found) != len(ids):
        msg = "holds two work packages for one issue"
        raise LevelProjectError(msg)
    return found


def _target(project_key: str, level_id: str, entry: object) -> LevelTarget:
    """The sub-project of a ``security_level_project`` entry; an entry for another key raises."""
    if not isinstance(entry, dict):
        msg = "malformed sub-project mapping entry"
        raise LevelProjectError(msg)
    ok = (
        entry.get("jira_project_key") == project_key
        and entry.get("level_id") == level_id
        and type(entry.get("openproject_id")) is int
        and type(entry.get("parent_openproject_id")) is int
        and entry.get("openproject_identifier") == level_identifier(project_key, level_id)
    )
    if not ok:
        msg = "the sub-project mapping entry does not belong to this project and level"
        raise LevelProjectError(msg)
    return LevelTarget(
        f"{project_key}:{level_id}",
        level_id,
        entry["openproject_id"],
        entry["parent_openproject_id"],
        entry["openproject_identifier"],
        "static",
    )


class LiveSubProjects:
    """The work packages each level sub-project holds right now, read once per sub-project."""

    def __init__(self, op_client: Any, logger: Any) -> None:
        """Read through ``op_client``; log an unreadable sub-project on ``logger``."""
        self.op_client = op_client
        self.logger = logger
        self.live: dict[str, dict[str, int] | None] = {}

    def held(self, key: str, target: LevelTarget, entry: object) -> tuple[int, str | None]:
        """The work package the sub-project of ``target`` holds for ``key``, or why there is none.

        An unreadable sub-project refuses every issue. A mapping entry, when there is one, must
        name the same work package, level and sub-project.
        """
        if target.label not in self.live:
            try:
                self.live[target.label] = existing_work_packages(self.op_client, target.project_id)
            except (LevelProjectError, QueryExecutionError, RubyError) as exc:
                self.logger.error("Level sub-project %s not readable: %s", target.label, exc)
                self.live[target.label] = None
        held = self.live[target.label]
        wp_id = None if held is None else held.get(key)
        if wp_id is None:
            return 0, f"no work package in {target.label}; run work_packages_skeleton"
        if entry is not None and (not target.matches(entry) or entry["openproject_id"] != wp_id):
            return 0, f"the mapping entry does not name the work package {target.label} holds"
        return wp_id, None


@dataclass
class LevelWorkPackages:
    """The level work packages a dependent component may write to, and what it must leave alone."""

    work_packages: dict[str, int] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)
    level_keys: set[str] = field(default_factory=set)
    level_ids: dict[str, str] = field(default_factory=dict)
    # Jira key to the sub-project its work package was resolved in (ID and parent from the mapping).
    targets: dict[str, LevelTarget] = field(default_factory=dict)

    def by_project(self) -> dict[str, dict[str, int]]:
        """Jira project key to the work packages of its selected issues, for one level search per project."""
        projects: dict[str, dict[str, int]] = {}
        for key, target in self.targets.items():
            projects.setdefault(target.label.rpartition(":")[0], {})[key] = self.work_packages[key]
        return projects

    def without_level_issues(self, lookup: Mapping[str, int]) -> dict[str, int]:
        """The ordinary key lookup without any issue of a level search or of the archive.

        Such an issue has a work package outside its sub-project; it is refused on both sides.
        """
        kept = {}
        for key, wp_id in lookup.items():
            if key in self.level_keys:
                self.work_packages.pop(key, None)
                self.targets.pop(key, None)
                self.refused.append(f"{key}: already a work package outside its level sub-project")
            else:
                kept[key] = wp_id
        return kept

    def report(self, result: Any, logger: Any) -> Any:
        """Count the refusals as failures of ``result`` and add the level counts to its details."""
        for reason in self.refused:
            logger.error("Level work package refused: %s", reason)
        result.details["security_level"] = {"work_packages": len(self.work_packages), "refused": len(self.refused)}
        result.failed += len(self.refused)
        if self.refused:
            result.success = False
        return result

    def add_project(
        self, jira_client: Any, scope: LevelScope, level_map: Mapping[str, Any], live: LiveSubProjects, project_key: str
    ) -> None:
        """Repeat the selection of one Jira project and resolve each selected issue live."""
        found = list(
            iter_level_issues(jira_client, project_key, config.migration_config.get("batch_size", 100), "security")
        )
        selection = scope.select(project_key, found)
        self.refused.extend(selection.refused)
        selected = set()
        for issue, target in selection.selected:
            selected.add(str(issue.id))
            wp_id, reason = live.held(issue.key, target, level_map.get(str(issue.id)))
            if reason is None:
                self.work_packages[issue.key] = wp_id
                self.level_ids[issue.key] = target.level_id
                self.targets[issue.key] = target
            else:
                self.refused.append(f"{issue.key}: {reason}")
        for issue in found:
            key, jira_id = _issue_key(issue), str(getattr(issue, "id", ""))
            self.level_keys.add(key)
            if jira_id in level_map and jira_id not in selected:
                self.refused.append(f"{key}: not selected for the level sub-project of its work package")


def level_work_packages(
    jira_client: Any, op_client: Any, mappings: Any, logger: Any, scope: LevelScope | None = None
) -> LevelWorkPackages:
    """Repeat the selection for every Jira project in scope and read each sub-project once.

    A selected issue resolves to the work package its sub-project holds for it right now; a lost
    mapping entry is recovered from that read and not written back. A selected issue without such
    a work package, an entry that names another one, and an entry of an issue the selection no
    longer keeps are refused. Unselected issues get nothing. A missing or malformed report raises.
    A caller that already read the reports passes its ``scope``, so the run uses one inventory.
    """
    scope = scope or LevelScope.load(mappings)
    level_map = mappings.get_mapping(WORK_PACKAGE_MAPPING) or {}
    result = LevelWorkPackages(level_keys=set(scope.archived))
    live = LiveSubProjects(op_client, logger)
    wanted = config.selected_jira_projects()
    for project in jira_client.get_projects():
        project_key = str(project.get("key", ""))
        if not wanted or project_key.upper() in wanted:
            result.add_project(jira_client, scope, level_map, live, project_key)
    return result
