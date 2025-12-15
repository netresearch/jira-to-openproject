# Multi-WP batch journal creation for optimized migration
# This script processes multiple work packages' journals in ONE Rails call
#
# OPTIMIZED VERSION: Uses pre-computed values from Python
# Python pre-computes: version, validity_period_start/end, field_changes mapping
# Ruby only: reads WP initial state, applies field_changes, bulk INSERT
#
# Expected variables:
# - input_data: Array of {wp_id:, jira_key:, rails_ops:} hashes
# - rails_ops contain: version, validity_period_start, validity_period_end, field_changes, user_id, notes
#
# Output: JSON with results per WP
# {"results": [{"wp_id": X, "jira_key": "Y", "created": N, "error": null}, ...]}

require 'json'

results = []

if input_data && input_data.respond_to?(:each)
  conn = ActiveRecord::Base.connection

  # Cache lookups shared across all WPs (one-time cost)
  workflow_cf = CustomField.find_by(name: "J2O Jira Workflow")
  resolution_cf = CustomField.find_by(name: "J2O Jira Resolution")
  affects_version_cf = CustomField.find_by(name: "J2O Affects Version")
  j2o_cf_ids = [workflow_cf&.id, resolution_cf&.id, affects_version_cf&.id].compact
  workflow_cf_id = workflow_cf&.id
  resolution_cf_id = resolution_cf&.id
  affects_version_cf_id = affects_version_cf&.id

  priority_cache = {}
  IssuePriority.all.each { |p| priority_cache[p.name.downcase] = p.id }

  valid_journal_attributes = [
    :type_id, :project_id, :subject, :description, :due_date, :category_id,
    :status_id, :assigned_to_id, :priority_id, :version_id, :author_id,
    :done_ratio, :estimated_hours, :start_date, :parent_id,
    :schedule_manually, :ignore_non_working_days
  ].freeze

  # Lambda: Apply field_changes to state hash
  apply_field_changes_to_state = lambda do |current_state, field_changes, priority_cache, rec|
    return current_state unless field_changes && field_changes.is_a?(Hash)
    field_changes.each do |k, v|
      field_sym = k.to_sym
      next unless valid_journal_attributes.include?(field_sym)
      new_value = v.is_a?(Array) ? v[1] : v
      next if new_value.nil?
      next if new_value.is_a?(String) && new_value.empty?
      next if new_value.is_a?(Array)

      # Special handling for priority_id - resolve string name to ID
      if field_sym == :priority_id && new_value.is_a?(String) && !(new_value =~ /^\d+$/)
        resolved = priority_cache[new_value.downcase]
        new_value = resolved if resolved
      end

      next unless new_value.is_a?(Integer) || new_value.is_a?(String) ||
                  new_value.is_a?(TrueClass) || new_value.is_a?(FalseClass) ||
                  new_value.is_a?(Float) || new_value.is_a?(Date) ||
                  new_value.is_a?(Time) || new_value.is_a?(Numeric)
      current_state[field_sym] = new_value.is_a?(String) && new_value =~ /^\d+$/ ? new_value.to_i : new_value
    end
    current_state
  end

  # Lambda: Ensure required fields have valid defaults from WP
  ensure_required_fields = lambda do |state, rec|
    state = state.transform_keys(&:to_sym) if state.is_a?(Hash) && state.keys.first.is_a?(String)
    state[:priority_id] ||= rec.priority_id
    state[:type_id] ||= rec.type_id
    state[:status_id] ||= rec.status_id
    state[:project_id] ||= rec.project_id
    state[:author_id] ||= rec.author_id
    state[:schedule_manually] = rec.schedule_manually if state[:schedule_manually].nil?
    state[:ignore_non_working_days] = rec.ignore_non_working_days if state[:ignore_non_working_days].nil?
    state
  end

  # Lambda: Sanitize ID fields
  sanitize_id_field = lambda do |value, cache, fallback|
    return fallback if value.nil?
    return value if value.is_a?(Integer)
    return value.to_i if value.to_s =~ /^\d+$/
    cache[value.to_s.downcase] || fallback
  end

  input_data.each_with_index do |wp_data, batch_idx|
    wp_id = wp_data['wp_id'] || wp_data[:wp_id]
    jira_key = wp_data['jira_key'] || wp_data[:jira_key]
    rails_ops = wp_data['rails_ops'] || wp_data[:rails_ops]

    result = { 'wp_id' => wp_id, 'jira_key' => jira_key, 'created' => 0, 'error' => nil }

    begin
      rec = WorkPackage.find_by(id: wp_id)
      unless rec
        result['error'] = "WP not found"
        results << result
        next
      end

      # Skip if no operations
      unless rails_ops && rails_ops.respond_to?(:each) && rails_ops.any?
        results << result
        next
      end

      # Delete v2+ journals for idempotent re-migration
      v2_plus_journals = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').where('version > 1')
      v2_plus_count = v2_plus_journals.count

      if v2_plus_count > 0
        v2_plus_ids = v2_plus_journals.pluck(:id)
        if v2_plus_ids.any?
          Journal::CustomizableJournal.where(journal_id: v2_plus_ids).delete_all
          data_ids = v2_plus_journals.pluck(:data_id).compact
          v2_plus_journals.delete_all
          Journal::WorkPackageJournal.where(id: data_ids).delete_all if data_ids.any?
        end
      end

      # Operations are already sorted by Python, use as-is
      ops = rails_ops

      # Get base version for this WP
      base_version = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').maximum(:version) || 0

      # Initialize state from WP record (Ruby has DB access)
      current_state = {
        type_id: rec.type_id, project_id: rec.project_id, subject: rec.subject,
        description: rec.description, due_date: rec.due_date, category_id: rec.category_id,
        status_id: rec.status_id, assigned_to_id: rec.assigned_to_id, priority_id: rec.priority_id,
        version_id: rec.version_id, author_id: rec.author_id, done_ratio: rec.done_ratio,
        estimated_hours: rec.estimated_hours, start_date: rec.start_date, parent_id: rec.parent_id,
        schedule_manually: rec.schedule_manually, ignore_non_working_days: rec.ignore_non_working_days
      }

      # Collect journal data using pre-computed values from Python
      bulk_journals = []
      v1_journal = nil
      v1_cf_snapshot = nil

      ops.each_with_index do |op, op_idx|
        op_type = op['type'] || op[:type]
        next if op_type == 'set_journal_user'

        notes = op['notes'] || op[:notes] || ''
        field_changes = op['field_changes'] || op[:field_changes]

        # Skip empty operations (except first which updates v1)
        is_empty = (notes.nil? || notes.to_s.strip.empty?) && (field_changes.nil? || field_changes.empty?)
        next if is_empty && op_idx != 0

        # Use pre-computed user_id from Python
        raw_user_id = (op['user_id'] || op[:user_id]).to_i
        fallback_user_id = rec.author_id && rec.author_id > 0 ? rec.author_id : 2
        user_id = raw_user_id > 0 ? raw_user_id : fallback_user_id

        # Use pre-computed timestamps from Python
        validity_start_str = op['validity_period_start'] || op[:validity_period_start] || op['created_at'] || op[:created_at]
        validity_end_str = op['validity_period_end'] || op[:validity_period_end]

        # Parse timestamps
        target_time = validity_start_str && !validity_start_str.to_s.empty? ? Time.parse(validity_start_str.to_s).utc : (rec.created_at || Time.now).utc

        # Build validity_period from pre-computed values
        if validity_end_str && !validity_end_str.to_s.empty?
          period_end = Time.parse(validity_end_str.to_s).utc
          validity_period = (target_time...period_end)
        else
          # Open-ended (last entry)
          validity_period = (target_time..)
        end

        # Apply field_changes to build progressive state snapshot
        if op.is_a?(Hash) && (op.key?("state_snapshot") || op.key?(:state_snapshot))
          state_snapshot = op["state_snapshot"] || op[:state_snapshot]
          sanitized_state = ensure_required_fields.call(state_snapshot, rec)
        else
          current_state = apply_field_changes_to_state.call(current_state, field_changes, priority_cache, rec)
          sanitized_state = current_state.dup
        end

        # Get cf_state_snapshot (pre-computed by Python with field names, resolve to IDs here)
        cf_snapshot = op["cf_state_snapshot"] || op[:cf_state_snapshot]
        resolved_cf_snapshot = nil
        if cf_snapshot.is_a?(Hash)
          resolved_cf_snapshot = {}
          if cf_snapshot['workflow'] && workflow_cf_id
            resolved_cf_snapshot[workflow_cf_id] = cf_snapshot['workflow']
          end
          if cf_snapshot['resolution'] && resolution_cf_id
            resolved_cf_snapshot[resolution_cf_id] = cf_snapshot['resolution']
          end
        end

        # Use pre-computed version from Python, or calculate if not provided
        pre_computed_version = op['version'] || op[:version]

        if op_idx == 0
          # First operation updates v1 journal
          v1_cf_snapshot = resolved_cf_snapshot
          v1_journal = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage', version: 1).first
          if v1_journal
            v1_journal.user_id = user_id
            v1_journal.notes = notes
            v1_journal.data = Journal::WorkPackageJournal.new(sanitized_state)
            v1_journal.save(validate: false)

            # Update timestamps via raw SQL
            target_time_str = target_time.strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
            if validity_period.end
              period_end_time = validity_period.end.is_a?(Time) ? validity_period.end : Time.parse(validity_period.end.to_s)
              period_end_str = period_end_time.strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
              range_sql = "tstzrange('#{target_time_str}', '#{period_end_str}', '[)')"
            else
              range_sql = "tstzrange('#{target_time_str}', NULL, '[)')"
            end
            conn.execute("UPDATE journals SET created_at = '#{target_time_str}', updated_at = '#{target_time_str}', validity_period = #{range_sql} WHERE id = #{v1_journal.id}")
          end
        else
          # v2+ journals: use pre-computed version or increment
          version = pre_computed_version || (base_version + bulk_journals.size + 1)
          bulk_journals << {
            version: version, user_id: user_id, notes: notes,
            created_at: target_time, validity_period: validity_period,
            state: sanitized_state, cf_snapshot: resolved_cf_snapshot
          }
        end
      end

      # Deduplicate by validity_period (in case Python sent duplicates)
      if bulk_journals.any?
        seen = {}
        deduped = []
        bulk_journals.each do |j|
          vp = j[:validity_period]
          if vp
            vp_key = vp.end ? "#{vp.begin.to_i}_#{vp.end.to_i}" : "#{vp.begin.to_i}_infinity"
            next if seen[vp_key]
            seen[vp_key] = true
          end
          deduped << j
        end

        # Re-number versions if deduplication removed entries
        if deduped.size < bulk_journals.size
          deduped.each_with_index { |j, i| j[:version] = base_version + 1 + i }
        end
        bulk_journals = deduped
      end

      # Bulk INSERT work_package_journals first (to get data_id)
      if bulk_journals.any?
        wp_journal_values = bulk_journals.map do |j|
          s = j[:state]
          subject_escaped = conn.quote(s[:subject].to_s)
          desc_escaped = conn.quote(s[:description].to_s)
          due_date_sql = s[:due_date] ? "'#{s[:due_date]}'" : "NULL"
          start_date_sql = s[:start_date] ? "'#{s[:start_date]}'" : "NULL"

          "(#{s[:type_id] || 'NULL'}, #{s[:project_id] || 'NULL'}, #{subject_escaped}, #{desc_escaped}, " +
          "#{due_date_sql}, #{s[:category_id] || 'NULL'}, #{s[:status_id] || 'NULL'}, #{s[:assigned_to_id] || 'NULL'}, " +
          "#{sanitize_id_field.call(s[:priority_id], priority_cache, rec.priority_id) || 'NULL'}, #{s[:version_id] || 'NULL'}, #{s[:author_id] || 'NULL'}, " +
          "#{s[:done_ratio] || 0}, #{s[:estimated_hours] || 'NULL'}, #{start_date_sql}, #{s[:parent_id] || 'NULL'}, " +
          "#{s[:schedule_manually] || false}, #{s[:ignore_non_working_days] || false})"
        end

        wp_insert_sql = <<~SQL
          INSERT INTO work_package_journals (type_id, project_id, subject, description,
            due_date, category_id, status_id, assigned_to_id, priority_id, version_id, author_id,
            done_ratio, estimated_hours, start_date, parent_id, schedule_manually, ignore_non_working_days)
          VALUES #{wp_journal_values.join(",\n       ")}
          RETURNING id
        SQL

        wp_result = conn.execute(wp_insert_sql)
        wp_journal_ids = []
        wp_result.each { |row| wp_journal_ids << row['id'] }

        # Bulk INSERT journals with data_type and data_id
        journal_values = bulk_journals.each_with_index.map do |j, idx|
          wp_journal_id = wp_journal_ids[idx]
          next nil unless wp_journal_id

          ts_str = j[:created_at].strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
          notes_escaped = conn.quote(j[:notes].to_s)

          if j[:validity_period].end
            period_end = j[:validity_period].end.is_a?(Time) ? j[:validity_period].end : Time.parse(j[:validity_period].end.to_s)
            period_end_str = period_end.strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
            range_sql = "tstzrange('#{ts_str}', '#{period_end_str}', '[)')"
          else
            range_sql = "tstzrange('#{ts_str}', NULL, '[)')"
          end

          "(#{rec.id}, 'WorkPackage', #{j[:user_id]}, #{notes_escaped}, #{j[:version]}, '#{ts_str}', '#{ts_str}', " +
          "'Journal::WorkPackageJournal', #{wp_journal_id}, #{range_sql})"
        end.compact

        if journal_values.any?
          insert_sql = <<~SQL
            INSERT INTO journals (journable_id, journable_type, user_id, notes, version, created_at, updated_at,
              data_type, data_id, validity_period)
            VALUES #{journal_values.join(",\n       ")}
            RETURNING id, version
          SQL

          journal_result = conn.execute(insert_sql)
          version_to_id = {}
          journal_result.each { |row| version_to_id[row['version']] = row['id'] }

          # Bulk INSERT customizable_journals for v2+ (J2O custom fields)
          # NOOP FIX: Only insert entries when CF value actually CHANGED
          if j2o_cf_ids.any?
            cf_journal_values = []
            # Start with v1's CF state as the baseline for comparison
            prev_cf_snapshot = v1_cf_snapshot.is_a?(Hash) ? v1_cf_snapshot.dup : {}

            bulk_journals.each do |j|
              journal_id = version_to_id[j[:version]]
              next unless journal_id

              curr_cf_snapshot = j[:cf_snapshot].is_a?(Hash) ? j[:cf_snapshot] : {}

              # Only insert entries for CF values that actually CHANGED from previous version
              curr_cf_snapshot.each do |cf_id, cf_value|
                next if cf_id.nil? || cf_value.nil?
                prev_value = prev_cf_snapshot[cf_id]

                # Check if value actually changed (handle nil vs empty string)
                value_changed = prev_value.to_s != cf_value.to_s

                if value_changed
                  cf_journal_values << "(#{journal_id}, #{cf_id.to_i}, #{conn.quote(cf_value.to_s)})"
                end
              end

              # Update prev_cf_snapshot for next iteration
              prev_cf_snapshot = curr_cf_snapshot.dup
            end
            if cf_journal_values.any?
              conn.execute("INSERT INTO customizable_journals (journal_id, custom_field_id, value) VALUES #{cf_journal_values.join(', ')}")
            end
          end
        end
      end

      # Insert customizable_journals for v1
      if v1_journal && j2o_cf_ids.any?
        Journal::CustomizableJournal.where(journal_id: v1_journal.id, custom_field_id: j2o_cf_ids).delete_all
        if v1_cf_snapshot.is_a?(Hash) && v1_cf_snapshot.any?
          cf_values = v1_cf_snapshot.map do |cf_id, cf_value|
            next nil if cf_id.nil? || cf_value.nil?
            "(#{v1_journal.id}, #{cf_id.to_i}, #{conn.quote(cf_value.to_s)})"
          end.compact
          conn.execute("INSERT INTO customizable_journals (journal_id, custom_field_id, value) VALUES #{cf_values.join(', ')}") if cf_values.any?
        end
      end

      result['created'] = bulk_journals.length

    rescue => e
      result['error'] = "#{e.class}: #{e.message}"
    end

    results << result
  end
end

# Output JSON result with dynamic markers (set by Python via $j2o_start_marker / $j2o_end_marker)
start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : "JSON_OUTPUT_START"
end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : "JSON_OUTPUT_END"
puts start_marker + results.to_json + end_marker
