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
    # Sort operations by created_at/timestamp to ensure chronological order
    # This is critical for validity_period ranges to not overlap
    ops = rails_ops.sort_by do |op|
      # BUG #9 FIX (MISSING JOURNALS): Check both 'created_at' and 'timestamp' fields
      # set_* operations use 'timestamp' instead of 'created_at'
      created_at_str = op['created_at'] || op[:created_at] || op['timestamp'] || op[:timestamp]
      # BUG #6 FIX (MEDIUM): Use UTC parsing for timezone consistency
      created_at_str ? Time.parse(created_at_str).utc : Time.now.utc
    end

    puts "J2O bulk item #{idx}: Processing #{ops.length} journal operations (sorted by created_at)" if verbose
    # BUG #9 DEBUG: Detailed operation tracing to identify where operations 23-27 are lost
    if verbose
      puts "J2O bulk item #{idx}: DEBUG - Original rails_ops count: #{rails_ops.length}"
      puts "J2O bulk item #{idx}: DEBUG - After sort ops count: #{ops.length}"
      if ops.length > 0
        first_op = ops.first
        last_op = ops.last
        puts "J2O bulk item #{idx}: DEBUG - First op: type=#{first_op['type'] || first_op[:type]}, timestamp=#{first_op['timestamp'] || first_op[:timestamp] || first_op['created_at'] || first_op[:created_at]}"
        puts "J2O bulk item #{idx}: DEBUG - Last op: type=#{last_op['type'] || last_op[:type]}, timestamp=#{last_op['timestamp'] || last_op[:timestamp] || last_op['created_at'] || last_op[:created_at]}"
      end
    end

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
      # BUG #9 FIX (CRITICAL REGRESSION): Check 'timestamp' field for set_* operations
      # Operations like set_created_at, set_journal_created_at use 'timestamp' not 'created_at'
      if !created_at_str || created_at_str.empty?
        op = ops[op_idx]
        created_at_str = op['timestamp'] || op[:timestamp]
      end

      if created_at_str && !created_at_str.empty?
        # BUG #6 FIX: Use UTC parsing for timezone consistency
        target_time = Time.parse(created_at_str).utc
        
        # BUG #9 FIX (CRITICAL): Ensure timestamp progression - if parsed timestamp is not after last used, bump it forward
        # This handles cases where multiple operations have the same historical timestamp (e.g., ops 1 and 2 both at 2011-08-18 11:54:44)
        if last_used_timestamp && target_time <= last_used_timestamp
          original_time = target_time
          target_time = last_used_timestamp + SYNTHETIC_TIMESTAMP_INCREMENT_US
          puts "J2O bulk item #{idx}: Op #{op_idx+1} timestamp collision detected: #{original_time} <= #{last_used_timestamp}, adjusted to #{target_time}" if verbose
        else
          puts "J2O bulk item #{idx}: Op #{op_idx+1} using timestamp: #{target_time}" if verbose
        end
      elsif last_used_timestamp
        # BUG #1 FIX: Synthetic timestamp with microsecond increment
        target_time = last_used_timestamp + SYNTHETIC_TIMESTAMP_INCREMENT_US
        puts "J2O bulk item #{idx}: Op #{op_idx+1} using synthetic timestamp: #{target_time}" if verbose
      else
        # BUG #7 FIX (LOW): Prefer historical time over Time.now
        target_time = (rec.created_at || Time.now).utc
        puts "J2O bulk item #{idx}: Op #{op_idx+1} using fallback timestamp: #{target_time}" if verbose
      end

      # 2. Determine validity_period range BEFORE updating tracker
      # This ensures last_used_timestamp points to END of current journal, not START
      next_op = ops[op_idx + 1]
      if next_op
        # BUG #9 FIX (CRITICAL REGRESSION): Check 'timestamp' field for set_* operations
        next_created_at = next_op['created_at'] || next_op[:created_at] || next_op['timestamp'] || next_op[:timestamp]
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

      # 3. Update tracker for next operation - use END of validity period so next journal starts after this one
      # For bounded ranges, use the end. For endless ranges, next op will get its own timestamp or use fallback
      if journal.validity_period.end
        last_used_timestamp = journal.validity_period.end
      else
        # Endless range - next operation should use its own timestamp or fallback
        last_used_timestamp = target_time
      end

      # 4. Persist timestamps AND validity_period to journal
      # BUG #6 FIX (MEDIUM - SEC#2): update_columns needed for historical timestamps
      # Bypasses callbacks by design - required for migration to set past timestamps
      # BUG #32 FIX (REGRESSION): Must also persist validity_period, not just timestamps!
      puts "J2O bulk item #{idx}: DEBUG - Op #{op_idx+1} before update: persisted=#{journal.persisted?}, validity_period=#{journal.validity_period.inspect}" if verbose
      if journal.persisted?
        # BUG #9 FIX (CRITICAL - VALIDITY PERIOD CONFLICT): Use raw SQL for v1 updates
        # Rails update_columns triggers exclusion constraint violation when updating validity_period
        # Raw SQL performs atomic UPDATE that PostgreSQL handles correctly
        conn = ActiveRecord::Base.connection

        # Format timestamps for PostgreSQL (Ruby 3.4 compatible)
        # Ensure Time object before calling strftime to avoid Ruby 3.4 to_s(:db) errors
        target_time_str = target_time.to_time.strftime('%Y-%m-%d %H:%M:%S.%6N%:z')

        # Build PostgreSQL tstzrange literal for validity_period
        if journal.validity_period.end
          # Bounded range: [start, end)
          # Convert to Time object to ensure strftime compatibility
          period_end_time = journal.validity_period.end.is_a?(Time) ? journal.validity_period.end : Time.parse(journal.validity_period.end.to_s)
          period_end_str = period_end_time.strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
          range_sql = "tstzrange('#{target_time_str}', '#{period_end_str}', '[)')"
        else
          # Endless range: [start, ∞)
          range_sql = "tstzrange('#{target_time_str}', NULL, '[)')"
        end

        sql = <<~SQL
          UPDATE journals
          SET created_at = '#{target_time_str}',
              updated_at = '#{target_time_str}',
              validity_period = #{range_sql}
          WHERE id = #{journal.id}
        SQL

        conn.execute(sql)
        puts "J2O bulk item #{idx}: DEBUG - Op #{op_idx+1} raw SQL update complete" if verbose
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

    # BUG #14 FIX: Track actual journal-creating operations separately from metadata-only operations
    # set_journal_user only updates v1's user_id, it should NOT create a new journal
    journal_creating_op_idx = 0  # Counter for actual journal-creating operations

    ops.each_with_index do |op, op_idx|
      begin
        op_type = op['type'] || op[:type]

        # BUG #14 FIX (CRITICAL): Skip set_journal_user - it should NOT create new journals
        # This operation is meant to set the user on v1, but with Bug #12 sorting it ends up
        # at the END of operations (no timestamp → sorted last) and creates phantom journals.
        # The first operation already sets v1's user_id correctly, so we skip this entirely.
        if op_type == 'set_journal_user'
          puts "J2O bulk item #{idx}: SKIP set_journal_user (Bug #14 fix - no phantom journal)" if verbose
          next
        end

        # BUG #18 FIX: Skip timestamp-only operations that don't create meaningful journals
        # These operations modify timestamps/metadata but don't have notes or field_changes.
        # When processed as op_idx > 0, they create phantom "The changes were retracted" journals.
        # Only the first operation (op_idx=0) should be processed from these types to set v1 metadata.
        timestamp_only_ops = ['set_created_at', 'set_updated_at', 'set_closed_at', 'set_journal_created_at']
        if timestamp_only_ops.include?(op_type) && op_idx != 0
          puts "J2O bulk item #{idx}: SKIP #{op_type} at op_idx=#{op_idx} (Bug #18 fix - timestamp-only, no journal content)" if verbose
          next
        end

        # BUG #15 + BUG #16 + BUG #18 FIX: Skip ALL operations with no meaningful content
        # With Bug #16 Python fix, unmapped Jira fields are now captured in notes.
        # With Bug #18, we generalize to skip ANY empty operation (not just create_comment).
        # This prevents "The changes were retracted" phantom journals from any operation type.
        # Exception: First operation (op_idx=0) must always be processed to create v1.
        notes_preview = op['notes'] || op[:notes]
        field_changes_preview = op['field_changes'] || op[:field_changes]

        # BUG #18 DEBUG: Log what we're checking
        puts "J2O bulk item #{idx}: DEBUG op_idx=#{op_idx} type=#{op_type} notes=#{notes_preview.inspect[0..40]} field_changes=#{field_changes_preview.inspect[0..40]}" if verbose

        is_empty_operation = (notes_preview.nil? || notes_preview.to_s.strip.empty?) &&
                             (field_changes_preview.nil? || field_changes_preview.empty?)
        if is_empty_operation && op_idx != 0
          puts "J2O bulk item #{idx}: SKIP empty #{op_type} at #{op['created_at'] || op['timestamp']} (Bug #18 fix - no content)" if verbose
          next
        end

        # BUG #13 FIX: Fallback to work package author if user_id is 0 or nil
        # user_id=0 causes HTTP 500 on activities page (route matching fails)
        # BUG #17 FIX: Use work package author_id as fallback instead of DeletedUser (ID 2)
        # This is more accurate for historical attribution - the WP author is known and exists
        raw_user_id = (op['user_id'] || op[:user_id]).to_i
        fallback_user_id = rec.author_id && rec.author_id > 0 ? rec.author_id : 2
        user_id = raw_user_id > 0 ? raw_user_id : fallback_user_id
        # BUG #9 FIX (CRITICAL - LINE 254): Check 'timestamp' field for set_* operations
        # Operations like set_created_at, set_updated_at use 'timestamp' not 'created_at'
        created_at_str = op['created_at'] || op[:created_at] || op['timestamp'] || op[:timestamp]
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

        # BUG #21 FIX: Create Journal::CustomizableJournal records for CF state tracking
        # This allows OpenProject to show "J2O Jira Workflow changed from X to Y" in activity
        # NOTE: When work package is created via API with CF values, OpenProject auto-creates
        # CustomizableJournal entries for v1. We need to REPLACE those with our cf_state_snapshot values.
        # CRITICAL: This runs for ALL operations (including v1) - moved OUTSIDE the if-else block!
        if journal && op.is_a?(Hash) && (op.has_key?("cf_state_snapshot") || op.has_key?(:cf_state_snapshot))
          cf_state_snapshot = op["cf_state_snapshot"] || op[:cf_state_snapshot]

          # Get the J2O CF IDs that we're tracking (Workflow and Resolution)
          # These are the only CFs we want to manage - delete existing and replace with our snapshot
          j2o_cf_ids = []
          workflow_cf = CustomField.find_by(name: "J2O Jira Workflow")
          resolution_cf = CustomField.find_by(name: "J2O Jira Resolution")
          j2o_cf_ids << workflow_cf.id if workflow_cf
          j2o_cf_ids << resolution_cf.id if resolution_cf

          if j2o_cf_ids.any?
            # DEBUG: Show what exists before delete
            existing_cf_entries = Journal::CustomizableJournal.where(
              journal_id: journal.id,
              custom_field_id: j2o_cf_ids
            )
            puts "J2O bulk item #{idx}: DEBUG - v#{journal.version} (journal_id=#{journal.id}) has #{existing_cf_entries.count} existing J2O CF entries: #{existing_cf_entries.map { |e| "CF#{e.custom_field_id}=#{e.value}" }.join(', ')}" if verbose

            # DELETE existing CF journal entries for J2O CFs (auto-created by OpenProject during WP creation)
            deleted_count = existing_cf_entries.delete_all
            puts "J2O bulk item #{idx}: Deleted #{deleted_count} existing J2O CF journal entries for v#{journal.version}" if deleted_count > 0
          end

          # CREATE new CF journal entries from cf_state_snapshot
          if cf_state_snapshot.is_a?(Hash) && cf_state_snapshot.any?
            cf_state_snapshot.each do |cf_id, cf_value|
              next if cf_id.nil? || cf_value.nil?
              begin
                # Create the customizable journal entry
                Journal::CustomizableJournal.create!(
                  journal_id: journal.id,
                  custom_field_id: cf_id.to_i,
                  value: cf_value.to_s
                )
                puts "J2O bulk item #{idx}: Created CF journal entry for v#{journal.version}: CF #{cf_id}=#{cf_value}" if verbose
              rescue => cf_error
                puts "J2O bulk item #{idx}: WARNING - Failed to create CF journal for v#{journal.version}: #{cf_error.message}" if verbose
              end
            end
          end
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
