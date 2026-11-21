# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Components run only after the mappings they read have been written."""

import pytest

from src.application.components.registry import DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES


@pytest.mark.parametrize("sequence", [DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]])
def test_semantic_identifiers_run_after_the_skeleton_and_before_attachments(sequence) -> None:
    # The identifiers need every work package of the run, level work packages included.
    assert sequence.index("work_packages_skeleton") < sequence.index("semantic_identifiers")
    assert sequence.index("semantic_identifiers") < sequence.index("attachments")


@pytest.mark.parametrize("sequence", [DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]])
def test_groups_run_after_projects_and_before_admin_schemes(sequence) -> None:
    # GroupMigration._collect_project_role_groups reads the project mapping;
    # AdminSchemeMigration reads the group mapping.
    assert sequence.index("users") < sequence.index("groups")
    assert sequence.index("projects") < sequence.index("groups") < sequence.index("admin_schemes")


# Stages whose Ruby refuses to write once the target project has members:
# simpletasks (tasklist_origins.py) and components (component_assignments.py).
MEMBERLESS_STAGING = ("simpletasks", "components")
# Stages that need project members: category_defaults confirms the default
# assignee's existing membership; admin_schemes grants role memberships itself.
NEEDS_MEMBERS = ("category_defaults", "admin_schemes")


@pytest.mark.parametrize("sequence", [DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]])
def test_groups_grant_project_access_after_memberless_staging(sequence) -> None:
    # GroupMigration._assign_project_roles adds the first project members.
    groups = sequence.index("groups")
    assert [stage for stage in MEMBERLESS_STAGING if sequence.index(stage) > groups] == []
    assert [stage for stage in NEEDS_MEMBERS if sequence.index(stage) < groups] == []


@pytest.mark.parametrize("sequence", [DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]])
def test_level_access_runs_after_the_parent_memberships_and_before_reporting(sequence) -> None:
    # SecurityLevelAccessMigration copies each reader's roles in the parent project, which
    # groups and admin_schemes grant; every level stage before it writes into the sub-projects.
    assert sequence.index("admin_schemes") < sequence.index("security_level_access") < sequence.index("reporting")
    assert sequence.index("groups") < sequence.index("security_level_access")
