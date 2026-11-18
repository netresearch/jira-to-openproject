# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from unittest.mock import Mock

from src.application.components.workflow_migration import WorkflowMigration


def test_named_status_fallback_dedup_and_unmapped_sources(monkeypatch):
    from src import config

    jira, op = Mock(), Mock()
    jira.get_issue_types.return_value = [{"id": "10", "name": "Bug"}]
    jira.get_workflow_schemes.return_value = [{"defaultWorkflow": "Standard"}]
    jira.get_workflow_transitions.return_value = [
        {"from": ["1", "1", "unknown"], "to": {"name": "Closed"}},
        {"from": "1", "to": {"id": "2"}},
    ]
    jira.get_workflow_statuses.return_value = []
    op.get_roles.return_value = [{"id": 4, "name": "Project member"}, {"id": 9, "name": "Guest"}]
    mappings = Mock()
    mappings.get_mapping.side_effect = {
        "status": {"1": {"openproject_id": 11}, "2": {"jira_name": "Closed", "openproject_id": 12}},
        "issue_type": {"Bug": {"openproject_id": 8}},
    }.get
    monkeypatch.setattr(config, "mappings", mappings)
    monkeypatch.setattr(config, "migration_config", {"workflow_roles": ["Project member"]})
    migration = WorkflowMigration(jira, op)
    result = migration._map(migration._extract())
    assert result.success
    assert result.data == {
        "role_ids": [4],
        "transitions": [
            {
                "type_id": 8,
                "from_status_id": 11,
                "to_status_id": 12,
                "jira_issue_type": "Bug",
                "jira_workflow": "Standard",
            }
        ],
        "skipped": [
            {"reason": "missing_status_mapping", "issue_type": "Bug", "workflow": "Standard", "status_id": "unknown"}
        ],
    }
