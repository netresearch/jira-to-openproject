# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Archive a Jira instance's Tempo catalogue in one operator-selected private OpenProject project."""

from __future__ import annotations

import hashlib
from typing import Any

from src.comment_archive.archive import encoded
from src.utils.tempo_attribute_archive import open_document, seal_document

SETTING = "J2O_TEMPO_CATALOGUE_PROJECT"
_FORMAT = "j2o-tempo-instance-work-attributes-v1"

_STORE = """
require 'digest'
begin
  result = Project.transaction(requires_new: true) do
    project = Project.lock.find_by!(identifier: input_data.fetch('project'))
    raise 'Expected a private destination' if project.public?
    name = 'Jira instance Tempo work attributes (age)'
    scope = ProjectCustomField.where(name: name)
    field = scope.sole if scope.exists?
    replacement = input_data['replacement']
    if replacement && !field
      Project.connection.select_value('SELECT pg_advisory_xact_lock(124481108, 2)')
      section = ProjectCustomFieldSection.find_or_create_by!(name: 'J2O Operator Archives')
      ProjectCustomField.find_or_create_by!(name: name) do |f|
        f.assign_attributes(field_format: 'text', admin_only: true, is_for_all: false, is_required: false, custom_field_section: section)
      end
      field = scope.sole
    end
    next({packet: nil}) unless field
    raise 'Incompatible catalogue field' unless field.admin_only? && !field.is_for_all? && field.field_format == 'text'
    values = project.custom_values.where(custom_field_id: field.id)
    value = values.exists? ? values.sole : values.build
    packet = value.value
    packet = nil if packet == ''
    mapping = ProjectCustomFieldProjectMapping.where(project_id: project.id, custom_field_id: field.id)
    raise 'Unmapped catalogue' if packet && !mapping.exists?
    if replacement
      checksum = packet && Digest::SHA256.hexdigest(packet)
      next({conflict: true}) unless checksum == input_data['expected']
      mapping.first_or_create!
      value.update!(value: replacement)
      packet = values.sole.value
    end
    {packet: packet, field: field.id}
  end
rescue StandardError
  result = {error: 'Cannot access a compatible private catalogue field in the selected project'}
end
puts $j2o_start_marker, result.to_json, $j2o_end_marker
"""


def _storage(op: Any, project: str, **values: Any) -> dict:
    response = op.execute_script_with_data(_STORE, {"project": project, **values})
    result = response.get("data") or {}
    if response.get("status") != "success" or ("packet" not in result and result.get("conflict") is not True):
        msg = "Cannot verify the Tempo catalogue field in the selected private OpenProject project."
        raise ValueError(msg)
    return result


def _history(packet: str | None, jira: str) -> dict:
    if not packet:
        return {"format": _FORMAT, "scope": "jira-instance", "jira": jira, "snapshots": []}
    value = open_document(packet)
    if value.get("format") != _FORMAT or value.get("scope") != "jira-instance" or value.get("jira") != jira:
        msg = "The destination catalogue belongs to another source or format."
        raise ValueError(msg)
    if not isinstance(value.get("snapshots"), list) or not value["snapshots"]:
        msg = "The previous catalogue contains no recoverable snapshots."
        raise ValueError(msg)
    return value


def archive_catalogue(jira: Any, op: Any, project: str) -> dict:
    """Append a verified catalogue revision or confirm the existing one, including on a concurrent retry."""
    snapshot = jira.tempo.work_attribute_snapshot()
    if encoded(jira.tempo.work_attribute_snapshot()) != encoded(snapshot):
        msg = "The source Tempo catalogue changed during the capture."
        raise ValueError(msg)
    counts = {
        "verified": True,
        "scope": "jira-instance",
        "definitions": len(snapshot),
        "static_options": sum(len(r["static_options"]) for r in snapshot),
        "external_options_not_enumerated": sum(
            r["definition"]["type"]["value"] == "DYNAMIC_DROPDOWN" for r in snapshot
        ),
    }
    source = jira.base_url.rstrip("/")
    for _ in range(3):
        current = _storage(op, project)
        packet = current["packet"]
        history = _history(packet, source)
        versions = history["snapshots"]
        if versions and encoded(versions[-1]) == encoded(snapshot):
            return {**counts, "revisions": len(versions), "written": False, "field": current["field"]}
        versions.append(snapshot)
        checksum = hashlib.sha256(packet.encode("ascii")).hexdigest() if packet else None
        result = _storage(op, project, expected=checksum, replacement=seal_document(history))
        if result.get("conflict"):
            continue
        if encoded(_history(result["packet"], source)) == encoded(history):
            return {**counts, "revisions": len(versions), "written": True, "field": result["field"]}
        msg = "OpenProject returned a different Tempo catalogue after writing."
        raise ValueError(msg)
    msg = "The destination Tempo catalogue changed concurrently."
    raise ValueError(msg)
