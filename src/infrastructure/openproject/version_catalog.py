# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Reconcile Jira release identities and metadata with a native OpenProject version catalogue."""

from __future__ import annotations

import base64
import json
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from src.infrastructure.openproject.openproject_client import OpenProjectClient

_PROJECT = """
Project.transaction do
  project = Project.lock.find(data.fetch('project'))
  source = data.fetch('source')
  expected = {'Jira Project Key'=>data.fetch('key'), 'Jira Project ID'=>data.fetch('jira_project_id'), 'Jira Base URL'=>source}
  expected.each do |name, value|
    field = ProjectCustomField.where(name: name).sole
    raise 'Wrong Jira project origin' unless project.custom_values.where(custom_field: field).sole.value == value
  end
"""

# A level sub-project (ADR-009) carries no Jira origin of its own: its parent does, and the
# sub-project has to be the private child of that parent.
_LEVEL_PROJECT = """
Project.transaction do
  project = Project.lock.find(data.fetch('project'))
  owner = Project.find(data.fetch('parent_id'))
  raise 'Unexpected target sub-project' unless project.parent_id == owner.id && !project.public?
  source = data.fetch('source')
  expected = {'Jira Project Key'=>data.fetch('key'), 'Jira Project ID'=>data.fetch('jira_project_id'), 'Jira Base URL'=>source}
  expected.each do |name, value|
    field = ProjectCustomField.where(name: name).sole
    raise 'Wrong Jira project origin' unless owner.custom_values.where(custom_field: field).sole.value == value
  end
"""

# The sub-project's own copy of each Jira version (ADR-010): sharing none, adopted by its origin on
# a repeat run. A copy is created or changed only while the sub-project has no membership row,
# locked users and work package shares included (ADR-005); the parent's versions are not read.
_LEVEL_SYNC = """
  staging = !project.members.exists? && !Member.where(project_id: project.id).exists?
  field = VersionCustomField.find_or_create_by!(name: 'Jira Version Origin') { |f| f.field_format = 'text' }
  raise 'Unexpected version origin field' unless field.field_format == 'text' && !field.multi_value?
  origins = CustomValue.where(customized_type: 'Version', customized_id: project.versions.select(:id), custom_field: field)
  by_source = {}
  origins.where.not(value: [nil, '']).each do |value|
    key = JSON.parse(value.value).values_at('source', 'id')
    raise 'Duplicate Jira version identity' if by_source.key?(key)
    by_source[key] = value.customized
  end
  changes = 0
  versions = data.fetch('versions').to_h do |row|
    attrs = row.fetch('attributes').merge('sharing' => 'none')
    version = by_source[[source, row.fetch('id')]]
    if version.nil?
      raise 'Unregistered native version collision' if project.versions.where('lower(name) = lower(?)', attrs.fetch('name')).exists?
      version = project.versions.new
    end
    raise 'Version shares data beyond its project' unless version.sharing == 'none'
    version.assign_attributes(attrs)
    changed = version.new_record? || version.changed?
    if changed
      raise 'Create or change level versions before granting project access' unless staging
      version.save!
    end
    origin = version.custom_values.find_or_initialize_by(custom_field: field)
    origin.value = JSON.generate({'source'=>source, 'id'=>row.fetch('id'), 'archived'=>row.fetch('archived'), 'released'=>row.fetch('released')})
    if origin.new_record? || origin.changed?
      raise 'Create or change level versions before granting project access' unless staging
      origin.save!
      changed = true
    end
    version.reload
    raise 'Level version readback failed' unless version.project_id == project.id && version.sharing == 'none'
    changes += 1 if changed
    [row.fetch('id'), version.id]
  end
  {versions: versions, changed: changes}
"""

_SYNC = """
  field = VersionCustomField.find_or_create_by!(name: 'Jira Version Origin') { |f| f.field_format = 'text' }
  raise 'Unexpected version origin field' unless field.field_format == 'text' && !field.multi_value?
  origins = CustomValue.where(customized_type: 'Version', customized_id: project.versions.select(:id), custom_field: field)
  by_source = {}
  origins.where.not(value: [nil, '']).each do |value|
    origin = JSON.parse(value.value)
    key = origin.values_at('source', 'id')
    raise 'Duplicate Jira version identity' if by_source.key?(key)
    by_source[key] = value.customized
  end
  changes = 0
  versions = data.fetch('versions').to_h do |row|
    identity = [source, row.fetch('id')]
    attrs = row.fetch('attributes')
    matches = project.versions.where('lower(name) = lower(?)', attrs.fetch('name'))
    raise 'Ambiguous native version name' if matches.many?
    version = by_source[identity] || matches.first || project.versions.new
    raise 'Version shares data beyond its project' unless version.sharing == 'none'
    origin = version.persisted? ? version.custom_values.find_by(custom_field: field) : nil
    if origin&.value.present?
      raise 'Conflicting Jira version identities' unless JSON.parse(origin.value).values_at('source', 'id') == identity
    elsif version.persisted?
      # Earlier imports created name-only versions. Do not replace unrelated target metadata.
      defaults = {'status'=>'open', 'description'=>'', 'effective_date'=>nil, 'start_date'=>nil}
      defaults.each do |attr, default|
        value = version.public_send(attr).as_json
        next if value == attrs[attr] || value == default || (attr == 'description' && value.blank?)
        raise 'Existing native version conflicts with the Jira catalogue'
      end
    end
    version.assign_attributes(attrs)
    changed = version.new_record? || version.changed?
    version.save! if changed
    origin ||= version.custom_values.find_or_initialize_by(custom_field: field)
    origin.value = JSON.generate({'source'=>source, 'id'=>row.fetch('id'), 'archived'=>row.fetch('archived'), 'released'=>row.fetch('released')})
    changed ||= origin.new_record? || origin.changed?
    origin.save! if origin.new_record? || origin.changed?
    changes += 1 if changed
    [version.name, version.id]
  end
  {versions: versions, changed: changes}
"""


