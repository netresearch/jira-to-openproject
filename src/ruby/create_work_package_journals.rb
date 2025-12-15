# Journal creation logic for work package migration
# This file is loaded by openproject_client.py bulk_create_records function
#
# Expected variables to be set before loading this file:
# - rec: the WorkPackage record
# - rails_ops: array of journal operations from Jira
# - idx: bulk item index for logging
# - verbose: boolean for logging control (recommended: false for performance)
# - errors: array to collect error details (optional, for propagation to Python)
#
# PERFORMANCE OPTIMIZATIONS:
# - Bulk SQL INSERT for v2+ journals instead of individual saves
# - Bulk INSERT for work_package_journals and customizable_journals
# - Minimal logging (errors and summary only)

# Use 1 second increment for safer timestamp spacing (avoids precision issues with microseconds)
SYNTHETIC_TIMESTAMP_INCREMENT = 1  # 1 second increment

if rails_ops && rails_ops.respond_to?(:each)
  catch(:skip_work_package) do
  begin
    conn = ActiveRecord::Base.connection

    # ALWAYS delete v2+ journals to ensure idempotent re-migration
    # This prevents conflicts when the same WP is processed multiple times
    v2_plus_journals = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').where('version > 1')
    v2_plus_count = v2_plus_journals.count

    if v2_plus_count > 0
      # Delete associated customizable_journals and work_package_journals first
      v2_plus_ids = v2_plus_journals.pluck(:id)
      if v2_plus_ids.any?
        Journal::CustomizableJournal.where(journal_id: v2_plus_ids).delete_all
        # Get data_ids before deleting journals
        data_ids = v2_plus_journals.pluck(:data_id).compact
        v2_plus_journals.delete_all
        # Clean up orphaned work_package_journals
        if data_ids.any?
          Journal::WorkPackageJournal.where(id: data_ids).delete_all
        end
      end
      puts "J2O item #{idx}: CLEANUP - WP##{rec.id} deleted #{v2_plus_count} existing v2+ journals for re-migration"
    end

    # Sort operations by created_at/timestamp for chronological order
    ops = rails_ops.sort_by do |op|
      created_at_str = op['created_at'] || op[:created_at] || op['timestamp'] || op[:timestamp]
      created_at_str ? Time.parse(created_at_str).utc : Time.now.utc
    end

    # Query max_version ONCE outside loop (N+1 fix)
    current_version = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').maximum(:version) || 0
    last_used_timestamp = nil

    # Valid WorkPackageJournal attributes whitelist
    valid_journal_attributes = [
      :type_id, :project_id, :subject, :description, :due_date, :category_id,
      :status_id, :assigned_to_id, :priority_id, :version_id, :author_id,
      :done_ratio, :estimated_hours, :start_date, :parent_id,
      :schedule_manually, :ignore_non_working_days
    ].freeze

    # Lambda: Apply field_changes to state hash
    apply_field_changes_to_state = lambda do |current_state, field_changes|
      return current_state unless field_changes && field_changes.is_a?(Hash)

      field_changes.each do |k, v|
        field_sym = k.to_sym
        next unless valid_journal_attributes.include?(field_sym)

        new_value = v.is_a?(Array) ? v[1] : v
        next if new_value.nil?
        next if new_value.is_a?(String) && new_value.empty?
        next if new_value.is_a?(Array)
        next unless new_value.is_a?(Integer) || new_value.is_a?(String) ||
                    new_value.is_a?(TrueClass) || new_value.is_a?(FalseClass) ||
                    new_value.is_a?(Float) || new_value.is_a?(Date) ||
                    new_value.is_a?(Time) || new_value.is_a?(Numeric)

        current_state[field_sym] = new_value
      end
      current_state
    end

    # Lambda: Compute timestamp and validity_period
    compute_timestamp_and_validity = lambda do |op_idx, created_at_str|
      target_time = nil

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

      # Determine validity_period range
      next_op = ops[op_idx + 1]
      validity_period = nil
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

      # Update tracker
      last_used_timestamp = validity_period.end || target_time

      [target_time, validity_period]
    end

    # Lambda: Ensure required fields have valid defaults
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

    # Initialize progressive state from work package
    current_state = {
      type_id: rec.type_id, project_id: rec.project_id, subject: rec.subject,
      description: rec.description, due_date: rec.due_date, category_id: rec.category_id,
      status_id: rec.status_id, assigned_to_id: rec.assigned_to_id, priority_id: rec.priority_id,
      version_id: rec.version_id, author_id: rec.author_id, done_ratio: rec.done_ratio,
      estimated_hours: rec.estimated_hours, start_date: rec.start_date, parent_id: rec.parent_id,
      schedule_manually: rec.schedule_manually, ignore_non_working_days: rec.ignore_non_working_days
    }

    # Lookup J2O CF IDs for CF journal entries
    workflow_cf = CustomField.find_by(name: "J2O Jira Workflow")
    resolution_cf = CustomField.find_by(name: "J2O Jira Resolution")
    j2o_cf_ids = [workflow_cf&.id, resolution_cf&.id].compact

    # Build priority name->ID cache for resolving string priority values
    priority_cache = {}
    IssuePriority.all.each { |p| priority_cache[p.name.downcase] = p.id }

    # Lambda: Sanitize ID fields - convert string names to IDs
    sanitize_id_field = lambda do |value, cache, fallback|
      return fallback if value.nil?
      return value if value.is_a?(Integer)
      return value.to_i if value.to_s =~ /^\d+$/
      # Try to look up by name (case-insensitive)
      cache[value.to_s.downcase] || fallback
    end

    # ============================================================
    # PHASE 1: Collect all journal data (v2+) for bulk insert
    # ============================================================
    bulk_journals = []        # [{version:, user_id:, notes:, created_at:, validity_period:, state:, cf_snapshot:}]
    v1_journal = nil
    v1_op = nil
    v1_timestamp = nil
    v1_validity = nil
    v1_cf_snapshot = nil

    ops.each_with_index do |op, op_idx|
      op_type = op['type'] || op[:type]

      # Skip operations that don't create journals
      next if op_type == 'set_journal_user'

      timestamp_only_ops = ['set_created_at', 'set_updated_at', 'set_closed_at', 'set_journal_created_at']
      next if timestamp_only_ops.include?(op_type) && op_idx != 0

      notes = op['notes'] || op[:notes] || ''
      field_changes = op['field_changes'] || op[:field_changes]

      # Skip empty operations (except first)
      is_empty = (notes.nil? || notes.to_s.strip.empty?) && (field_changes.nil? || field_changes.empty?)
      next if is_empty && op_idx != 0

      # User ID with fallback
      raw_user_id = (op['user_id'] || op[:user_id]).to_i
      fallback_user_id = rec.author_id && rec.author_id > 0 ? rec.author_id : 2
      user_id = raw_user_id > 0 ? raw_user_id : fallback_user_id

      created_at_str = op['created_at'] || op[:created_at] || op['timestamp'] || op[:timestamp]
      target_time, validity_period = compute_timestamp_and_validity.call(op_idx, created_at_str)

      # Get state snapshot
      if op.is_a?(Hash) && (op.has_key?("state_snapshot") || op.has_key?(:state_snapshot))
        state_snapshot = op["state_snapshot"] || op[:state_snapshot]
        sanitized_state = ensure_required_fields.call(state_snapshot)
      else
        current_state = apply_field_changes_to_state.call(current_state, field_changes)
        sanitized_state = current_state.dup
      end

      # Get CF state snapshot
      cf_snapshot = op["cf_state_snapshot"] || op[:cf_state_snapshot]

      if op_idx == 0
        # V1: will update existing journal
        v1_op = op
        v1_timestamp = target_time
        v1_validity = validity_period
        v1_cf_snapshot = cf_snapshot

        v1_journal = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage', version: 1).first
        if v1_journal
          v1_journal.user_id = user_id
          v1_journal.notes = notes
          v1_journal.data = Journal::WorkPackageJournal.new(sanitized_state)
          v1_journal.save(validate: false)

          # Update timestamps via raw SQL
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
        # V2+: collect for bulk insert
        current_version += 1
        bulk_journals << {
          version: current_version,
          user_id: user_id,
          notes: notes,
          created_at: target_time,
          validity_period: validity_period,
          state: sanitized_state,
          cf_snapshot: cf_snapshot
        }
      end
    end

    # ============================================================
    # DEDUPLICATION: Remove duplicate validity_periods before bulk INSERT
    # This handles cases where Jira changelog has duplicate operations
    # ============================================================
    if bulk_journals.any?
      seen_validity_periods = {}
      original_count = bulk_journals.size
      deduped_journals = []

      bulk_journals.each do |j|
        # Create a unique key from the validity_period
        vp = j[:validity_period]
        if vp
          vp_key = if vp.end
            "#{vp.begin.to_i}_#{vp.end.to_i}"
          else
            "#{vp.begin.to_i}_infinity"
          end

          if seen_validity_periods[vp_key]
            # Skip duplicate - already have a journal with this validity_period
            next
          end
          seen_validity_periods[vp_key] = true
        end
        deduped_journals << j
      end

      # Re-number versions after deduplication
      if deduped_journals.size < original_count
        dup_count = original_count - deduped_journals.size
        puts "J2O item #{idx}: DEDUP - WP##{rec.id} removed #{dup_count} duplicate validity_periods"

        # Renumber versions starting from current_version (which was already incremented in the loop)
        base_version = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').maximum(:version) || 0
        deduped_journals.each_with_index do |j, i|
          j[:version] = base_version + 1 + i
        end
      end

      bulk_journals = deduped_journals
    end

    # ============================================================
    # PHASE 2: Bulk INSERT work_package_journals FIRST (to get data_id for journals)
    # ============================================================
    if bulk_journals.any?
      # Build work_package_journal values (order matches bulk_journals array)
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

      # Map bulk_journals index -> wp_journal_id (order preserved)
      wp_journal_ids = []
      wp_result.each { |row| wp_journal_ids << row['id'] }

      # ============================================================
      # PHASE 3: Bulk INSERT journals with data_type and data_id
      # ============================================================
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

        result = conn.execute(insert_sql)

        # Map version -> journal_id for customizable_journals
        version_to_id = {}
        result.each { |row| version_to_id[row['version']] = row['id'] }
      else
        version_to_id = {}
      end

      # ============================================================
      # PHASE 4: Bulk INSERT customizable_journals for v2+
      # NOOP FIX: Only insert entries when CF value actually CHANGED
      # ============================================================
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
              value_escaped = conn.quote(cf_value.to_s)
              cf_journal_values << "(#{journal_id}, #{cf_id.to_i}, #{value_escaped})"
            end
          end

          # Update prev_cf_snapshot for next iteration
          prev_cf_snapshot = curr_cf_snapshot.dup
        end

        if cf_journal_values.any?
          cf_insert_sql = <<~SQL
            INSERT INTO customizable_journals (journal_id, custom_field_id, value)
            VALUES #{cf_journal_values.join(",\n       ")}
          SQL
          conn.execute(cf_insert_sql)
        end
      end
    end

    # ============================================================
    # PHASE 5: Handle CustomizableJournals for v1
    # ============================================================
    if v1_journal && j2o_cf_ids.any?
      # Delete existing J2O CF entries for v1
      Journal::CustomizableJournal.where(journal_id: v1_journal.id, custom_field_id: j2o_cf_ids).delete_all

      # Insert new ones from cf_snapshot
      if v1_cf_snapshot.is_a?(Hash) && v1_cf_snapshot.any?
        cf_values = v1_cf_snapshot.map do |cf_id, cf_value|
          next nil if cf_id.nil? || cf_value.nil?
          value_escaped = conn.quote(cf_value.to_s)
          "(#{v1_journal.id}, #{cf_id.to_i}, #{value_escaped})"
        end.compact

        if cf_values.any?
          conn.execute("INSERT INTO customizable_journals (journal_id, custom_field_id, value) VALUES #{cf_values.join(', ')}")
        end
      end
    end

    puts "J2O item #{idx}: created #{bulk_journals.length} journals (bulk SQL)" if verbose

  rescue => e
    error_detail = {
      'bulk_item' => idx,
      'error_class' => e.class.to_s,
      'message' => "Journal processing failed: #{e.message}",
      'backtrace' => e.backtrace ? e.backtrace.first(10) : []
    }
    puts "J2O item #{idx}: FATAL - #{e.class}: #{e.message}"
    errors << error_detail if defined?(errors) && errors.respond_to?(:<<)
  end
  end  # close catch(:skip_work_package)
end
