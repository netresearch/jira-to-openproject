# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Verify the Rails membership import and cleanup in the running OpenProject container."""

from pathlib import PurePosixPath

import pytest

from src.infrastructure.openproject.openproject_membership_service import OpenProjectMembershipService
from tests.integration.test_openproject_agile_roundtrip import rails  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_private_membership_roundtrip(rails):  # noqa: F811
    client, query = rails
    service = OpenProjectMembershipService(client)
    user_id = query("User.admin.first!.id")
    assert service.sync_group_memberships([{"name": "J2O test group", "user_ids": [user_id]}]) == {
        "updated": 1,
        "errors": 0,
    }
    assert query("Group.find_by!(name: 'J2O test group').user_ids") == [user_id]
    assert service.sync_group_memberships([{"name": "J2O test group", "user_ids": [user_id]}]) == {
        "updated": 0,
        "errors": 0,
    }
    assert service.sync_group_memberships([{"name": "J2O test group", "user_ids": []}]) == {"updated": 1, "errors": 0}
    assert query("Group.find_by!(name: 'J2O test group').user_ids") == []
    remote = PurePosixPath(client.transfer_file_to_container.call_args.args[1]).parent
    assert remote.name.startswith("j2o-members-")
    assert not query(f"Dir.exist?('{remote}')")


def _member_fixture(query):
    """Create a non-admin user and a private project inside the rolled-back session."""
    return query(
        "u = User.create!(login: 'j2o-member', firstname: 'J2O', lastname: 'Member', "
        "mail: 'j2o-member@example.invalid', admin: false, status: :active, "
        "password: 'J2o-member-password-1!', password_confirmation: 'J2o-member-password-1!'); "
        "p = Project.create!(name: 'J2O members', identifier: 'j2o-members', public: false, workspace_type: 'project'); "
        "p.enabled_module_names = p.enabled_module_names | ['work_package_tracking']; p.save!; "
        "roles = ProjectRole.givable.select { |r| r.permissions.include?(:view_work_packages) }.first(2); "
        "{ user: u.id, project: p.id, roles: roles.map(&:id) }"
    )


def _can_view(query, ids):
    return query(f"User.find({ids['user']}).allowed_in_project?(:view_work_packages, Project.find({ids['project']}))")


def test_group_project_roles_reach_group_members(rails):  # noqa: F811
    client, query = rails
    service = OpenProjectMembershipService(client)
    ids = _member_fixture(query)
    service.sync_group_memberships([{"name": "J2O role group", "user_ids": [ids["user"]]}])
    assert _can_view(query, ids) is False

    row = {"group_name": "J2O role group", "project_id": ids["project"], "role_ids": ids["roles"][:1]}
    assert service.assign_group_roles([row]) == {"updated": 1, "errors": 0}
    assert _can_view(query, ids) is True
    assert service.assign_group_roles([row]) == {"updated": 0, "errors": 0}


def test_group_roles_repair_memberships_saved_without_inheritance(rails):  # noqa: F811
    client, query = rails
    service = OpenProjectMembershipService(client)
    ids = _member_fixture(query)
    service.sync_group_memberships([{"name": "J2O role group", "user_ids": [ids["user"]]}])
    # The state an earlier run left behind: the group's membership saved directly.
    query(
        f"Member.new(project: Project.find({ids['project']}), principal: Group.find_by!(name: 'J2O role group'), "
        f"role_ids: [{ids['roles'][0]}]).save!"
    )
    assert _can_view(query, ids) is False

    row = {"group_name": "J2O role group", "project_id": ids["project"], "role_ids": ids["roles"][:1]}
    assert service.assign_group_roles([row]) == {"updated": 0, "errors": 0}
    assert _can_view(query, ids) is True


def test_group_members_added_later_inherit_the_group_roles(rails):  # noqa: F811
    client, query = rails
    service = OpenProjectMembershipService(client)
    ids = _member_fixture(query)
    service.sync_group_memberships([{"name": "J2O role group", "user_ids": []}])
    row = {"group_name": "J2O role group", "project_id": ids["project"], "role_ids": ids["roles"][:1]}
    assert service.assign_group_roles([row]) == {"updated": 1, "errors": 0}
    assert _can_view(query, ids) is False

    service.sync_group_memberships([{"name": "J2O role group", "user_ids": [ids["user"]]}])
    assert _can_view(query, ids) is True


def test_user_roles_keep_roles_inherited_from_groups(rails):  # noqa: F811
    client, query = rails
    service = OpenProjectMembershipService(client)
    ids = _member_fixture(query)
    group_role, direct_role = ids["roles"]
    service.sync_group_memberships([{"name": "J2O role group", "user_ids": [ids["user"]]}])
    # Give the group its project role through OpenProject itself, independent of assign_group_roles.
    assert query(
        f"Members::CreateService.new(user: User.system).call(principal: Group.find_by!(name: 'J2O role group'), "
        f"project: Project.find({ids['project']}), role_ids: [{group_role}], send_notifications: false).success?"
    )

    result = service.assign_user_roles(project_id=ids["project"], user_id=ids["user"], role_ids=[direct_role])

    assert result["success"] is True
    rows = query(
        f"Member.find_by!(project_id: {ids['project']}, user_id: {ids['user']}, entity: nil)"
        ".member_roles.map { |mr| [mr.role_id, mr.inherited_from.present?] }.sort"
    )
    assert rows == sorted([[group_role, True], [direct_role, False]])
    assert (
        service.assign_user_roles(project_id=ids["project"], user_id=ids["user"], role_ids=[direct_role])["changed"]
        is False
    )


def test_user_roles_grant_access_to_a_new_member(rails):  # noqa: F811
    client, query = rails
    service = OpenProjectMembershipService(client)
    ids = _member_fixture(query)
    assert _can_view(query, ids) is False

    result = service.assign_user_roles(project_id=ids["project"], user_id=ids["user"], role_ids=ids["roles"][:1])

    assert result["success"] is True
    assert result["changed"] is True
    assert _can_view(query, ids) is True
