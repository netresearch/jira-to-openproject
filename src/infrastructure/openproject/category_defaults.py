# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Apply verified category defaults and preserve source policies in a private project attribute."""

import base64
import json
from typing import Any

_APPLY = r"""
project = Project.find(data.fetch("project_id"))
project.with_lock(requires_new: true) do
  raise "Private target project required" if project.public?
  value_for = lambda do |model, object, name|
    field = model.where(name: name).sole
    object.custom_values.where(custom_field: field).sole.value
  end
  source = data.fetch("source")
  source.each do |name, value|
    raise "Project origin mismatch" unless value_for.call(ProjectCustomField, project, name) == value
  end
  private_field = lambda do |field|
    raise "Unsafe source policy register" unless field.field_format == "text" && !field.is_for_all? && field.admin_only?
  end
  Project.connection.execute("SELECT pg_advisory_xact_lock(17970449)")
  name = "Jira category default policies"
  field = ProjectCustomField.find_by(name: name)
  if data.fetch("rules").empty? && (!field || !project.custom_values.exists?(custom_field_id: field.id))
    next({updated: 0, native: 0, held: 0, preserved: 0})
  end
  field ||= ProjectCustomField.create!(name: name, field_format: "text", admin_only: true, is_for_all: false,
    custom_field_section: ProjectCustomFieldSection.find_or_create_by!(name: "J2O Origin"))
  private_field.call(field)
  ProjectCustomFieldProjectMapping.find_or_create_by!(project_id: project.id, custom_field_id: field.id)
  value = project.custom_values.find_or_initialize_by(custom_field: field)
  saved = value.persisted? ? JSON.parse(value.value) : {"source"=>source, "rules"=>{}}
  raise "Foreign category default policies" unless saved.fetch("source") == source
  old = saved.fetch("rules")
  category_field = ProjectCustomField.find_by(name: "Jira component membership origins")
  category_register = category_field && project.custom_values.find_by(custom_field: category_field)
  catalogue = {}
  if category_register
    private_field.call(category_field)
    parsed = JSON.parse(category_register.value)
    raise "Foreign category register" unless parsed.fetch("source") == source
    catalogue = parsed.fetch("components")
  end
  verified_user = lambda do |identity|
    next unless identity
    user = User.active.lock.find_by(id: identity.fetch("id"))
    next unless user && project.principals.exists?(id: user.id)
    expected = {"J2O User ID"=>identity.fetch("user_id"), "J2O User Key"=>identity.fetch("user_key"),
                "J2O External URL"=>identity.fetch("url")}
    fields = user.custom_values.joins(:custom_field).where(custom_fields: {type: "UserCustomField",
      name: expected.keys + ["J2O Origin System"]}).pluck("custom_fields.name", :value)
    actual = fields.to_h
    system = actual.delete("J2O Origin System").to_s.downcase
    jira_system = /\Ajira(?: (?:server(?: on-prem)?|data ?center|cloud))?(?: [0-9]+(?:\.[0-9]+)*(?:[-+][a-z0-9.-]+)?)?\z/
    next unless fields.size == expected.size + 1 && actual == expected && system.match?(jira_system)
    url_field = UserCustomField.where(name: "J2O External URL").sole
    aliases = CustomValue.where(custom_field: url_field, value: identity.fetch("url")).distinct.pluck(:customized_id)
    user.id if aliases == [user.id]
  end
  result = {updated: 0, native: 0, preserved: 0, held: 0}
  rules = {}
  data.fetch("rules").each do |row|
    origin, policy = row.values_at("source", "policy")
    sid = origin.fetch("id")
    category_info = catalogue[sid]
    prior = old[sid]
    if category_info && category_info.fetch("source") != origin
      raise "Unexpected native category origin"
    end
    category = category_info && project.categories.lock.find(category_info.fetch("category"))
    raise "Native category name changed" if category && category.name != origin.fetch("name")
    owner = prior && prior["owned"]
    if owner && (!category || category.id != prior["category"] || category.assigned_to_id != owner)
      raise "Native category default has changed independently"
    end
    user_id = verified_user.call(row["candidate"])
    desired = category && user_id
    native = false
    if category && (owner || category.assigned_to_id.nil?) && category.assigned_to_id != desired
      category.update!(assigned_to_id: desired)
      raise "Native category default was not persisted" unless category.reload.assigned_to_id == desired
      owner = desired
    end
    if desired && category.assigned_to_id == desired
      result[:native] += 1
      native = true
    end
    unassigned = policy["assigneeType"] == "UNASSIGNED" && policy["realAssigneeType"] == "UNASSIGNED" &&
                 policy["isAssigneeTypeValid"] == true && !policy["realAssignee"]
    result[:held] += 1 unless native || (unassigned && category && !category.assigned_to_id)
    rules[sid] = {"source"=>origin, "policy"=>policy, "category"=>category&.id, "owned"=>owner}
    result[:updated] += 1 if rules[sid] != prior
  end
  removed = old.keys - rules.keys
  removed.each do |sid|
    prior = old.fetch(sid)
    next unless prior["owned"]
    category = project.categories.lock.find(prior.fetch("category"))
    raise "Removed component default changed independently" unless category.assigned_to_id == prior.fetch("owned")
    category.update!(assigned_to_id: nil)
  end
  result[:updated] += removed.size
  payload = {"source"=>source, "project"=>data.fetch("project_policy"), "rules"=>rules}
  unless saved == payload
    value.update!(value: JSON.generate(payload))
    result[:updated] = 1 if result[:updated].zero?
  end
  result[:preserved] = rules.size
  result
end
"""


def apply_category_defaults(client: Any, project: dict) -> dict:
    """Reconcile category ownership and defaults in one project transaction."""
    payload = base64.b64encode(json.dumps(project).encode()).decode("ascii")
    return client.execute_large_query_to_json_file(f'data = JSON.parse(Base64.strict_decode64("{payload}"))\n{_APPLY}')
