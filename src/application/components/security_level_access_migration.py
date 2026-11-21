# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Late access grant for the level sub-projects (ADR-009, step 6; ADR-012).

Per level sub-project, one membership for each static reader with an OpenProject user, with
the project roles that user holds in the parent project, or "Reader" when it holds none. On
Enterprise, each ``per_issue`` work package is shared as "Work package viewer" with exactly the
OpenProject users of its issue's extra readers. Every membership row is read back after writing
and compared with the expected rows. A row this component would not have written (another
principal, another role, a group, a share of another work package) refuses the sub-project
before any write; nothing is deleted. Off unless ``migration.security_level_work_packages`` is on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src import config
from src.application.components import level_issues
from src.application.components.base_migration import BaseMigration
from src.application.components.level_access import Audience, load_audience
from src.application.components.level_issues import LevelTarget, _target
from src.application.components.security_level_projects_migration import LevelProjectError, _script, load_reports
from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.rails_console_client import RubyError
from src.models import ComponentResult

# Permissions that let a member widen the audience of a level sub-project (ADR-012, decision 3).
WIDENING_PERMISSIONS = (
    "manage_members",
    "invite_members_by_email",
    "share_work_packages",
    "share_sprint",
    "edit_project",
)
READER_ROLE = "Reader"  # seeded project role of OpenProject 17.9.7 (view only)

_MEMBERS = (
    "rows = lambda do |project|\n"
    "  Member.where(project_id: project.id).includes(:principal, :member_roles).order(:id).map do |m|\n"
    "    [m.principal&.type, m.user_id, m.entity_type, m.entity_id, m.member_roles.map(&:role_id).uniq.sort]\n"
    "  end\n"
    "end\n"
    'project = Project.find_by(id: input.fetch("project_id"))\n'
    'raise "Level sub-project is gone or has another parent" unless project && '
    'project.parent_id == input.fetch("parent_id")\n'
    'raise "Level sub-project is public" if project.public?\n'
)
# The parent's roles include those inherited from groups (the J2O Role groups, ADR-005); only
# project roles count, and none that lets a member widen the audience (ADR-012): adding or inviting
# members, sharing work packages or sprints, or editing the project (which can make it public).
# A reader left without a role gets "Reader", itself refused if it grants one of these.
# The share role is found by its builtin value, not by its name.
_STATE = _MEMBERS + (
    f"widening = %i[{' '.join(WIDENING_PERMISSIONS)}]\n"
    "narrow = ->(r) { r.is_a?(ProjectRole) && widening.none? { |p| r.has_permission?(p) } }\n"
    'reader = ProjectRole.find_by(name: input.fetch("reader"), builtin: Role::NON_BUILTIN)\n'
    'ids = input.fetch("users")\n'
    "users = User.where(id: ids).to_a\n"
    "parent = Member.where(project_id: project.parent_id, entity_id: nil, user_id: ids).includes(:roles)\n"
    '{"reader_role" => (reader if reader && narrow.call(reader))&.id, '
    '"viewer_role" => Role.find_by(builtin: Role::BUILTIN_WORK_PACKAGE_VIEWER)&.id, '
    '"parent_roles" => parent.to_h { |m| [m.user_id.to_s, m.roles.select(&narrow).map(&:id).uniq.sort] }, '
    '"locked" => users.select(&:locked?).map(&:id), "found" => users.map(&:id), "members" => rows.call(project)}'
)
# Project memberships go through Members::CreateService (ADR-005). Shares are saved directly:
# Shares::CreateService always sends the "shared with you" mail (app/services/shares/create_service.rb:62).
_WRITE = _MEMBERS + (
    "Member.transaction do\n"
    '  input.fetch("create").each do |row|\n'
    '    call = Members::CreateService.new(user: User.system).call(principal: User.find(row.fetch("user")), '
    'project: project, role_ids: row.fetch("roles"), send_notifications: false)\n'
    '    raise call.errors.full_messages.join(", ") unless call.success?\n'
    "  end\n"
    '  input.fetch("shares").each do |row|\n'
    '    wp = WorkPackage.find_by!(id: row.fetch("wp"), project_id: project.id)\n'
    '    Member.create!(project: project, entity: wp, principal: User.find(row.fetch("user")), '
    'roles: [Role.find(row.fetch("role"))])\n'
    "  end\n"
    "end\n"
    '{"members" => rows.call(project)}'
)

