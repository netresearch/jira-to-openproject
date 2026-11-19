# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Exercise the catalogue's production storage transaction on a temporary private project."""

import os
import subprocess

import pytest

from src.application.components.tempo_attribute_catalogue import _STORE


@pytest.mark.integration
def test_private_catalogue_storage():
    container = os.getenv("J2O_LIVE_TIME_ENTRY_CONTAINER")
    if not container:
        pytest.skip("Select a live OpenProject container for the rollback-only catalogue check")
    setup = """
require 'json'
require 'securerandom'
$j2o_start_marker = 'catalogue-start'
$j2o_end_marker = 'catalogue-end'
User.current = User.admin.active.first!
write = lambda do |input_data|
"""
    verify = """
  result
end
Project.transaction do
  id = 'catalogue-' + SecureRandom.hex(8)
  project = Project.create!(name: id, identifier: id, public: false, workspace_type: 'project')
  data = {'project' => id}
  raise unless write.call(data)[:packet].nil?
  saved = write.call(data.merge('expected' => nil, 'replacement' => 'first-test-envelope'))
  raise unless saved[:packet] == 'first-test-envelope'
  raise unless write.call(data.merge('expected' => nil, 'replacement' => 'stale')) == {conflict: true}
  raise unless write.call(data) == saved
  updated = write.call(data.merge('expected' => Digest::SHA256.hexdigest(saved.fetch(:packet)), 'replacement' => 'second-test-envelope'))
  raise unless updated[:packet] == 'second-test-envelope' && updated[:field] == saved[:field]
  raise unless ProjectCustomField.find(saved.fetch(:field)).admin_only?
  raise unless ProjectCustomFieldProjectMapping.where(custom_field_id: saved.fetch(:field), project_id: project.id).count == 1
  project.update_column(:public, true)
  raise unless write.call(data).key?(:error)
  raise ActiveRecord::Rollback
end
puts 'catalogue rollback verified'
"""
    result = subprocess.run(
        ["docker", "exec", "-i", container, "bundle", "exec", "rails", "runner", "-"],
        input=setup + _STORE + verify,
        text=True,
        capture_output=True,
        timeout=120,
        check=True,
    )
    assert "catalogue rollback verified" in result.stdout
