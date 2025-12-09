# Multi-WP batch journal creation for optimized migration
# This script processes multiple work packages' journals in ONE Rails call
#
# Expected variables:
# - input_data: Array of {wp_id:, jira_key:, rails_ops:} hashes (loaded by execute_script_with_data)
# - verbose: boolean for logging control (optional)
#
# Output: JSON with results per WP
# {"results": [{"wp_id": X, "jira_key": "Y", "created": N, "error": null}, ...]}

require 'json'

SYNTHETIC_TIMESTAMP_INCREMENT = 1  # 1 second increment

results = []

if input_data && input_data.respond_to?(:each)
  conn = ActiveRecord::Base.connection

  # Cache lookups that are shared across all WPs
  workflow_cf = CustomField.find_by(name: "J2O Jira Workflow")
  resolution_cf = CustomField.find_by(name: "J2O Jira Resolution")
  j2o_cf_ids = [workflow_cf&.id, resolution_cf&.id].compact

  priority_cache = {}
  IssuePriority.all.each { |p| priority_cache[p.name.downcase] = p.id }

  valid_journal_attributes = [
    :type_id, :project_id, :subject, :description, :due_date, :category_id,
    :status_id, :assigned_to_id, :priority_id, :version_id, :author_id,
    :done_ratio, :estimated_hours, :start_date, :parent_id,
    :schedule_manually, :ignore_non_working_days
  ].freeze

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

      # Sort operations chronologically
      ops = rails_ops.sort_by do |op|
        created_at_str = op['created_at'] || op[:created_at] || op['timestamp'] || op[:timestamp]
        created_at_str ? Time.parse(created_at_str).utc : Time.now.utc
      end

      current_version = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').maximum(:version) || 0
      last_used_timestamp = nil

      # Lambda helpers
      apply_field_changes_to_state = lambda do |current_state, field_changes|
        return current_state unless field_changes && field_changes.is_a?(Hash)
        field_changes.each do |k, v|
          field_sym = k.to_sym
          next unless valid_journal_attributes.include?(field_sym)
          new_value = v.is_a?(Array) ? v[1] : v
          next if new_value.nil? || (new_value.is_a?(String) && new_value.empty?) || new_value.is_a?(Array)
          next unless new_value.is_a?(Integer) || new_value.is_a?(String) ||
                      new_value.is_a?(TrueClass) || new_value.is_a?(FalseClass) ||
                      new_value.is_a?(Float) || new_value.is_a?(Date) ||
                      new_value.is_a?(Time) || new_value.is_a?(Numeric)
          current_state[field_sym] = new_value
        end
        current_state
      end

      compute_timestamp_and_validity = lambda do |op_idx, created_at_str|
        if !created_at_str || created_at_str.empty?
          op = ops[op_idx]
          created_at_str = op['timestamp'] || op[:timestamp]
        end

        if created_at_str && !created_at_str.empty?
          target_time = Time.parse(created_at_str).utc
          if last_used_timestamp && target_time <= last_used_timestamp
            target_time = last_used_timestamp + SYNTHETIC_TIMESTAMP_INCREMENT
          end
        elsif last_used_timestamp
          target_time = last_used_timestamp + SYNTHETIC_TIMESTAMP_INCREMENT
        else
          target_time = (rec.created_at || Time.now).utc
        end

        next_op = ops[op_idx + 1]
        if next_op
          next_created_at = next_op['created_at'] || next_op[:created_at] || next_op['timestamp'] || next_op[:timestamp]
          if next_created_at && !next_created_at.empty?
            period_end = Time.parse(next_created_at).utc
            period_end = target_time + SYNTHETIC_TIMESTAMP_INCREMENT if period_end <= target_time
            validity_period = (target_time...period_end)
          else
            period_end = target_time + SYNTHETIC_TIMESTAMP_INCREMENT
            validity_period = (target_time...period_end)
          end
        else
          validity_period = (target_time..)
        end

        last_used_timestamp = validity_period.end || target_time
        [target_time, validity_period]
      end

      ensure_required_fields = lambda do |state|
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

      sanitize_id_field = lambda do |value, cache, fallback|
        return fallback if value.nil?
        return value if value.is_a?(Integer)
        return value.to_i if value.to_s =~ /^\d+$/
        cache[value.to_s.downcase] || fallback
      end

      # Initialize state
      current_state = {
        type_id: rec.type_id, project_id: rec.project_id, subject: rec.subject,
        description: rec.description, due_date: rec.due_date, category_id: rec.category_id,
        status_id: rec.status_id, assigned_to_id: rec.assigned_to_id, priority_id: rec.priority_id,
        version_id: rec.version_id, author_id: rec.author_id, done_ratio: rec.done_ratio,
        estimated_hours: rec.estimated_hours, start_date: rec.start_date, parent_id: rec.parent_id,
        schedule_manually: rec.schedule_manually, ignore_non_working_days: rec.ignore_non_working_days
      }

      # Collect journal data
      bulk_journals = []
      v1_journal = nil
      v1_cf_snapshot = nil

      ops.each_with_index do |op, op_idx|
        op_type = op['type'] || op[:type]
        next if op_type == 'set_journal_user'

        timestamp_only_ops = ['set_created_at', 'set_updated_at', 'set_closed_at', 'set_journal_created_at']
        next if timestamp_only_ops.include?(op_type) && op_idx != 0

        notes = op['notes'] || op[:notes] || ''
        field_changes = op['field_changes'] || op[:field_changes]

        is_empty = (notes.nil? || notes.to_s.strip.empty?) && (field_changes.nil? || field_changes.empty?)
        next if is_empty && op_idx != 0

        raw_user_id = (op['user_id'] || op[:user_id]).to_i
        fallback_user_id = rec.author_id && rec.author_id > 0 ? rec.author_id : 2
        user_id = raw_user_id > 0 ? raw_user_id : fallback_user_id

        created_at_str = op['created_at'] || op[:created_at] || op['timestamp'] || op[:timestamp]
        target_time, validity_period = compute_timestamp_and_validity.call(op_idx, created_at_str)

        if op.is_a?(Hash) && (op.has_key?("state_snapshot") || op.has_key?(:state_snapshot))
          state_snapshot = op["state_snapshot"] || op[:state_snapshot]
          sanitized_state = ensure_required_fields.call(state_snapshot)
        else
          current_state = apply_field_changes_to_state.call(current_state, field_changes)
          sanitized_state = current_state.dup
        end

        cf_snapshot = op["cf_state_snapshot"] || op[:cf_state_snapshot]

        if op_idx == 0
          v1_cf_snapshot = cf_snapshot
          v1_journal = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage', version: 1).first
          if v1_journal
            v1_journal.user_id = user_id
            v1_journal.notes = notes
            v1_journal.data = Journal::WorkPackageJournal.new(sanitized_state)
            v1_journal.save(validate: false)

            target_time_str = target_time.to_time.strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
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
          current_version += 1
          bulk_journals << {
            version: current_version, user_id: user_id, notes: notes,
            created_at: target_time, validity_period: validity_period,
            state: sanitized_state, cf_snapshot: cf_snapshot
          }
        end
      end

      # Deduplicate
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

        if deduped.size < bulk_journals.size
          base_version = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').maximum(:version) || 0
          deduped.each_with_index { |j, i| j[:version] = base_version + 1 + i }
        end
        bulk_journals = deduped
      end

      # Bulk INSERT
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

        journal_values = bulk_journals.each_with_index.map do |j, idx|
          wp_journal_id = wp_journal_ids[idx]
          next nil unless wp_journal_id

          ts_str = j[:created_at].to_time.strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
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

          # CustomizableJournals for v2+
          if j2o_cf_ids.any?
            cf_journal_values = []
            bulk_journals.each do |j|
              journal_id = version_to_id[j[:version]]
              next unless journal_id && j[:cf_snapshot].is_a?(Hash)
              j[:cf_snapshot].each do |cf_id, cf_value|
                next if cf_id.nil? || cf_value.nil?
                cf_journal_values << "(#{journal_id}, #{cf_id.to_i}, #{conn.quote(cf_value.to_s)})"
              end
            end
            if cf_journal_values.any?
              conn.execute("INSERT INTO customizable_journals (journal_id, custom_field_id, value) VALUES #{cf_journal_values.join(', ')}")
            end
          end
        end
      end

      # CustomizableJournals for v1
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

# Output JSON result with markers expected by execute_script_with_data
puts "JSON_OUTPUT_START" + results.to_json + "JSON_OUTPUT_END"