type Row = tuple[int, int | None]  # (user ID, shared work package ID or None)


@dataclass
class Grant:
    """The membership rows one sub-project must hold, and why some readers get none."""

    expected: dict[Row, tuple[int, ...]] = field(default_factory=dict)
    # Rows of locked readers: never created, but an existing one with these roles is a grant too.
    optional: dict[Row, tuple[int, ...]] = field(default_factory=dict)
    locked: int = 0
    missing: int = 0

    def allows(self, row: Row, roles: tuple[int, ...]) -> bool:
        """Whether an existing row is one of the readers' grants with its roles."""
        return (self.expected.get(row) or self.optional.get(row)) == roles

    def holds(self, actual: Mapping[Row, tuple[int, ...]]) -> bool:
        """Every expected row exists and every row is a grant: the read-back check."""
        return set(self.expected) <= set(actual) and all(self.allows(row, roles) for row, roles in actual.items())


def _members(rows: object) -> dict[Row, tuple[int, ...]]:
    """Membership rows by user and shared work package; a group, another entity or a malformed row raises."""
    if not isinstance(rows, list):
        msg = "malformed membership answer"
        raise LevelProjectError(msg)
    found: dict[Row, tuple[int, ...]] = {}
    for row in rows:
        ok = isinstance(row, list) and len(row) == 5 and row[0] == "User" and type(row[1]) is int
        ok = ok and ((row[2], row[3]) == (None, None) or (row[2] == "WorkPackage" and type(row[3]) is int))
        if not ok or not isinstance(row[4], list):
            msg = "holds a membership that is not a user's project membership or work package share"
            raise LevelProjectError(msg)
        found[(row[1], row[3])] = tuple(row[4])
    return found


def plan(state: Mapping[str, Any], members: frozenset[int], shares: Mapping[int, frozenset[int]]) -> Grant:
    """The expected rows: parent roles or "Reader" per static reader, the viewer role per share."""
    found, locked = set(state.get("found") or []), set(state.get("locked") or [])
    parent = state.get("parent_roles")
    if not isinstance(parent, dict):
        msg = "malformed OpenProject answer"
        raise LevelProjectError(msg)
    grant = Grant()
    wanted = {(user, None) for user in members} | {(user, wp) for wp, users in shares.items() for user in users}
    for user, wp in sorted(wanted, key=lambda row: (row[0], row[1] or 0)):
        if user not in found:
            grant.missing += 1
            continue
        if wp is None:
            roles = parent.get(str(user)) or [state.get("reader_role")]
        else:
            roles = [state.get("viewer_role")]
        target = grant.expected
        if user in locked:
            grant.locked += 1
            target = grant.optional
        target[(user, wp)] = tuple(sorted(roles, key=lambda role: role or 0))
    if any(None in roles for roles in [*grant.expected.values(), *grant.optional.values()]):
        msg = "OpenProject has no Reader or Work package viewer role"
        raise LevelProjectError(msg)
    return grant


@dataclass
class Totals:
    """Counts of one run."""

    granted: int = 0
    verified: int = 0
    memberships_created: int = 0
    shares_created: int = 0
    readers_without_user: int = 0
    locked_readers: int = 0
    refused: list[str] = field(default_factory=list)


