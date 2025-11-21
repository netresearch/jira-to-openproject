# Journal creation logic for work package migration
# This file is loaded by openproject_client.py bulk_create_records function
#
# Expected variables to be set before loading this file:
# - rec: the WorkPackage record
# - rails_ops: array of journal operations from Jira
# - idx: bulk item index for logging
# - verbose: boolean for logging control
# - errors: array to collect error details (optional, for propagation to Python)

# BUG #7 FIX (LOW): Extract magic numbers to constants
SYNTHETIC_TIMESTAMP_INCREMENT_US = Rational(1, 1_000_000)  # 1 microsecond increment

if rails_ops && rails_ops.respond_to?(:each)
  puts "[RUBY] INSIDE JOURNAL BLOCK - Processing rails_ops..." if verbose
  STDOUT.flush

  begin
    # Sort operations by created_at timestamp to ensure chronological order
    # This is critical for validity_period ranges to not overlap
    ops = rails_ops.sort_by do |op|
      created_at_str = op['created_at'] || op[:created_at]
      # BUG #6 FIX (MEDIUM): Use UTC parsing for timezone consistency
      created_at_str ? Time.parse(created_at_str).utc : Time.now.utc
    end

    puts "J2O bulk item #{idx}: Processing #{ops.length} journal operations (sorted by created_at)" if verbose

    # BUG #4 FIX (HIGH): Query max_version ONCE outside loop to avoid N+1 queries
    current_version = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').maximum(:version) || 0

    # BUG #1 FIX (CRITICAL): Track last used timestamp to generate synthetic timestamps
    # for operations with missing created_at (prevents EXCLUSION constraint violations)
    last_used_timestamp = nil

    # BUG #9 FIX (CRITICAL): Build progressive state history instead of copying current state
    # This lambda applies field_changes to a current_state hash to build historical progression
    apply_field_changes_to_state = lambda do |current_state, field_changes|
      # BUG #32 FIX (REGRESSION #3): Whitelist valid WorkPackageJournal attributes
      valid_journal_attributes = [
        :type_id, :project_id, :subject, :description, :due_date, :category_id,
        :status_id, :assigned_to_id, :priority_id, :version_id, :author_id,
        :done_ratio, :estimated_hours, :start_date, :parent_id,
        :schedule_manually, :ignore_non_working_days
      ].freeze

      if field_changes && field_changes.is_a?(Hash)
        field_changes.each do |k, v|
          field_sym = k.to_sym
          next unless valid_journal_attributes.include?(field_sym)

          # BUG #32 FIX (REGRESSION #3): Extract NEW value from [old, new] array
          new_value = v.is_a?(Array) ? v[1] : v

          puts "J2O bulk item #{idx}: DEBUG - field #{k}: raw_value=#{v.inspect}, new_value=#{new_value.inspect}" if verbose

          # Skip if value is nil
          next if new_value.nil?

          # BUG #9 FIX (CRITICAL): Skip empty strings to prevent INTEGER column coercion to 0
          # Empty strings in field_changes would overwrite valid state and convert to 0 in DB
          if new_value.is_a?(String) && new_value.empty?
            puts "J2O bulk item #{idx}: DEBUG - Skipping #{k} with empty string (prevents 0 coercion)" if verbose
            next
          end

          # BUG #32 FIX (REGRESSION #5): Ensure scalar values only
          if new_value.is_a?(Array)
            puts "J2O bulk item #{idx}: WARNING - Skipping #{k} with array value" if verbose
            next
          end
          unless new_value.is_a?(Integer) || new_value.is_a?(String) || new_value.is_a?(TrueClass) || new_value.is_a?(FalseClass) || new_value.is_a?(Float) || new_value.is_a?(Date) || new_value.is_a?(Time) || new_value.is_a?(Numeric)
            puts "J2O bulk item #{idx}: WARNING - Skipping #{k} with non-scalar class #{new_value.class}" if verbose
            next
          end

          # BUG #9 FIX: Update the progressive state
          puts "J2O bulk item #{idx}: DEBUG - Updating state #{field_sym}: #{current_state[field_sym].inspect} -> #{new_value.inspect}" if verbose
          current_state[field_sym] = new_value
        end
      end

      current_state
    end

    # BUG #1 FIX (CRITICAL): Unified timestamp and validity_period logic
    # Applies to BOTH v1 and v2+ journals to maintain timestamp chain
    apply_timestamp_and_validity = lambda do |journal, op_idx, created_at_str|
      # 1. Determine target start time
      target_time = nil
      if created_at_str && !created_at_str.empty?
        # BUG #6 FIX: Use UTC parsing for timezone consistency
        target_time = Time.parse(created_at_str).utc
        puts "J2O bulk item #{idx}: Op #{op_idx+1} using timestamp: #{target_time}" if verbose
      elsif last_used_timestamp
        # BUG #1 FIX: Synthetic timestamp with microsecond increment
        target_time = last_used_timestamp + SYNTHETIC_TIMESTAMP_INCREMENT_US
        puts "J2O bulk item #{idx}: Op #{op_idx+1} using synthetic timestamp: #{target_time}" if verbose
      else
        # BUG #7 FIX (LOW): Prefer historical time over Time.now
        target_time = (rec.created_at || Time.now).utc
        puts "J2O bulk item #{idx}: Op #{op_idx+1} using fallback timestamp: #{target_time}" if verbose
      end

      # 2. Update tracker for next operation
      last_used_timestamp = target_time

      # 3. Determine validity_period range
      next_op = ops[op_idx + 1]
      if next_op
        next_created_at = next_op['created_at'] || next_op[:created_at]
        if next_created_at && !next_created_at.empty?
          # Next op has timestamp - use it as end of range
          period_end = Time.parse(next_created_at).utc
          # BUG #32 FIX: Prevent empty range if timestamps identical (collision detection)
          if period_end <= target_time
            period_end = target_time + SYNTHETIC_TIMESTAMP_INCREMENT_US
          end
          journal.validity_period = (target_time...period_end)  # Bounded exclusive range
        else
          # Next op will use synthetic timestamp
          period_end = target_time + SYNTHETIC_TIMESTAMP_INCREMENT_US
          journal.validity_period = (target_time...period_end)
        end
      else
        # Last operation - endless range
        journal.validity_period = (target_time..)
      end

      # 4. Persist timestamps AND validity_period to journal
      # BUG #6 FIX (MEDIUM - SEC#2): update_columns needed for historical timestamps
      # Bypasses callbacks by design - required for migration to set past timestamps
      # BUG #32 FIX (REGRESSION): Must also persist validity_period, not just timestamps!
      puts "J2O bulk item #{idx}: DEBUG - Op #{op_idx+1} before update: persisted=#{journal.persisted?}, validity_period=#{journal.validity_period.inspect}" if verbose
      if journal.persisted?
        result = journal.update_columns(
          created_at: target_time,
          updated_at: target_time,
          validity_period: journal.validity_period
        )
        puts "J2O bulk item #{idx}: DEBUG - Op #{op_idx+1} update_columns result: #{result.inspect}" if verbose
      else
        puts "J2O bulk item #{idx}: WARNING - Op #{op_idx+1} journal not persisted, cannot update validity_period!" if verbose
      end

      target_time
    end

    # BUG #9 FIX (NOT NULL CONSTRAINTS): Ensure required fields have valid defaults
    # OpenProject database has NOT NULL constraints on priority_id, type_id, status_id,
    # schedule_manually, and ignore_non_working_days
    # Historical data may have nil values which need defaults
    ensure_required_fields = lambda do |state|
      # Convert string keys to symbol keys if needed
      if state.is_a?(Hash) && state.keys.first.is_a?(String)
        state = state.transform_keys(&:to_sym)
      end

      # Use work package's current values as defaults (guaranteed to be valid)
      state[:priority_id] ||= rec.priority_id
      state[:type_id] ||= rec.type_id
      state[:status_id] ||= rec.status_id
      state[:project_id] ||= rec.project_id
      state[:author_id] ||= rec.author_id

      # BUG #9 FIX: Boolean fields also have NOT NULL constraints
      # Use nil-safe assignment with explicit false default for booleans
      state[:schedule_manually] = rec.schedule_manually if state[:schedule_manually].nil?
      state[:ignore_non_working_days] = rec.ignore_non_working_days if state[:ignore_non_working_days].nil?

      state
    end

    # BUG #9 FIX (CRITICAL): Initialize progressive state from work package
    # This state will be updated with field_changes from each operation
    current_state = {
      type_id: rec.type_id,
      project_id: rec.project_id,
      subject: rec.subject,
      description: rec.description,
      due_date: rec.due_date,
      category_id: rec.category_id,
      status_id: rec.status_id,
      assigned_to_id: rec.assigned_to_id,
      priority_id: rec.priority_id,
      version_id: rec.version_id,
      author_id: rec.author_id,
      done_ratio: rec.done_ratio,
      estimated_hours: rec.estimated_hours,
      start_date: rec.start_date,
      parent_id: rec.parent_id,
      schedule_manually: rec.schedule_manually,
      ignore_non_working_days: rec.ignore_non_working_days
    }
    puts "J2O bulk item #{idx}: DEBUG - Initial state: status_id=#{current_state[:status_id]}" if verbose

    ops.each_with_index do |op, op_idx|
      begin
        op_type = op['type'] || op[:type]
        user_id = (op['user_id'] || op[:user_id]).to_i
        created_at_str = op['created_at'] || op[:created_at]
        notes = op['notes'] || op[:notes] || ''
        field_changes = op['field_changes'] || op[:field_changes]

        journal = nil

        if op_idx == 0
          # FIRST OPERATION: Update existing auto-created journal v1
          journal = Journal.where(
            journable_id: rec.id,
            journable_type: 'WorkPackage',
            version: 1
          ).first

          if journal
            puts "J2O bulk item #{idx}: Updating existing journal v1" if verbose

            # Update journal metadata
            journal.user_id = user_id
            journal.notes = notes

            # BUG #9 FIX (COMPLETE): Use state_snapshot from Python if available
            # Python now provides complete state snapshots by processing operations in REVERSE
            if op.is_a?(Hash) && (op.has_key?("state_snapshot") || op.has_key?(:state_snapshot))
              state_snapshot = op["state_snapshot"] || op[:state_snapshot]
              puts "J2O bulk item #{idx}: Using state_snapshot for v1 (#{state_snapshot.keys.count} fields)" if verbose
              # BUG #9 FIX: Ensure required fields have defaults to prevent NOT NULL violations
              sanitized_state = ensure_required_fields.call(state_snapshot)
              journal.data = Journal::WorkPackageJournal.new(sanitized_state)
            else
              # Fallback: Use progressive state building (old behavior)
              puts "J2O bulk item #{idx}: WARNING - No state_snapshot, using fallback progressive state for v1" if verbose
              current_state = apply_field_changes_to_state.call(current_state, field_changes)
              journal.data = Journal::WorkPackageJournal.new(current_state)
            end

            # BUG #6 FIX (MEDIUM - SEC#1): save(validate: false) required for historical migration
            # Skips validations to allow past timestamps and bypass business rules
            # This is safe in migration context - historical data already validated in Jira
            journal.save(validate: false)

            # BUG #1 FIX: Apply unified timestamp logic to v1 block
            apply_timestamp_and_validity.call(journal, op_idx, created_at_str)

            puts "J2O bulk item #{idx}: Updated journal v#{journal.version} (op #{op_idx+1}/#{ops.length})" if verbose
          else
            # BUG #7 FIX (LOW): Create journal v1 if missing instead of just warning
            puts "J2O bulk item #{idx}: WARNING - No journal v1 found, creating new v1" if verbose
            journal = Journal.new(
              journable_id: rec.id,
              journable_type: 'WorkPackage',
              user_id: user_id,
              notes: notes,
              version: 1
            )

            # BUG #9 FIX: For v1 (creation), apply field_changes to initial state
            current_state = apply_field_changes_to_state.call(current_state, field_changes)

            # BUG #32 FIX (REGRESSION #7): Use direct assignment instead of build_data to avoid Rails association callbacks
            journal.data = Journal::WorkPackageJournal.new(current_state)
            journal.save(validate: false)
            apply_timestamp_and_validity.call(journal, op_idx, created_at_str)
            current_version = 1  # Sync counter
          end

        else
          # SUBSEQUENT OPERATIONS: Create new journals v2, v3, etc.
          # BUG #4 FIX: Use local counter instead of querying DB each time (N+1 fix)
          current_version += 1

          journal = Journal.new(
            journable_id: rec.id,
            journable_type: 'WorkPackage',
            user_id: user_id,
            notes: notes,
            version: current_version
          )

          # BUG #9 FIX (COMPLETE): Use state_snapshot from Python if available
          # Python now provides complete state snapshots by processing operations in REVERSE
          if op.is_a?(Hash) && (op.has_key?("state_snapshot") || op.has_key?(:state_snapshot))
            state_snapshot = op["state_snapshot"] || op[:state_snapshot]
            puts "J2O bulk item #{idx}: Using state_snapshot for v#{current_version} (#{state_snapshot.keys.count} fields)" if verbose
            # BUG #9 FIX: Ensure required fields have defaults to prevent NOT NULL violations
            sanitized_state = ensure_required_fields.call(state_snapshot)
            journal.data = Journal::WorkPackageJournal.new(sanitized_state)
          else
            # Fallback: Use progressive state building (old behavior)
            puts "J2O bulk item #{idx}: WARNING - No state_snapshot, using fallback progressive state for v#{current_version}" if verbose
            current_state = apply_field_changes_to_state.call(current_state, field_changes)
            puts "J2O bulk item #{idx}: DEBUG - After applying changes for v#{current_version}: status_id=#{current_state[:status_id]}" if verbose
            journal.data = Journal::WorkPackageJournal.new(current_state)
          end

          # BUG #32 FIX (REGRESSION #4 - CRITICAL): Set validity_period BEFORE save to prevent NULL constraint violation
          # For NEW journals, apply_timestamp_and_validity sets validity_period in memory but doesn't call update_columns (journal not persisted yet)
          target_time = apply_timestamp_and_validity.call(journal, op_idx, created_at_str)

          # BUG #6 FIX (MEDIUM - SEC#1): save(validate: false) required for historical migration
          # Now journal saves WITH validity_period set, preventing CHECK constraint violation
          save_result = journal.save(validate: false)

          # BUG #9 FIX (MISSING JOURNALS): Check if save actually succeeded
          if !save_result || !journal.persisted?
            error_msg = "Failed to save journal v#{current_version}"
            error_details = journal.errors.full_messages.join(", ")

            puts "J2O bulk item #{idx}: ERROR - #{error_msg}" if verbose
            puts "J2O bulk item #{idx}: ERROR - Validation errors: #{error_details}" if verbose && !error_details.empty?
            puts "J2O bulk item #{idx}: ERROR - Journal state: id=#{journal.id.inspect}, persisted=#{journal.persisted?}, version=#{journal.version}" if verbose

            # Propagate error to Python layer
            if defined?(errors) && errors.respond_to?(:<<)
              errors << {
                'bulk_item' => idx,
                'operation' => op_idx + 1,
                'error_class' => 'ActiveRecord::RecordNotSaved',
                'message' => error_msg,
                'details' => error_details,
                'journal_version' => current_version,
                'save_result' => save_result
              }
            end

            next  # Skip to next operation
          end

          # BUG #32 FIX (REGRESSION #4): After save, update historical timestamps while preserving validity_period
          journal.update_columns(
            created_at: target_time,
            updated_at: target_time
          )

          puts "J2O bulk item #{idx}: Created journal v#{journal.version} (op #{op_idx+1}/#{ops.length})" if verbose
        end

      rescue => e
        # BUG #3 FIX (CRITICAL): Comprehensive error logging with stack traces
        error_detail = {
          'bulk_item' => idx,
          'operation' => op_idx + 1,
          'error_class' => e.class.to_s,
          'message' => e.message,
          'backtrace' => e.backtrace ? e.backtrace.first(10) : []
        }
        puts "J2O bulk item #{idx}: Journal op #{op_idx+1} FAILED: #{e.class}: #{e.message}" if verbose
        puts "  Backtrace: #{error_detail['backtrace'].first(3).join(' <- ')}" if verbose && error_detail['backtrace'].any?

        # BUG #3 FIX: Propagate error to Python layer if errors array exists
        errors << error_detail if defined?(errors) && errors.respond_to?(:<<)
      end
    end

  rescue => e
    # BUG #3 FIX (CRITICAL): Top-level error logging with full details
    error_detail = {
      'bulk_item' => idx,
      'error_class' => e.class.to_s,
      'message' => "Journal processing failed: #{e.message}",
      'backtrace' => e.backtrace ? e.backtrace.first(15) : []
    }
    puts "J2O bulk item #{idx}: FATAL - Journal processing error: #{e.class}: #{e.message}" if verbose
    puts "  Backtrace: #{error_detail['backtrace'].first(5).join("\n  ")}" if verbose && error_detail['backtrace'].any?

    # BUG #3 FIX: Propagate to Python layer
    errors << error_detail if defined?(errors) && errors.respond_to?(:<<)
  end
end
