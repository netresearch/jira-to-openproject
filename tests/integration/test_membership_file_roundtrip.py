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
