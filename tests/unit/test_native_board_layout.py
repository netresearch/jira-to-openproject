# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.native_boards import layout


def test_cards_follow_jira_rank_within_each_column():
    jira = Mock(base_url="https://jira.invalid")
    cols = [{"name": "Open", "statuses": [{"id": "1"}]}, {"name": "Done", "statuses": [{"id": "2"}]}]
    jira.get_board_configuration.return_value = {"columnConfig": {"columns": cols}}
    jira.agile.ranked_board_issues.return_value = [
        {"id": i, "key": f"APP-{i}", "fields": {"status": {"id": s}}} for i, s in [(3, "2"), (2, "1"), (1, "2")]
    ]
    board = {"id": 17, "name": "Scrum"}
    initial = layout(jira, board, "APP")
    assert [[r["id"] for r in col] for col in initial["cards"]] == [["2"], ["3", "1"]]
    assert initial == layout(jira, board, "APP")
    jira.agile.ranked_board_issues.return_value.reverse()
    assert layout(jira, board, "APP")["revision"] != initial["revision"]
    cols[1]["statuses"] = [{"id": "1"}]
    with pytest.raises(ValueError, match="multiple"):
        layout(jira, board, "APP")


def test_help_without_a_migration_directory(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    run = subprocess.run(
        [sys.executable, "-m", "src.native_boards", "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(repo)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--jira-project" in run.stdout
    assert list(tmp_path.iterdir()) == []
