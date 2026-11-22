# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Reconcile Jira release identities and metadata with a native OpenProject version catalogue."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from src.infrastructure.openproject.level_audience import LEVEL_AUDIENCE

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

# The rows of the level copies: Jira releases from the catalogue (fix and affected versions), or
# the parent's sprint versions, whose attributes the copy takes over.
# An audience copy (ADR-010, update 2026-11-22) carries its readers' user IDs in its origin.
_LEVEL_RELEASES = """
  rows = data.fetch('versions').map do |row|
    origin = {'source'=>source, 'id'=>row.fetch('id'), 'archived'=>row.fetch('archived'), 'released'=>row.fetch('released')}
    audience = row['audience']
    origin['audience'] = audience if audience
    {'id'=>row.fetch('id'), 'attributes'=>row.fetch('attributes'), 'origin'=>origin, 'audience'=>audience}
  end
"""

_LEVEL_SPRINTS = """
  rows = data.fetch('sprints').map do |row|
    sprint = owner.versions.find(row.fetch('version'))
    id = 'sprint:' + row.fetch('id')
    attrs = sprint.attributes.slice('name', 'description', 'status', 'start_date', 'effective_date')
    origin = {'source'=>source, 'id'=>id, 'version'=>sprint.id}
    audience = row['audience']
    if audience
      attrs['name'] = attrs.fetch('name') + row.fetch('suffix')
      origin['audience'] = audience
    end
    {'id'=>id, 'attributes'=>attrs, 'origin'=>origin, 'audience'=>audience}
  end
"""

