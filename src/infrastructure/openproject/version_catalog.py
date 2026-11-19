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

_SYNC = """
Project.transaction do
  project = Project.lock.find(data.fetch('project'))
  source = data.fetch('source')
  expected = {'Jira Project Key'=>data.fetch('key'), 'Jira Project ID'=>data.fetch('jira_project_id'), 'Jira Base URL'=>source}
  expected.each do |name, value|
    field = ProjectCustomField.where(name: name).sole
    raise 'Wrong Jira project origin' unless project.custom_values.where(custom_field: field).sole.value == value
  end
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
end
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


def sync_project_versions(
    client: OpenProjectClient, project: int, source: str, project_key: str, rows: list[dict[str, Any]]
) -> tuple[dict[str, int], int]:
    """Reconcile one catalogue atomically and keep the flags that have no separate target field."""
    if not rows:
        return {}, 0
    url = urlsplit(source)
    if url.scheme not in {"https", "http"} or not url.hostname or url.username is not None or url.query or url.fragment:
        msg = "Expected a credential-free Jira base URL for version provenance"
        raise ValueError(msg)
    project_ids = {str(row["projectId"]) for row in rows}
    if len(project_ids) != 1 or not next(iter(project_ids)).isdecimal():
        msg = "Inconsistent Jira project IDs in the version catalogue"
        raise ValueError(msg)
    versions = [_native_version(row) for row in rows]
    names = [v["attributes"]["name"] for v in versions]
    if len({v["id"] for v in versions}) != len(rows) or len({name.casefold() for name in names}) != len(rows):
        msg = "Ambiguous Jira project version catalogue"
        raise ValueError(msg)
    payload = json.dumps(
        {
            "project": project,
            "source": source.rstrip("/"),
            "key": project_key,
            "jira_project_id": project_ids.pop(),
            "versions": versions,
        }
    ).encode()
    data = base64.b64encode(payload).decode("ascii")
    result = client.execute_json_query(
        f"require 'base64'\ndata = JSON.parse(Base64.strict_decode64('{data}'))\n{_SYNC}"
    )
    if not isinstance(result, dict) or set(result["versions"]) != set(names):
        msg = "Incomplete OpenProject version catalogue response"
        raise ValueError(msg)
    return result["versions"], result["changed"]
