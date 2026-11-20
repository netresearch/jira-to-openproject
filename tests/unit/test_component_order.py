# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Components run only after the mappings they read have been written."""

import pytest

from src.application.components.registry import DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES


@pytest.mark.parametrize("sequence", [DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]])
def test_groups_run_after_projects_and_before_admin_schemes(sequence) -> None:
    # GroupMigration._collect_project_role_groups reads the project mapping;
    # AdminSchemeMigration reads the group mapping.
    assert sequence.index("users") < sequence.index("groups")
    assert sequence.index("projects") < sequence.index("groups") < sequence.index("admin_schemes")
