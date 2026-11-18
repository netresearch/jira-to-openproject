# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from scripts.migration_quality_analysis import analyze_differences, generate_report


def test_audit_reports_unmatched_people_missing_fields_and_matching_mapped_ids():
    jira = [
        {
            "jira_key": "TEST-1",
            "fields": {
                "priority": "Urgent",
                "priority_id": "1",
                "assignee": "ann",
                "reporter": "bob",
                "created": "2026-01-01",
            },
        }
    ]
    target = {"TEST-1": {"priority": "Hoch", "priority_id": 3, "author": "Different person", "author_id": 12}}
    mappings = {"priority_mapping": {"1": {"openproject_id": 3}}, "user_mapping": {"bob": {"openproject_id": 11}}}
    result = analyze_differences(jira, target, mappings)
    stats = result["field_analysis"]
    assert stats["priority"]["matches"] == 1
    assert stats["reporter"]["mismatches"] == 1
    assert stats["assignee"]["missing"] == stats["timestamps"]["missing"] == 1
    assert len(result["detailed_issues"][0]["differences"]) == 3
    assert "4323" not in generate_report(result)