class SecurityLevelAccessMigration(BaseMigration):
    """Grant each level sub-project exactly its readers and read the memberships back."""

    def run(self) -> ComponentResult:
        """Off with the switch; otherwise every mapped sub-project of the inventory."""
        if not level_issues.enabled():
            return ComponentResult(
                success=True,
                message="Security level access skipped: migration.security_level_work_packages is off",
                details={"status": "skipped", "reason": "security_level_work_packages_off"},
            )
        try:
            # One read of the reports: the issues the scope selects and the grants come from one inventory.
            report, edition = load_reports()
            audience = load_audience(self.mappings, report, edition)
            scope = level_issues.LevelScope(
                report, edition, self.mappings.get_mapping(level_issues.MAPPING) or {}, audience
            )
            level = None
            if edition == "enterprise":
                level = level_issues.level_work_packages(self.jira_client, self.op_client, self.mappings, self.logger)
        except LevelProjectError as exc:
            self.logger.error("Security level access stopped: %s", exc)
            return ComponentResult(success=False, message=str(exc), errors=["security_level"])
        totals = Totals(readers_without_user=sum(audience.unmapped.values()))
        for target in self._targets(scope, totals):
            self._grant(target, audience, self._shares(level, audience, target), totals)
        return self._result(totals, edition, level)

    def _targets(self, scope: level_issues.LevelScope, totals: Totals) -> list[LevelTarget]:
        selected = set(config.selected_jira_projects())
        targets = []
        for row in scope.levels:
            label = f"{row['project']}:{row['level_id']}"
            if (selected and row["project"] not in selected) or label not in scope.projects:
                continue
            try:
                targets.append(_target(row["project"], row["level_id"], scope.projects[label]))
            except LevelProjectError as exc:
                totals.refused.append(f"{label}: {exc}")
        return targets

    @staticmethod
    def _shares(
        level: level_issues.LevelWorkPackages | None, audience: Audience, target: LevelTarget
    ) -> dict[int, frozenset[int]]:
        """Per ``per_issue`` work package of the sub-project, the users of its issue's extra readers."""
        if level is None:
            return {}
        # The audience holds extra readers for ``per_issue`` issues only (load_readers checks it).
        return {
            level.work_packages[key]: users
            for key, users in audience.shares.items()
            if key in level.targets and level.targets[key].label == target.label
        }

    def _grant(
        self, target: LevelTarget, audience: Audience, shares: dict[int, frozenset[int]], totals: Totals
    ) -> None:
        members = audience.members.get(target.label, frozenset())
        users = sorted(members | {user for group in shares.values() for user in group})
        values = {"project_id": target.project_id, "parent_id": target.parent_id}
        try:
            state = self._query(_STATE, values | {"users": users, "reader": READER_ROLE})
            grant = plan(state, members, shares)
            actual = _members(state.get("members"))
            foreign = [row for row, roles in actual.items() if not grant.allows(row, roles)]
            if foreign:
                msg = f"holds {len(foreign)} membership rows that are not its readers' grants; nothing changed"
                raise LevelProjectError(msg)
            create = [row for row in grant.expected if row not in actual]
            if create:
                answer = self._query(_WRITE, values | self._payload(grant, create))
                actual = _members(answer.get("members") if isinstance(answer, dict) else None)
        except (LevelProjectError, QueryExecutionError, RubyError) as exc:
            self.logger.error("Security level access %s refused: %s", target.label, exc)
            totals.refused.append(f"{target.label}: {exc}")
            return
        totals.locked_readers += grant.locked
        totals.readers_without_user += grant.missing
        if not grant.holds(actual):
            totals.refused.append(f"{target.label}: the memberships read back differ from the readers")
            return
        totals.memberships_created += sum(1 for _, wp in create if wp is None)
        totals.shares_created += sum(1 for _, wp in create if wp is not None)
        if create:
            totals.granted += 1
        else:
            totals.verified += 1

    @staticmethod
    def _payload(grant: Grant, create: list[Row]) -> dict[str, Any]:
        return {
            "create": [{"user": user, "roles": list(grant.expected[(user, wp)])} for user, wp in create if wp is None],
            "shares": [
                {"wp": wp, "user": user, "role": grant.expected[(user, wp)][0]} for user, wp in create if wp is not None
            ],
        }

    def _query(self, body: str, values: dict[str, Any]) -> dict[str, Any]:
        answer = self.op_client.execute_query_to_json_file(_script(body, values))
        if not isinstance(answer, dict):
            msg = "malformed OpenProject answer"
            raise LevelProjectError(msg)
        return answer

    def _result(self, totals: Totals, edition: str, level: level_issues.LevelWorkPackages | None) -> ComponentResult:
        done = totals.granted + totals.verified
        details = {key: value for key, value in vars(totals).items() if key != "refused"} | {"edition": edition}
        result = ComponentResult(
            success=not totals.refused,
            message=f"Security level access: {totals.granted} granted, {totals.verified} verified, "
            f"{len(totals.refused)} refused",
            details=details | {"refused": len(totals.refused)},
            errors=list(totals.refused),
            total_count=done + len(totals.refused),
            success_count=done,
            failed_count=len(totals.refused),
        )
        if totals.readers_without_user or totals.locked_readers:
            self.logger.warning(
                "Security level access: %d readers without an OpenProject user, %d locked readers got no membership",
                totals.readers_without_user,
                totals.locked_readers,
            )
        return result if level is None else level.report(result, self.logger)
