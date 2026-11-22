# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Work packages a project already holds for Jira issues, read before the skeleton creates any (#482).

A lost ``work_package_mapping.json`` or a lost batch answer must not make a rerun create a work
package a second time. The skeleton writes the Jira issue ID as ``J2O Origin ID`` and the key as
``J2O Origin Key``. A work package matches an issue by its Origin ID; a work package without an
Origin ID matches by its Origin Key, as written before the ID was recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.components.security_level_projects_migration import _script
from src.domain import provenance_fields

# One row per work package of the project that carries a marker: [id, origin id, origin key].
_MARKED = (
    'fields = WorkPackageCustomField.where(name: input.fetch("fields")).pluck(:id, :name).to_h\n'
    'ids = WorkPackage.where(project_id: input.fetch("project_id")).pluck(:id)\n'
    "rows = {}\n"
    'CustomValue.where(customized_type: "WorkPackage", customized_id: ids, custom_field_id: fields.keys)'
    '.where.not(value: [nil, ""]).pluck(:customized_id, :custom_field_id, :value).each do |wp, cf, value|\n'
    "  (rows[wp] ||= {})[fields[cf]] = value\n"
    "end\n"
    'rows.sort.map { |wp, values| [wp, values[input.fetch("fields")[0]], values[input.fetch("fields")[1]]] }'
)


class AdoptionError(Exception):
    """OpenProject's answer about the existing work packages cannot be read."""


@dataclass(frozen=True)
class MarkedWorkPackages:
    """The marked work packages of one project, indexed by Origin ID and, without one, by Origin Key."""

    by_id: dict[str, list[int]]
    by_key: dict[str, list[int]]

    def match(self, jira_id: str, jira_key: str) -> list[int]:
        """Every work package that claims the issue; more than one is a conflict."""
        return sorted(set(self.by_id.get(jira_id, []) + self.by_key.get(jira_key, [])))


def read(op_client: Any, project_id: int) -> MarkedWorkPackages:
    """Read the marked work packages of the project; an unreadable answer raises."""
    rows = op_client.execute_query_to_json_file(
        _script(
            _MARKED,
            {"project_id": project_id, "fields": [provenance_fields.ORIGIN_ID, provenance_fields.ORIGIN_KEY]},
        )
    )
    if not isinstance(rows, list) or not all(_row(row) for row in rows):
        msg = "malformed answer for the marked work packages of the project"
        raise AdoptionError(msg)
    by_id: dict[str, list[int]] = {}
    by_key: dict[str, list[int]] = {}
    for wp, origin_id, origin_key in rows:
        if origin_id is not None:
            by_id.setdefault(origin_id, []).append(wp)
        elif origin_key is not None:
            by_key.setdefault(origin_key, []).append(wp)
    return MarkedWorkPackages(by_id, by_key)


def _row(row: object) -> bool:
    return (
        isinstance(row, list)
        and len(row) == 3
        and type(row[0]) is int
        and all(value is None or isinstance(value, str) for value in row[1:])
    )
