# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Verify the result set of a translated Jira project filter in OpenProject."""

import pytest

from src.infrastructure.openproject.openproject_content_service import OpenProjectContentService
from tests.integration.test_openproject_agile_roundtrip import rails  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_saved_jira_project_filter_keeps_its_exact_scope(rails):  # noqa: F811
    client, read = rails
    service = OpenProjectContentService(client)
    parent, parent_wp, child_wp = read("""
      projects = WorkPackage.order(:project_id).distinct.pluck(:project_id)
      parent, child = Project.find(projects.take(2))
      child.update!(parent: parent)
      [parent.id, parent.work_packages.first!.id, child.work_packages.first!.id]
    """)
    name = "Jira Filter 998877"
    payload = {
        "project_id": parent,
        "name": name,
        "is_public": True,
        "options": {"include_subprojects": False, "filters": []},
    }
    first = service.create_or_update_query(**payload)
    assert first["success"], first
    query_id = int(first["id"])
    read(f"Query.find({query_id}).update!(include_subprojects: true)")
    assert service.create_or_update_query(**payload)["id"] == query_id
    matches = read(f"Query.find({query_id}).results.work_packages.map(&:id)")
    assert parent_wp in matches
    assert child_wp not in matches

    read(f"Query.find({query_id}).update!(public: false)")
    assert not service.create_or_update_query(**payload)["success"]
    assert read(f"Query.find({query_id}).public") is False
    assert not service.create_or_update_query(**(payload | {"project_id": -1}))["success"]
    assert not read("Query.global.exists?(name: 'Jira Filter 998877')")
