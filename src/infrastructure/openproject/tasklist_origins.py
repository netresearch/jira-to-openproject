# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Atomically write ordinary Markdown tasks and their operator-only task origins."""

import base64
import json
from typing import Any

from src.infrastructure.openproject.checklist_section import CHECKLIST_SECTION
from src.infrastructure.openproject.level_audience import LEVEL_AUDIENCE
from src.infrastructure.openproject.written_guard import WRITTEN_GUARD

_APPLY = r"""
require "digest"
project = Project.find(data.fetch("project"))
project.with_lock(requires_new: true) do
  raise "Private target required for tasklists" if project.public?
  # A level sub-project (ADR-009) has no origin of its own: its parent carries the Jira project's
  # origin, and every membership row of the sub-project, locked users and shares included, is
  # access; only the grants of its readers may exist (ADR-012).
  level = data.key?("parent")
  source = level ? Project.find(data.fetch("parent")) : project
  raise "Unexpected tasklist sub-project" if level && project.parent_id != source.id
  origin = {"Jira Project Key"=>data.fetch("key"), "Jira Project ID"=>data.fetch("source_id"), "Jira Base URL"=>data.fetch("base")}
  origins = lambda do |obj, type, names|
    obj.custom_values.joins(:custom_field).where(custom_fields: {type: type, name: names})
       .pluck("custom_fields.name", :value).group_by(&:first).transform_values { |v| v.sole.last }
  end
  actual = origins.call(source, "ProjectCustomField", origin.keys)
  # Older migrated projects predate the Jira Base URL attribute. In that case
  # each affected work package must still have its exact source URL, key and ID.
  expected = actual.key?("Jira Base URL") ? origin : origin.except("Jira Base URL")
  raise "Unexpected tasklist project" unless actual == expected
  Project.connection.execute("SELECT pg_advisory_xact_lock(17970449)")
  name = "Jira tasklist origins"
  field = ProjectCustomField.find_by(name: name)
  if field && !(field.admin_only? && !field.is_for_all? && field.field_format == "text")
    raise "Unsafe tasklist register"
  end
  value = field && project.custom_values.find_by(custom_field: field)
  saved = value ? JSON.parse(value.value) : {"source"=>origin, "issues"=>{}}
  raise "Foreign tasklist register" unless saved.fetch("source") == origin
  issues = saved.fetch("issues")
  marked = project.work_packages.where("description LIKE ?", "%<!-- j2o:checklist:%").ids.to_set
  updated = tasks = 0
  seen = Set.new
  digests = {}
  data.fetch("rows").each do |row|
    sid, key, wid, records, markdown = row.values_at("id","key","wp","tasks","markdown")
    raise "Duplicate tasklist work package" unless seen.add?(wid)
    prior = issues[sid]
    next if records.empty? && !prior && !marked.include?(wid)
    wp = project.work_packages.lock.find(wid)
    expect = {"J2O Origin ID"=>sid,"J2O Origin Key"=>key,"J2O Origin URL"=>data.fetch("base")+"/browse/"+key}
    raise "Unexpected tasklist work package" unless origins.call(wp,"WorkPackageCustomField",expect.keys) == expect
    raise "Registered tasklist target changed" if prior && (prior.fetch("wp") != wid || prior.fetch("key") != key)
    expected = checklist.call(wp.description.to_s, markdown, records.empty?, prior && prior.fetch("section_sha256"))
    changed = wp.description.to_s != expected
    if changed
      staging = level ? level_audience.call(project, data.fetch("audience")) : !project.members.exists?
      raise "Stage checklist changes before granting project access" unless staging
      # Rewriting the text J2O wrote last keeps its digest current (ADR-013).
      before = wp.description.to_s
      kept = row["expect"] ? j2o_digest.call(before) == row["expect"] : before.empty?
      wp.update_columns(description: expected)
      raise "Native checklist readback failed" unless wp.reload.description == expected
      digests[wid] = j2o_digest.call(expected) if kept
    end
    tasks += records.size
    if records.empty?
      updated += 1 if issues.delete(sid) || changed
    else
      section = checklist.call("",markdown,false,nil)
      entry = {"wp"=>wid,"key"=>key,"tasks"=>records,"section_sha256"=>Digest::SHA256.hexdigest(section)}
      updated += 1 if changed || prior != entry
      issues[sid] = entry
    end
  end
  if updated.positive? && (!issues.empty? || value)
    field ||= ProjectCustomField.create!(name: name, field_format: "text", admin_only: true, is_for_all: false,
      custom_field_section: ProjectCustomFieldSection.find_or_create_by!(name: "J2O Origin"))
    ProjectCustomFieldProjectMapping.find_or_create_by!(project_id: project.id, custom_field_id: field.id)
    value ||= project.custom_values.find_or_initialize_by(custom_field: field)
    value.update!(value: saved.to_json)
  end
  {updated: updated, tasks: tasks, digests: digests}
end
"""


def apply_tasklists(client: Any, project: dict) -> dict:
    """Validate project and issue origins inside the same transaction that persists all task records."""
    data = base64.b64encode(json.dumps(project, ensure_ascii=False).encode()).decode("ascii")
    query = (
        f'data = JSON.parse(Base64.strict_decode64("{data}"))\n'
        + WRITTEN_GUARD
        + CHECKLIST_SECTION
        + LEVEL_AUDIENCE
        + _APPLY
    )
    return client.execute_large_query_to_json_file(query, timeout=180)
