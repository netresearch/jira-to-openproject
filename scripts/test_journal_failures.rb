#!/usr/bin/env ruby
# Test script to reproduce journal creation failures for NRS-182
# Run in Rails console: rails runner scripts/test_journal_failures.rb

puts "=== Journal Creation Failure Tests ==="
puts

# Get the NRS-182 work package
wp = WorkPackage.find(5578081)
puts "Work Package: #{wp.id} (#{wp.subject})"
puts "Current journals: #{wp.journals.count}"
puts

# Test 1: NOT NULL Violation (Operations 9, 15)
puts "=== Test 1: NOT NULL Violation ==="
puts "Simulating operation with nil status_id in field_changes"
begin
  max_version = wp.journals.maximum(:version) || 0

  journal = Journal.new(
    journable_id: wp.id,
    journable_type: 'WorkPackage',
    user_id: 1,
    notes: 'Test operation with nil field',
    version: max_version + 1
  )

  # Simulate field_changes with nil value
  wp_data_attrs = {
    type_id: wp.type_id,
    project_id: wp.project_id,
    subject: wp.subject,
    description: wp.description,
    due_date: wp.due_date,
    category_id: wp.category_id,
    status_id: nil,  # <- This causes NOT NULL violation
    assigned_to_id: wp.assigned_to_id,
    priority_id: wp.priority_id,
    version_id: wp.version_id,
    author_id: wp.author_id,
    done_ratio: wp.done_ratio,
    estimated_hours: wp.estimated_hours,
    start_date: wp.start_date,
    parent_id: wp.parent_id,
    schedule_manually: (wp.respond_to?(:schedule_manually) ? wp.schedule_manually : false),
    ignore_non_working_days: (wp.respond_to?(:ignore_non_working_days) ? wp.ignore_non_working_days : false)
  }

  journal.data = Journal::WorkPackageJournal.new(wp_data_attrs)
  journal.validity_period = (Time.now...)
  journal.save(validate: false)

  puts "❌ UNEXPECTED: Save succeeded (should have failed)"
rescue ActiveRecord::NotNullViolation => e
  puts "✅ EXPECTED: NOT NULL violation"
  puts "   Error: #{e.message[0..100]}"
end
puts

# Test 2: CHECK Constraint Violation (Operations 23-27)
puts "=== Test 2: CHECK Constraint Violation ==="
puts "Simulating operation with missing created_at (nil validity_period)"
begin
  max_version = wp.journals.maximum(:version) || 0

  journal = Journal.new(
    journable_id: wp.id,
    journable_type: 'WorkPackage',
    user_id: 1,
    notes: 'Test operation without validity_period',
    version: max_version + 1
  )

  journal.data = Journal::WorkPackageJournal.new(
    type_id: wp.type_id,
    project_id: wp.project_id,
    subject: wp.subject,
    description: wp.description,
    due_date: wp.due_date,
    category_id: wp.category_id,
    status_id: wp.status_id,
    assigned_to_id: wp.assigned_to_id,
    priority_id: wp.priority_id,
    version_id: wp.version_id,
    author_id: wp.author_id,
    done_ratio: wp.done_ratio,
    estimated_hours: wp.estimated_hours,
    start_date: wp.start_date,
    parent_id: wp.parent_id,
    schedule_manually: (wp.respond_to?(:schedule_manually) ? wp.schedule_manually : false),
    ignore_non_working_days: (wp.respond_to?(:ignore_non_working_days) ? wp.ignore_non_working_days : false)
  )

  # Do NOT set validity_period - leave it nil
  # journal.validity_period = nil  <- This is the problem

  journal.save(validate: false)

  puts "❌ UNEXPECTED: Save succeeded (should have failed)"
rescue ActiveRecord::StatementInvalid => e
  if e.message.include?('journals_validity_period_not_empty')
    puts "✅ EXPECTED: CHECK constraint violation"
    puts "   Error: #{e.message[0..150]}"
  else
    puts "❌ UNEXPECTED ERROR: #{e.message[0..150]}"
  end
