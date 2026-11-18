# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

# Apply one frozen Jira project's completed sprints on an OpenProject 17.9 target.
class JiraCompletedSprints
  class ImportConflict < StandardError; end

  REGISTER = "J2O completed sprint origins"
  HISTORY = "Jira sprint membership history"
  ATTRIBUTES = %w[id originBoardId name state startDate endDate activatedDate completeDate goal].freeze

  def initialize(manifest)
    @source = manifest
    @project = Project.find(manifest.fetch("targetProjectId"))
    @key = manifest.fetch("project").fetch("key")
    @jira = manifest.fetch("jira")
    @names_by_id = Hash.new { |names, id| names[id] = Set.new }
  end

  def apply
    @project.with_lock(requires_new: true) do
      # Serialize definition of the shared metadata field across independent project imports.
      Project.connection.execute("SELECT pg_advisory_xact_lock(17970160)")
      check_project!
      issues = memberships
      catalogue = sprint_catalogue(issues.flat_map(&:last).to_set)
      registry = origin_register
      stored = registry.persisted? ? JSON.parse(registry.value) : nil
      origins = @source.fetch("issues").map { |issue| issue.slice("id", "key", "sprintIds") }
      snapshot = { "jira" => @jira, "project" => @source.fetch("project").slice("id", "key"),
                   "sprints" => catalogue, "issues" => origins.sort_by { |issue| issue.fetch("key") } }
      raise ImportConflict, "Frozen sprint export changed" if stored && stored.fetch("source") != snapshot

      if !stored && Sprint.where(project: @project, name: catalogue.values.pluck("name")).exists?
        raise ImportConflict, "Existing native sprint has no registered Jira origin"
      end
      @changes = 0
      @project.enabled_modules.find_or_create_by!(name: "backlogs")
      targets = catalogue.to_h do |sid, source|
        [sid, native_sprint(source, stored&.fetch("targets")&.fetch(sid)).id]
      end
      if targets.values.uniq.size != targets.size || (stored && targets != stored.fetch("targets"))
        raise ImportConflict, "Ambiguous native sprint provenance"
      end
      issues.each { |wid, ids| assign(wid, ids, catalogue, targets) }
      unless stored
        registry.update!(value: JSON.generate({ "source" => snapshot, "targets" => targets }))
      end
      @project.touch if @changes.positive?
      { sprints: targets.size, work_packages: issues.size,
        memberships: issues.values.sum(&:size), changes: @changes }
    end
  end

  private

  def check_project!
    raise ImportConflict, "Expected a closed private target project" if @project.public? || @project.members.exists?
    raise ImportConflict, "Sprint sharing must be off" unless @project.sprint_sharing == "no_sharing"
    origins = { "Jira Project Key" => @key, "Jira Project ID" => @source.fetch("project").fetch("id").to_s,
                "Jira Base URL" => @jira }
    raise ImportConflict, "Invalid Jira project key" unless @key.match?(/\A[A-Z][A-Z0-9_]*\z/)
    numeric_id(origins.fetch("Jira Project ID"))
    origins.each do |name, expected|
      raise ImportConflict, "Missing Jira origin" unless expected.is_a?(String) && expected.present?
      field = ProjectCustomField.where(name: name).sole
      actual = @project.custom_values.where(custom_field_id: field.id).sole.value
      raise ImportConflict, "Unexpected project origin" unless actual == expected
    end
  end

  def memberships
    field = WorkPackageCustomField.where(name: HISTORY).sole
    origin = WorkPackageCustomField.where(name: "J2O Origin ID").sole
    url = WorkPackageCustomField.where(name: "J2O Origin URL").sole
    issues = {}
    @source.fetch("issues").each do |issue|
      key = issue.fetch("key")
      raise ImportConflict, "Issue belongs to another Jira project" unless key.match?(/\A#{Regexp.escape(@key)}-[1-9][0-9]*\z/)
      sid = numeric_id(issue.fetch("id"))
      wp = @project.work_packages.joins(:custom_values)
                   .where(custom_values: {custom_field_id: origin.id, value: sid}).sole
      actual_url = wp.custom_values.where(custom_field_id: url.id).sole.value
      raise ImportConflict, "Duplicate issue or incorrect origin URL" if issues.key?(wp.id) || actual_url != "#{@jira}/browse/#{key}"
      ids = issue.fetch("sprintIds").map { |id| numeric_id(id) }
      raise ImportConflict, "Empty or duplicate memberships" if ids.empty? || ids.uniq != ids
      history = JSON.parse(wp.custom_values.where(custom_field_id: field.id).sole.value)
      actual = history.map { |s| numeric_id(s.fetch("id")) }
      raise ImportConflict, "Source sprint membership differs from work-package history" unless actual == ids
      history.each { |s| @names_by_id[numeric_id(s.fetch("id"))] << s.fetch("name") }
      issues[wp.id] = ids
    end
    existing = @project.work_packages.joins(:custom_values).where(custom_values: {custom_field_id: field.id})
                       .where.not(custom_values: {value: [nil, "", "[]"]}).pluck(:id)
    raise ImportConflict, "Incomplete work-package membership export" unless issues.keys.sort == existing.sort
    issues
  end

  def numeric_id(value)
    id = value.to_s
    raise ImportConflict, "Invalid Jira ID" unless id.match?(/\A[1-9][0-9]*\z/)
    id
  end

  def sprint_catalogue(referenced)
    result = {}
    @source.fetch("sprints").each do |row|
      sid = numeric_id(row.fetch("id"))
      board = numeric_id(row.fetch("originBoardId"))
      keys = @source.fetch("boardProjectKeys").fetch(board)
      owned = keys == [@key]
      unless keys.is_a?(Array) && keys.include?(@key) && (owned || referenced.include?(sid))
        raise ImportConflict, "Unknown or mixed board scope"
      end
      if result.key?(sid) || row.fetch("state") != "closed" || row["goal"].present?
        raise ImportConflict, "Duplicate or unsupported sprint"
      end
      raise ImportConflict, "Sprint name missing" unless row.fetch("name").is_a?(String) && row["name"].present?
      raise ImportConflict, "Shared-board name differs from issue history" unless owned || @names_by_id[sid] == Set[row.fetch("name")]
      result[sid] = row.slice(*ATTRIBUTES).merge("projectOwnedBoard" => owned)
    end
    raise ImportConflict, "Incomplete sprint catalogue" unless referenced.subset?(result.keys.to_set)
    result
  end

  def origin_register
    section = ProjectCustomFieldSection.find_or_create_by!(name: "J2O Origin")
    field = ProjectCustomField.find_or_create_by!(name: REGISTER) do |f|
      f.assign_attributes(field_format: "text", is_for_all: false, admin_only: true,
                          custom_field_section_id: section.id)
    end
    unless field.admin_only? && !field.is_for_all? && field.field_format == "text"
      raise ImportConflict, "Unexpected sprint register definition"
    end
    ProjectCustomFieldProjectMapping.find_or_create_by!(project_id: @project.id, custom_field_id: field.id)
    @project.custom_values.find_or_initialize_by(custom_field_id: field.id)
  end

  def instant(row, field)
    value = row.fetch(field)
    raise ImportConflict, "Expected an offset on each sprint timestamp" unless value.match?(/(?:Z|[+-][0-9]{2}:?[0-9]{2})\z/)
    Time.iso8601(value)
  end

  def native_sprint(row, previous_id)
    attributes = { name: row.fetch("name"), status: "completed", start_date: instant(row, "startDate").to_date,
                   finish_date: instant(row, "endDate").to_date, started_at: instant(row, "activatedDate"),
                   completed_at: instant(row, "completeDate") }
    if previous_id
      sprint = Sprint.where(project_id: @project.id, id: previous_id).sole
      matches = attributes.all? { |key, value| sprint.public_send(key) == value }
      raise ImportConflict, "Native sprint changed" unless matches && sprint.goals.empty?
      return sprint
    end
    @changes += 1
    Sprint.create!(**attributes, project: @project)
  end

  def assign(wid, ids, catalogue, targets)
    wp = @project.work_packages.lock.find(wid)
    latest = ids.max_by { |id| [instant(catalogue.fetch(id), "completeDate"), id.to_i] }
    sprint_id = targets.fetch(latest)
    return if wp.sprint_id == sprint_id
    raise ImportConflict, "Existing native sprint assignment conflicts" if wp.sprint_id
    wp.update_columns(sprint_id: sprint_id)
    @changes += 1
  end
end

if $PROGRAM_NAME == __FILE__
  puts JSON.generate(JiraCompletedSprints.new(JSON.parse($stdin.read)).apply)
end
