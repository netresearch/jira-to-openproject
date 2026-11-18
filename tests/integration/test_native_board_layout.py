# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json

import pytest

from src.infrastructure.openproject.native_board_writer import save_layout
from tests.integration.test_openproject_agile_roundtrip import rails  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_private_native_board_layout_and_replay(rails):  # noqa: F811
    client, query = rails
    identifiers = query("""
      root = Project.create!(name: 'Archive', identifier: 'board-snapshots', public: false, workspace_type: 'project')
      target = Project.create!(name: 'Target', identifier: 'snapshot-data', public: false, workspace_type: 'project')
      target.enabled_modules.find_or_create_by!(name: 'work_package_tracking')
      template = WorkPackage.first!
      cards = [1,2].map do |n|
        w = WorkPackage.create!(project: target, type: template.type, status: template.status,
          priority: template.priority, author: User.current, subject: "Card #{n}")
        {'ID'=>n.to_s, 'Key'=>"APP-#{n}", 'URL'=>"https://jira.invalid/browse/APP-#{n}", 'System'=>'Jira'}.each do |k,v|
          f=WorkPackageCustomField.find_or_create_by!(name:"J2O Origin #{k}"){|cf| cf.field_format='string'}
          CustomValue.create!(customized:w,custom_field:f,value:v)
        end
        w.id
      end
      cards
    """)

    def execute(script, payload, **kwargs):
        # Exercise the same structured Ruby data and retain the transaction around this entire fixture.
        script = script.partition("puts $j2o_start_marker")[0]
        return {
            "status": "success",
            "data": query("input_data=JSON.parse(" + json.dumps(json.dumps(payload)) + ");" + script),
        }

    client.execute_script_with_data.side_effect = execute
    board = {
        "jira": "https://jira.invalid",
        "project": "APP",
        "board": 7,
        "name": "Board",
        "revision": "1" * 64,
        "configuration": {"columnConfig": {"columns": [{"name": "First"}, {"name": "Empty"}]}},
        "cards": [[{"id": "2", "key": "APP-2"}, {"id": "1", "key": "APP-1"}], []],
    }
    first = save_layout(client, board, "snapshot-data", "board-snapshots")
    assert first["created"]
    assert first["cards"] == 2
    assert first["columns"] == 2
    again = save_layout(client, board, "snapshot-data", "board-snapshots")
    assert not again["created"]
    assert again["grid_id"] == first["grid_id"]
    assert (
        query(
            f"Query.find(Boards::Grid.find({first['grid_id']}).widgets.order(:start_column).first.options['queryId']).results.work_packages.pluck(:id)"
        )
        == identifiers[::-1]
    )
    rows_before = query("Boards::Grid.count")
    wrong = {**board, "revision": "2" * 64, "cards": [[{"id": "1", "key": "APP-999"}], []]}
    with pytest.raises(AssertionError, match="Card provenance differs"):
        save_layout(client, wrong, "snapshot-data", "board-snapshots")
    assert query("Boards::Grid.count") == rows_before
    query("Project.find_by!(identifier: 'board-snapshots').update!(public: true)")
    with pytest.raises(AssertionError, match="private root project"):
        save_layout(client, board, "snapshot-data", "board-snapshots")
