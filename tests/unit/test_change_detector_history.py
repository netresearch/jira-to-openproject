# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json

from src.utils.change_detector import ChangeDetector


def test_compare_latest_state_with_snapshot_at_cutoff(tmp_path):
    detector = ChangeDetector(tmp_path)
    assert detector.get_changes_since_timestamp("issues", "2026-01-01") is None
    first = detector.create_snapshot([{"key": "A"}, {"key": "B"}], "issues")
    cutoff = json.loads(first.read_bytes())["timestamp"]
    detector.create_snapshot([{"key": "P"}], "projects")
    detector.create_snapshot([{"key": "A", "summary": "updated"}, {"key": "C"}], "issues")

    report = detector.get_changes_since_timestamp("issues", cutoff)
    assert report["baseline_snapshot_timestamp"] == cutoff
    assert report["changes_by_type"] == {"created": 1, "updated": 1, "deleted": 1}
    assert [(change["entity_id"], change["change_type"]) for change in report["changes"]] == [
        ("B", "deleted"),
        ("C", "created"),
        ("A", "updated"),
    ]
    assert detector.get_changes_since_timestamp("issues", "2000-01-01T12:00:00Z") is None
    assert detector.get_changes_since_timestamp("issues", "2100-01-01")["total_changes"] == 0
