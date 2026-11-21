# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Atomically store the first native category and ordered, admin-only Jira assignments."""

import base64
import json
from typing import Any

from src.domain.provenance_fields import JIRA_SOURCE_LABELS

_REGISTER = "Jira component membership origins"
_APPLY = """
project = Project.find(data.fetch("project_id"))
project.with_lock(requires_new: true) do
  raise "Component migration requires a private project" if project.public?
  # A level sub-project (ADR-009) has no origin of its own: its parent carries the Jira project's
  # origin, and every membership row of the sub-project, locked users and shares included, is access.
  level = data.key?("parent_id")
  owner = level ? Project.find(data.fetch("parent_id")) : project
  raise "Unexpected target sub-project" if level && project.parent_id != owner.id
  origins = lambda do |entity, field_type, names|
    entity.custom_values.joins(:custom_field).where(custom_fields: {type: field_type, name: names})
          .pluck("custom_fields.name", :value).group_by(&:first).transform_values { |rows| rows.sole.last }
  end
  source = {"Jira Project Key"=>data.fetch("key"), "Jira Project ID"=>data.fetch("jira_id"), "Jira Base URL"=>data.fetch("jira")}
  raise "Unexpected target project origin" unless origins.call(owner, "ProjectCustomField", source.keys) == source

  private_staging = !project.members.exists? && !(level && Member.where(project_id: project.id).exists?)
  existing_field = ProjectCustomField.find_by(name: data.fetch("register"))
  if data.fetch("issues").all? { |row| row.fetch("components").empty? } &&
      (!existing_field || !project.custom_values.exists?(custom_field_id: existing_field.id))
    next({updated: 0, categories: {}})
  end
  Project.connection.execute("SELECT pg_advisory_xact_lock(17970449)")
  section = ProjectCustomFieldSection.find_or_create_by!(name: "J2O Origin")
  field = ProjectCustomField.find_or_create_by!(name: data.fetch("register")) do |f|
    f.assign_attributes(field_format: "text", is_for_all: false, admin_only: true, custom_field_section: section)
  end
  raise "Unsafe component register" unless field.field_format == "text" && !field.is_for_all? && field.admin_only?
  ProjectCustomFieldProjectMapping.find_or_create_by!(project_id: project.id, custom_field_id: field.id)
  value = project.custom_values.find_or_initialize_by(custom_field: field)
  registry = value.persisted? ? JSON.parse(value.value) : {"source"=>source, "issues"=>{}, "components"=>{}}
  raise "Foreign component register" unless registry.fetch("source") == source
  catalogue = registry.fetch("components")
  issues = registry.fetch("issues")
  updated = 0
  verified = {}
  renamed = Set.new
  data.fetch("issues").each do |row|
    sid = row.fetch("id")
    old = issues[sid]
    refs = row.fetch("components")
    next if refs.empty? && !old
    wp = project.work_packages.lock.find(row.fetch("wp"))
    expected = {"J2O Origin ID"=>sid, "J2O Origin Key"=>row.fetch("key"),
                "J2O Origin URL"=>data.fetch("jira") + "/browse/" + row.fetch("key")}
    actual = origins.call(wp, "WorkPackageCustomField", expected.keys + ["J2O Origin System"])
    system = actual.delete("J2O Origin System").to_s.downcase
    raise "Unexpected work package origin" unless actual == expected && data.fetch("systems").include?(system)
    if old && (old.fetch("wp") != wp.id || old.fetch("key") != row.fetch("key") || wp.category_id != old["category"])
      raise "Existing component assignment changed"
    end
    ids = refs.map do |ref|
      id, name = ref.values_at("id", "name")
      saved = catalogue[id]
      old_source = saved && saved.fetch("source")
      if old_source && old_source.except("name") != ref.except("name")
        raise "Registered component origin changed"
      end
      unless verified.key?(id)
        known = saved ? saved.fetch("category") : data.fetch("legacy")[name]
        if known
          category = project.categories.lock.find(known)
          raise "Native category changed" unless category.name == (old_source ? old_source.fetch("name") : name)
          if old_source && category.name != name
            raise "Rename categories before granting project access" unless private_staging
            category.update!(name: name)
            renamed.add(id)
          end
        else
          raise "Unregistered native category collision" if project.categories.where("lower(name) = lower(?)", name).exists?
          raise "Create categories before granting project access" unless private_staging
          category = project.categories.create!(name: name)
        end
        catalogue[id] = {"source"=>ref, "category"=>category.id}
        verified[id] = category.id
      end
      verified.fetch(id)
    end
    first = ids.first
    raise "Existing category conflicts with Jira" if !old && wp.category_id && wp.category_id != first
    entry = {"key"=>row.fetch("key"), "wp"=>wp.id, "components"=>refs.pluck("id"), "category"=>first}
    next if old == entry
    if wp.category_id != first
      raise "Assign categories before granting project access" unless private_staging
      wp.update_columns(category_id: first)
    end
    raise "Native category was not persisted" unless wp.reload.category_id == first
    issues[sid] = entry
    updated += 1
  end
  targets = catalogue.values.pluck("category")
  raise "Ambiguous component origin" unless targets.uniq == targets
  updated += renamed.size
  value.update!(value: JSON.generate(registry)) if updated.positive?
  {updated: updated, categories: catalogue.values.to_h { |r| [r.fetch("source").fetch("name"), r.fetch("category")] }}
end
"""


def apply_component_assignments(client: Any, project: dict, legacy: dict) -> dict:
    """The Rails transaction checks source identities, target scope, category and register together."""
    data = {**project, "legacy": legacy, "register": _REGISTER, "systems": JIRA_SOURCE_LABELS}
    payload = base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode("ascii")
    return client.execute_large_query_to_json_file(
        f'data = JSON.parse(Base64.strict_decode64("{payload}"))\n{_APPLY}', timeout=180
    )