end
puts

# Test 3: EXCLUSION Constraint Violation (Multiple endless ranges)
puts "=== Test 3: EXCLUSION Constraint Violation ==="
puts "Simulating multiple operations with endless ranges"
begin
  max_version = wp.journals.maximum(:version) || 0
  fallback_time = Time.now

  # Create first journal with endless range
  journal1 = Journal.new(
    journable_id: wp.id,
    journable_type: 'WorkPackage',
    user_id: 1,
    notes: 'First operation with endless range',
    version: max_version + 1
  )

  journal1.data = Journal::WorkPackageJournal.new(
    type_id: wp.type_id,
    project_id: wp.project_id,
    subject: wp.subject,
    description: wp.description,
    due_date: wp.due_date,
    category_id: wp.category_id,
    status_id: wp.status_id,
    assigned_to_id: wp.assigned_to_id,
    priority_id: wp.priority_id,
    version_id: wp.version_id,
    author_id: wp.author_id,
    done_ratio: wp.done_ratio,
    estimated_hours: wp.estimated_hours,
    start_date: wp.start_date,
    parent_id: wp.parent_id,
    schedule_manually: (wp.respond_to?(:schedule_manually) ? wp.schedule_manually : false),
    ignore_non_working_days: (wp.respond_to?(:ignore_non_working_days) ? wp.ignore_non_working_days : false)
  )

  journal1.validity_period = (fallback_time..)
  journal1.save(validate: false)
  puts "   First journal created: v#{journal1.version}"

  # Create second journal with same endless range
  journal2 = Journal.new(
    journable_id: wp.id,
    journable_type: 'WorkPackage',
    user_id: 1,
    notes: 'Second operation with endless range',
    version: max_version + 2
  )

  journal2.data = Journal::WorkPackageJournal.new(
    type_id: wp.type_id,
    project_id: wp.project_id,
    subject: wp.subject,
    description: wp.description,
    due_date: wp.due_date,
    category_id: wp.category_id,
    status_id: wp.status_id,
    assigned_to_id: wp.assigned_to_id,
    priority_id: wp.priority_id,
    version_id: wp.version_id,
    author_id: wp.author_id,
    done_ratio: wp.done_ratio,
    estimated_hours: wp.estimated_hours,
    start_date: wp.start_date,
    parent_id: wp.parent_id,
    schedule_manually: (wp.respond_to?(:schedule_manually) ? wp.schedule_manually : false),
    ignore_non_working_days: (wp.respond_to?(:ignore_non_working_days) ? wp.ignore_non_working_days : false)
  )

  journal2.validity_period = (fallback_time..)  # Same endless range!
  journal2.save(validate: false)

  puts "❌ UNEXPECTED: Both saves succeeded (second should have failed)"

  # Cleanup
  journal1.destroy
  journal2.destroy

rescue ActiveRecord::StatementInvalid => e
  if e.message.include?('non_overlapping_journals_validity_periods')
    puts "✅ EXPECTED: EXCLUSION constraint violation"
    puts "   Error: Endless ranges overlap"
    puts "   Detail: #{e.message.match(/DETAIL:.*$/)[0][0..150]}" if e.message =~ /DETAIL:/

    # Cleanup first journal
    Journal.where(journable_id: wp.id, notes: 'First operation with endless range').destroy_all
  else
    puts "❌ UNEXPECTED ERROR: #{e.message[0..150]}"
  end
end
puts

puts "=== Summary ==="
puts "These tests reproduce the exact failures encountered during NRS-182 migration:"
puts "1. Operations 9, 15: field_changes with nil values → NOT NULL violation"
puts "2. Operations 23-27: missing created_at → nil validity_period → CHECK violation"
puts "3. Fallback attempt: multiple endless ranges → EXCLUSION violation"
