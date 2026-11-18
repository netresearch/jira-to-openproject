# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Run the production batch writer on an explicitly selected live work package."""

import os
import subprocess

import pytest

from src.infrastructure.openproject.openproject_time_entry_service import _BULK_TIME_ENTRY_BODY


@pytest.mark.integration
def test_native_worklog_round_trip_and_retry():
    container = os.getenv("J2O_LIVE_TIME_ENTRY_CONTAINER")
    work_package = os.getenv("J2O_LIVE_TIME_ENTRY_WORK_PACKAGE")
    if not container or not work_package:
        pytest.skip("Select a live OpenProject container and work package for the rollback-only check")

    setup = """
require 'json'
require 'tmpdir'
require 'securerandom'
require 'time'
wp = WorkPackage.find(Integer(ENV.fetch('TEST_WORK_PACKAGE')))
user = wp.author
activity = TimeEntryActivity.active.where(project_id: [nil, wp.project_id]).first!
key = 'integration:' + SecureRandom.uuid
TimeEntry.transaction do
  Dir.mktmpdir do |directory|
    data_path = File.join(directory, 'entries')
    result_path = File.join(directory, 'results')
    row = { index: 0, work_package_id: wp.id, target_project_id: wp.project_id,
            user_id: user.id, activity_id: activity.id, hours: 17.fdiv(3600),
            spent_on: '2024-01-16', comments: " Synthetic note\\n ",
            jira_worklog_key: key, jira_started: '2024-01-15T23:15:00.123+0000',
            jira_time_zone: 'Europe/Berlin', jira_update_author_id: user.id,
            jira_created_at: '2024-01-16T09:00:00Z', jira_updated_at: '2024-01-17T09:00:00Z' }
    File.write(data_path, [row, row, row.merge(comments: 'Changed'), row.merge(target_project_id: -1)].to_json)
"""
    assertions = """
    result = JSON.parse(File.read(result_path))
    raise unless result.values_at('created', 'reused', 'failed') == [1, 1, 2]
    first, second, conflict, wrong_project = result.fetch('results')
    raise unless first.fetch('id') == second.fetch('id') && second.fetch('reused')
    raise unless [first, second].all? { |receipt| receipt.fetch('verified_worklog_key') == key }
    raise if [conflict, wrong_project].any? { |receipt| receipt.key?('verified_worklog_key') }
    raise unless !conflict.fetch('success') && !wrong_project.fetch('success')
    te = TimeEntry.find(first.fetch('id'))
    raise unless te.entity == wp && te.user == user && te.logged_by_id == user.id
    raise unless (te.hours * 3600 - 17).abs < 1e-6 && te.comments == row[:comments]
    raise unless te.spent_on.iso8601 == row[:spent_on] && te.start_time.nil? && te.time_zone == row[:jira_time_zone]
    raise unless te.created_at == Time.iso8601(row[:jira_created_at]) && te.updated_at == Time.iso8601(row[:jira_updated_at])
    values = te.custom_values.joins(:custom_field).pluck('custom_fields.name', :value).to_h
    raise unless values.fetch('J2O Source Worklog Started') == row[:jira_started]
    raise unless values.fetch('J2O Worklog Update Author ID') == user.id.to_s
  end
  raise ActiveRecord::Rollback
end
raise if CustomValue.where(customized_type: 'TimeEntry', value: key).exists?
puts 'Verified worklog rollback'
"""
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "--env",
            f"TEST_WORK_PACKAGE={int(work_package)}",
            container,
            "bundle",
            "exec",
            "rails",
            "runner",
            "-",
        ],
        input=setup + _BULK_TIME_ENTRY_BODY + assertions,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert "Verified worklog rollback" in result.stdout