def _native_version(row: dict[str, Any]) -> dict[str, Any]:
    name, source_id = row["name"], str(row["id"])
    released, archived = row["released"], row["archived"]
    if not (
        isinstance(name, str)
        and name.strip()
        and source_id.isdecimal()
        and isinstance(released, bool)
        and isinstance(archived, bool)
    ):
        msg = "Invalid Jira version identity or release flags"
        raise ValueError(msg)
    status = "open"
    if released:
        status = "closed"
    elif archived:
        status = "locked"
    attrs = {
        "name": name,
        "description": row.get("description") or "",
        "status": status,
    }
    for field, target in (("startDate", "start_date"), ("releaseDate", "effective_date")):
        value = row.get(field)
        attrs[target] = date.fromisoformat(value).isoformat() if value is not None else None
    return {"id": source_id, "archived": archived, "released": released, "attributes": attrs}


def _catalogue_payload(project: int, source: str, project_key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The payload of one non-empty catalogue; inconsistent or ambiguous rows raise."""
    project_ids = {str(row["projectId"]) for row in rows}
    if len(project_ids) != 1 or not next(iter(project_ids)).isdecimal():
        msg = "Inconsistent Jira project IDs in the version catalogue"
        raise ValueError(msg)
    versions = [_native_version(row) for row in rows]
    names = [v["attributes"]["name"] for v in versions]
    if len({v["id"] for v in versions}) != len(rows) or len({name.casefold() for name in names}) != len(rows):
        msg = "Ambiguous Jira project version catalogue"
        raise ValueError(msg)
    return {
        "project": project,
        "source": source.rstrip("/"),
        "key": project_key,
        "jira_project_id": project_ids.pop(),
        "versions": versions,
    }


def sync_project_versions(
    client: OpenProjectClient, project: int, source: str, project_key: str, rows: list[dict[str, Any]]
) -> tuple[dict[str, int], int]:
    """Reconcile one catalogue atomically and keep the flags that have no separate target field."""
    if not rows:
        return {}, 0
    payload = _catalogue_payload(project, source, project_key, rows)
    names = [v["attributes"]["name"] for v in payload["versions"]]
    result = version_project_query(client, payload, _SYNC)
    if not isinstance(result, dict) or set(result["versions"]) != set(names):
        msg = "Incomplete OpenProject version catalogue response"
        raise ValueError(msg)
    return result["versions"], result["changed"]


def sync_level_versions(
    client: OpenProjectClient, sub_project: tuple[int, int], source: str, project_key: str, rows: list[dict[str, Any]]
) -> dict[str, int]:
    """Jira version ID to the sub-project's own copy, created or adopted (ADR-010).

    ``sub_project`` is the level sub-project's ID and its parent's ID; ``rows`` are the Jira
    catalogue rows of the versions its work packages use.
    """
    if not rows:
        return {}
    project, parent = sub_project
    payload = _catalogue_payload(project, source, project_key, rows) | {"parent_id": parent}
    result = level_version_project_query(client, payload, _LEVEL_SYNC)
    copies = result.get("versions") if isinstance(result, dict) else None
    if (
        not isinstance(copies, dict)
        or set(copies) != {row["id"] for row in payload["versions"]}
        or not all(type(value) is int for value in copies.values())
    ):
        msg = "Incomplete OpenProject level version response"
        raise ValueError(msg)
    return copies


def level_version_project_query(client: OpenProjectClient, payload: dict, operation: str) -> object:
    """Lock the level sub-project; verify its parent, its privacy and the parent's source identity."""
    return client.execute_json_query(_script(payload, _LEVEL_PROJECT, operation))


def version_project_query(client: OpenProjectClient, payload: dict, operation: str) -> object:
    """Lock the mapped project and verify all three source identity fields before changing release data."""
    return client.execute_json_query(_script(payload, _PROJECT, operation))


def _script(payload: dict, preamble: str, operation: str) -> str:
    url = urlsplit(payload["source"])
    if url.scheme not in {"https", "http"} or not url.hostname or url.username is not None or url.query or url.fragment:
        msg = "Expected a credential-free Jira base URL for version provenance"
        raise ValueError(msg)
    data = base64.b64encode(json.dumps(payload).encode()).decode("ascii")
    return f"require 'base64'\ndata = JSON.parse(Base64.strict_decode64('{data}'))\n{preamble}\n{operation}\nend"
