# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json
from unittest.mock import Mock

import pytest

from scripts import generate_description_updates as export
from scripts.update_nrs_descriptions import read_targets


def test_full_stream_is_required_before_publishing(tmp_path, monkeypatch):
    destination = tmp_path / "nrs_description_updates.json"
    destination.write_text("previous")
    client = Mock()
    monkeypatch.setattr(export, "JiraClient", lambda: client)
    row = {"key": "NRS-1", "fields": {"description": "Beschreibung"}}

    def incomplete():
        yield row
        raise OSError("source unavailable")

    client.stream_all_issues_for_project.return_value = incomplete()
    assert export.publish_updates({"NRS-1": 100}, tmp_path) == 1
    assert destination.read_text() == "previous"
    client.stream_all_issues_for_project.return_value = iter([row])
    assert export.publish_updates({"NRS-1": 100}, tmp_path) == 0
    assert json.loads(destination.read_text()) == [
        {"description": "Beschreibung", "description_length": 12, "jira_key": "NRS-1", "wp_id": 100}
    ]


def test_two_targets_for_one_jira_key_are_ambiguous():
    client = Mock()
    client.execute_script_with_data.return_value = {"status": "success", "data": [["NRS-1", 2], ["NRS-1", 3]]}
    with pytest.raises(ValueError, match="multiple target"):
        read_targets(client)
