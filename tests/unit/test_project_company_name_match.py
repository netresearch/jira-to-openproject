# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""A Jira project named like its Tempo customer never maps onto the company container."""

from unittest.mock import MagicMock

import pytest

from src.application.components.project_migration import ProjectMigration

# Live run: Jira project BLUG "blugento" was mapped onto company container 13 (customer_blug).
CONTAINER = {"id": 13, "name": "Blugento", "identifier": "customer_blug"}
COMPANIES = {"26": {"openproject_id": 13, "openproject_identifier": "customer_blug", "tempo_name": "Blugento"}}
BLUG = {"key": "BLUG", "name": "blugento", "id": "14551"}


def _migration(op_projects):
    migration = ProjectMigration(MagicMock(), MagicMock())
    migration.project_filters = set()
    migration.project_mapping = {}
    migration.company_mapping = COMPANIES
    migration.op_projects = op_projects
    migration.jira_projects = [dict(BLUG)]
    return migration


def test_first_run_creates_the_project_instead_of_reusing_its_company_container():
    plans, mapping, setup = _migration([CONTAINER])._plan_projects()
    assert [(source["key"], data["identifier"]) for source, data in plans] == [("BLUG", "blug")]
    assert setup == []
    assert "BLUG" not in mapping


@pytest.mark.parametrize("container_last", [False, True])
def test_rerun_maps_the_created_child_project_beside_its_container(container_last):
    child = {"id": 300, "name": "blugento", "identifier": "blug"}
    projects = [child, CONTAINER] if container_last else [CONTAINER, child]
    plans, mapping, setup = _migration(projects)._plan_projects()
    assert plans == []
    assert mapping["BLUG"]["openproject_id"] == 300
    assert [target for target, _source in setup] == [300]
