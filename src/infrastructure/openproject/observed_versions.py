# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Apply verified Jira affected versions to work packages, preserving independent native versions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.infrastructure.openproject.version_catalog import level_version_project_query, version_project_query

if TYPE_CHECKING:
    from src.infrastructure.openproject.openproject_client import OpenProjectClient

_WRITE = """
native = WorkPackage.method_defined?(:observed_in_version_ids_replacements=)
catalog = {}
registry = nil
if native
  registry = WorkPackageCustomField.find_or_create_by!(name:'Jira Imported Observed Versions') { |f| f.field_format='text'; f.is_for_all=false }
  raise 'Invalid observed version registry' unless registry.field_format == 'text' && !registry.multi_value? && !registry.is_for_all?
  field = VersionCustomField.find_by(name: 'Jira Version Origin')
  if field
    versions = CustomValue.where(custom_field: field, customized_type: 'Version', customized_id: project.versions.select(:id))
    versions.where.not(value: [nil, '']).each do |cv|
      origin = JSON.parse(cv.value)
      next unless origin.fetch('source') == source
      sid = origin.fetch('id')
      raise 'Ambiguous Jira version origin' if catalog.key?(sid)
      catalog[sid] = cv.customized_id
    end
  end
end
cf = WorkPackageCustomField.find(data.fetch('field'))
raise 'Wrong affected versions field' unless cf.name == 'Affects Versions' && cf.field_format == 'text'
by_origin = ['J2O Origin ID','J2O Origin Key','J2O Origin URL'].map { |name| WorkPackageCustomField.where(name: name).sole }
changes = 0
data.fetch('issues').each do |issue|
  wp = project.work_packages.lock.find(issue.fetch('wp'))
  expected = [issue.fetch('id'), issue.fetch('key'), source + '/browse/' + issue.fetch('key')]
  by_origin.zip(expected).each do |field, value|
    raise 'Wrong Jira issue origin' unless wp.custom_values.where(custom_field: field).sole.value == value
  end
  refs = issue.fetch('versions')
  text = refs.map { |r| r.fetch('name') }.uniq.sort.join(', ')
  cv = wp.custom_values.find_by(custom_field: cf)
  changed = false
  if native
    previous = wp.observed_in_version_ids
    required = refs.map { |r| catalog.fetch(r.fetch('id')) }.uniq
    marker = wp.custom_values.find_by(custom_field: registry)
    identity = [source, issue.fetch('id')]
    old = marker&.value.present? ? JSON.parse(marker.value) : {'origin'=>identity, 'ids'=>[]}
    raise 'Wrong observed version registry origin' unless old.fetch('origin') == identity
    independent = previous - old.fetch('ids')
    expected_ids = independent | required
    managed = required - independent
    if previous.sort != expected_ids.sort
      wp.observed_in_version_ids_replacements = expected_ids
      wp.save!
      raise 'Native observed versions readback failed' unless wp.reload.observed_in_version_ids.sort == expected_ids.sort
      changed = true
    end
    if managed.sort != old.fetch('ids').sort
      marker ||= wp.custom_values.find_or_initialize_by(custom_field: registry)
      marker.update!(value: JSON.generate({'origin'=>identity, 'ids'=>managed.sort}))
      changed = true
    end
  end
  if text.present?
    CustomFieldsProject.find_or_create_by!(custom_field: cf, project: project)
  end
  if cv&.value.to_s != text
    cv ||= wp.custom_values.find_or_initialize_by(custom_field: cf)
    cv.update!(value: text)
    raise 'Affected version text readback failed' unless cv.reload.value.to_s == text
    changed = true
  end
  changes += 1 if changed
end
{updated: changes, native: native}
"""


def write_observed_versions(client: OpenProjectClient, payload: dict, *, level: bool = False) -> dict:
    """Keep source text on earlier OpenProject versions and check native associations when supported.

    With ``level``, ``payload`` names a level sub-project and its parent (ADR-010): the parent's
    origin is checked, and the native links go to the sub-project's own version copies.
    """
    query = level_version_project_query if level else version_project_query
    result = query(client, payload, _WRITE)
    if not isinstance(result, dict) or type(result.get("updated")) is not int or type(result.get("native")) is not bool:
        msg = "Invalid affected version readback"
        raise ValueError(msg)
    return result
