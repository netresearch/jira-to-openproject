# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The bulk custom field writer enables a field in the work package's own project and type (#502).

Components such as security_levels and story_points enable nothing themselves for ordinary work
packages: the skeleton writes no project ID into the mapping. The value is only visible because
the writer enables the field before it writes. This check runs the production script on a live
work package inside a transaction that is rolled back.
"""

import json
import os
import subprocess

import pytest

from src.infrastructure.openproject.openproject_work_package_custom_field_service import (
    OpenProjectWorkPackageCustomFieldService,
)

# An explicit ID for the field the transaction creates, so that the script can name it in advance.
FIELD_ID = 2147483000


def _production_script(work_package: int) -> str:
    """The script the writer sends for one row, captured instead of executed."""
    captured: list[str] = []

    class Capture:
        def execute_query_to_json_file(self, script, timeout=None):
            captured.append(script)
            return {"updated": 1, "results": []}

    service = OpenProjectWorkPackageCustomFieldService.__new__(OpenProjectWorkPackageCustomFieldService)
    service._client = Capture()
    service._logger = __import__("logging").getLogger(__name__)
    service.bulk_set_wp_custom_field_values(
        [{"work_package_id": work_package, "custom_field_id": FIELD_ID, "value": "Synthetic level"}]
    )
    return captured[0]


@pytest.mark.integration
def test_a_value_in_a_field_no_project_had_enabled_is_visible_in_the_api():
    container = os.getenv("J2O_LIVE_TIME_ENTRY_CONTAINER")
    work_package = os.getenv("J2O_LIVE_TIME_ENTRY_WORK_PACKAGE")
    if not container or not work_package:
        pytest.skip("Select a live OpenProject container and work package for the rollback-only check")

    script = f"""
require 'json'
out = {{}}
ActiveRecord::Base.transaction do
  User.current = User.where(admin: true).active.first!
  raise 'field ID taken' if CustomField.exists?({FIELD_ID})
  cf = WorkPackageCustomField.create!(id: {FIELD_ID}, name: 'J2O enablement check', field_format: 'string', is_for_all: false)
  wp = WorkPackage.find({int(work_package)})
  out[:before] = wp.available_custom_fields.map(&:id).include?(cf.id)
  out[:result] = begin
{_production_script(int(work_package))}
  end
  w = WorkPackage.find(wp.id)
  out[:projects] = CustomFieldsProject.where(custom_field_id: cf.id).pluck(:project_id)
  out[:project] = w.project_id
  out[:available] = w.available_custom_fields.map(&:id).include?(cf.id)
  out[:api] = JSON.parse(API::V3::WorkPackages::WorkPackageRepresenter.create(w, current_user: User.current, embed_links: false).to_json)["customField#{{cf.id}}"]
  raise ActiveRecord::Rollback
end
out[:rolled_back] = !CustomField.exists?({FIELD_ID})
puts 'J2O_OUT' + out.to_json
"""
    result = subprocess.run(
        ["docker", "exec", "-i", container, "bundle", "exec", "rails", "runner", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    line = next(line for line in result.stdout.splitlines() if line.startswith("J2O_OUT"))
    out = json.loads(line.removeprefix("J2O_OUT"))
    assert out["before"] is False
    assert out["result"]["updated"] == 1, out["result"]
    assert out["projects"] == [out["project"]]
    assert out["available"] is True
    assert out["api"] == "Synthetic level"
    assert out["rolled_back"] is True