# The sub-project's own copy of each row (ADR-010): sharing none, adopted by its origin on a
# repeat run. A copy is created or changed only while every membership row of the sub-project,
# locked users and work package shares included, is a grant of its readers (ADR-012; none
# before the grant, ADR-005); the parent's versions are not changed. An audience copy is also
# adopted unchanged only then: a share left over from earlier readers would otherwise see it.
# An adopted copy must carry exactly the row's audience, none for a static copy.
_LEVEL_COPIES = """
  staging = level_audience.call(project, data.fetch('audience'))
  field = VersionCustomField.find_or_create_by!(name: 'Jira Version Origin') { |f| f.field_format = 'text' }
  raise 'Unexpected version origin field' unless field.field_format == 'text' && !field.multi_value?
  origins = CustomValue.where(customized_type: 'Version', customized_id: project.versions.select(:id), custom_field: field)
  by_source = {}
  audiences = {}
  origins.where.not(value: [nil, '']).each do |value|
    parsed = JSON.parse(value.value)
    key = parsed.values_at('source', 'id')
    raise 'Duplicate Jira version identity' if by_source.key?(key)
    by_source[key] = value.customized
    audiences[key] = parsed['audience']
  end
  changes = 0
  versions = rows.to_h do |row|
    attrs = row.fetch('attributes').merge('sharing' => 'none')
    audience = row.fetch('audience')
    raise 'Use audience version copies only in a sub-project of its readers' if audience && !staging
    version = by_source[[source, row.fetch('id')]]
    raise 'Level version copy of another audience' if version && audiences[[source, row.fetch('id')]] != audience
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
    origin.value = JSON.generate(row.fetch('origin'))
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


_AUDIENCE = ":audience:"


def audience_token(users: Iterable[int]) -> str:
    """The first 12 hex digits of the SHA-256 of the sorted OpenProject user IDs, joined by commas."""
    ids = sorted(set(users))
    if not ids or not all(type(user) is int and user > 0 for user in ids):
        msg = "Invalid version audience"
        raise ValueError(msg)
    return hashlib.sha256(",".join(map(str, ids)).encode()).hexdigest()[:12]


def level_copy_id(source_id: str, users: Iterable[int] = ()) -> str:
    """The copy ID of a Jira version or sprint ID for an audience; the ID itself for static readers only.

    It is the ID in ``Jira Version Origin`` (after ``sprint:`` for a sprint) and the suffix of
    the mapping key (ADR-010).
    """
    ids = tuple(users)
    return f"{source_id}{_AUDIENCE}{audience_token(ids)}" if ids else source_id


def audience_suffix(users: Iterable[int]) -> str:
    """What the name of an audience copy adds to the source name: `` [shared <token>]``."""
    return f" [shared {audience_token(users)}]"


def _audience_rows(versions: list[dict[str, Any]], audiences: Mapping[str, Iterable[tuple[int, ...]]]) -> list:
    """Per catalogue row the copies its audiences need; the empty audience is the static copy."""
    rows = []
    for row in versions:
        for users in sorted({tuple(sorted(users)) for users in audiences.get(row["id"], ())}):
            if not users:
                rows.append(row)
                continue
            name = row["attributes"]["name"] + audience_suffix(users)
            copy = {"id": level_copy_id(row["id"], users), "attributes": row["attributes"] | {"name": name}}
            rows.append(row | copy | {"audience": list(users)})
    names = [row["attributes"]["name"].casefold() for row in rows]
    if len(set(names)) != len(names):
        msg = "Ambiguous level version copy names"
        raise ValueError(msg)
    return rows


def sync_level_versions(
    client: OpenProjectClient,
    sub_project: tuple[int, int, dict[str, Any]],
    source: str,
    project_key: str,
    rows: list[dict[str, Any]],
    *,
    audiences: Mapping[str, Iterable[tuple[int, ...]]] | None = None,
) -> dict[str, int]:
    """Copy ID to the sub-project's own copy, created or adopted (ADR-010).

    ``sub_project`` is the level sub-project's ID, its parent's ID and its staging audience
    (ADR-012); ``rows`` are the Jira catalogue rows of the versions its work packages use.
    ``audiences`` maps a Jira version ID to the user IDs of each audience that needs a copy,
    the empty tuple for static readers only; without it every row gets its static copy, keyed
    by its Jira version ID (see ``level_copy_id``).
    """
    if not rows:
        return {}
    project, parent, audience = sub_project
    payload = _catalogue_payload(project, source, project_key, rows) | {"parent_id": parent, "audience": audience}
    if audiences is not None:
        payload["versions"] = _audience_rows(payload["versions"], audiences)
    result = level_version_project_query(client, payload, _LEVEL_RELEASES + _LEVEL_COPIES)
    return _level_copies(result, {row["id"] for row in payload["versions"]})


def sync_level_sprints(
    client: OpenProjectClient,
    sub_project: tuple[int, int, dict[str, Any]],
    origin: tuple[str, str, str],
    sprints: dict[str, int],
    audiences: Mapping[str, tuple[int, ...]] | None = None,
) -> dict[str, int]:
    """Copy ID to the sub-project's copy of the parent's sprint version (ADR-010).

    ``origin`` is the Jira base URL, project key and project ID the parent must carry;
    ``sprints`` maps each copy ID (``level_copy_id`` of the Jira sprint ID) to the parent's
    version of that sprint, and ``audiences`` an audience copy ID to its user IDs.
    """
    if not sprints:
        return {}
    (project, parent, audience), (source, project_key, jira_project_id) = sub_project, origin
    audiences = audiences or {}
    if not jira_project_id.isdecimal() or not all(
        _valid_sprint_copy(cid, version, audiences.get(cid, ())) for cid, version in sprints.items()
    ):
        msg = "Invalid Jira sprint or project identity"
        raise ValueError(msg)
    payload = {
        "project": project,
        "parent_id": parent,
        "audience": audience,
        "source": source.rstrip("/"),
        "key": project_key,
        "jira_project_id": jira_project_id,
        "sprints": [_sprint_row(cid, version, audiences.get(cid, ())) for cid, version in sorted(sprints.items())],
    }
    result = level_version_project_query(client, payload, _LEVEL_SPRINTS + _LEVEL_COPIES)
    copies = _level_copies(result, {f"sprint:{sid}" for sid in sprints})
    return {sid.removeprefix("sprint:"): copy for sid, copy in copies.items()}


def _valid_sprint_copy(cid: str, version: object, users: tuple[int, ...]) -> bool:
    sid = cid.partition(_AUDIENCE)[0]
    return sid.isdecimal() and type(version) is int and level_copy_id(sid, users) == cid


def _sprint_row(cid: str, version: int, users: tuple[int, ...]) -> dict[str, Any]:
    row: dict[str, Any] = {"id": cid, "version": version}
    return row | {"audience": list(users), "suffix": audience_suffix(users)} if users else row


def _level_copies(result: object, ids: set[str]) -> dict[str, int]:
    copies = result.get("versions") if isinstance(result, dict) else None
    if not isinstance(copies, dict) or set(copies) != ids or not all(type(value) is int for value in copies.values()):
        msg = "Incomplete OpenProject level version response"
        raise ValueError(msg)
    return copies


def level_version_project_query(client: OpenProjectClient, payload: dict, operation: str) -> object:
    """Lock the level sub-project; verify its parent, its privacy and the parent's source identity."""
    return client.execute_json_query(_script(payload, LEVEL_AUDIENCE + _LEVEL_PROJECT, operation))


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
