# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Write private native board snapshots through the structured Rails transport."""

from src.infrastructure.openproject.openproject_client import OpenProjectClient

_SCRIPT = """
d = input_data
outcome = Project.transaction(requires_new: true) do
  archive = Project.lock.find_by!(identifier: d.fetch('archive'))
  target = Project.lock.find_by!(identifier: d.fetch('target'))
  raise 'Board archive must be a separate private root project without members' if
    archive == target || archive.public? || archive.parent_id || archive.members.exists? || target.public?
  raise 'Target work package tracking is disabled' unless target.module_enabled?(:work_package_tracking)
  archive.enabled_modules.find_or_create_by!(name: 'work_package_tracking')
  archive.enabled_modules.find_or_create_by!(name: 'board_view')
  user = User.admin.active.order(:id).first!
  User.execute_as(user) do
    fields = %w[Key ID URL System].to_h { |n| [n, WorkPackageCustomField.where(name: "J2O Origin #{n}").sole.id] }
    origin_ids = d.fetch('cards').flatten.map { |ref| ref.fetch('id') }
    raise 'Duplicate board card' unless origin_ids.uniq == origin_ids
    by_origin = target.work_packages.joins(:custom_values).where(custom_values: {
      custom_field_id: fields.fetch('ID'), value: origin_ids
    }).lock('FOR UPDATE OF work_packages').preload(:custom_values).group_by { |wp| wp.custom_values.find { |cv| cv.custom_field_id == fields['ID'] }.value }
    resolve = lambda do |card|
      wp = by_origin.fetch(card.fetch('id')).sole
      expected = {'Key'=>card.fetch('key'), 'ID'=>card.fetch('id'), 'System'=>'Jira', 'URL'=>d.fetch('jira')+'/browse/'+card.fetch('key')}
      raise 'Card provenance differs' unless expected.all? { |key, value|
        wp.custom_values.select { |cv| cv.custom_field_id == fields.fetch(key) }.sole.value == value
      }
      wp.id
    end
    revision = d.fetch('revision')
    name = "[Jira #{d.fetch('project')} #{d.fetch('board')} #{revision.first(12)}] #{d.fetch('name')}".truncate(255)
    existing = Boards::Grid.where(project: archive, name: name).limit(2).to_a
    raise 'Ambiguous board name' if existing.size > 1
    grid = existing.first || Boards::Grid.new(project: archive, name: name)
    fresh = grid.new_record?
    columns = d.fetch('configuration').fetch('columnConfig').fetch('columns')
    marker = d.slice('jira', 'project', 'board', 'name', 'configuration', 'revision').merge('target' => target.id)
    if fresh
      grid.assign_attributes(row_count: 1, column_count: columns.size, options: {'type'=>'free', 'jiraSource'=>marker})
      grid.save!
    end
    raise 'Existing layout differs' unless grid.options['jiraSource'] == marker && grid.column_count == columns.size
    count = 0
    columns.each_with_index do |column, index|
      ids = d.fetch('cards').fetch(index).map(&resolve)
      if fresh
        list = Query.new(project: target, user: user, public: false, name: column.fetch('name'), include_subprojects: false)
        list.filters = []
        list.add_filter('manual_sort', 'ow', [])
        list.sort_criteria = [['manual_sorting','asc'], ['id','asc']]
        list.save!
        ids.each_with_index { |id, i| list.ordered_work_packages.create!(work_package_id: id, position: i+1) }
        grid.widgets.create!(start_row: 1, end_row: 2, start_column: index+1, end_column: index+2,
          identifier: 'work_package_query', options: {'queryId'=>list.id, 'filters'=>[{'manual_sort'=>{'operator'=>'ow','values'=>[]}}]})
      end
      widget = grid.widgets.where(start_column: index+1).sole
      raise 'Widget layout differs' unless widget.start_row == 1 && widget.end_row == 2 &&
        widget.end_column == index+2 && widget.identifier == 'work_package_query'
      list = Query.lock.find(widget.options.fetch('queryId'))
      raise 'Board column audience differs' if list.public? || list.user != user || list.project != target || Member.where(entity: list).exists?
      raise 'Unexpected column filter' unless !list.include_subprojects &&
        list.filters.map { |f| [f.name.to_s, f.operator, f.values] } == [['manual_sort','ow',[]]]
      raise 'Unexpected card ordering' unless list.sort_criteria == [['manual_sorting','asc'], ['id','asc']]
      raise 'Board column name or rank differs' unless list.name == column.fetch('name') && list.results.work_packages.pluck(:id) == ids
      count += ids.size
    end
    raise 'Unexpected board columns' unless grid.widgets.count == columns.size
    {created: fresh, grid_id: grid.id, columns: columns.size, cards: count}
  end
end
puts $j2o_start_marker, outcome.to_json, $j2o_end_marker
"""


def save_layout(client: OpenProjectClient, board: dict, target: str, archive: str) -> dict:
    """Create an immutable board revision or verify the existing native record."""
    response = client.execute_script_with_data(_SCRIPT, {**board, "target": target, "archive": archive}, timeout=300)
    result = response.get("data") or {}
    if response.get("status") != "success" or "grid_id" not in result:
        msg = "OpenProject could not verify the native board snapshot"
        raise RuntimeError(msg)
    return result
