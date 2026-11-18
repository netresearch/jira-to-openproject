# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Read source IDs back from mapping work packages inside a rolled-back transaction."""

import pytest

from src.domain import provenance_fields
from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService
from src.infrastructure.openproject.openproject_provenance_service import OpenProjectProvenanceService
from tests.integration.test_openproject_agile_roundtrip import rails as rails

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_single_and_bulk_mapping_keep_jira_source_ids(rails):
    client, query = rails
    project_id = query("WorkPackage.first!.project_id")
    client.ensure_reporting_project.return_value = project_id
    client.get_project_by_identifier.return_value = {"id": project_id}
    client.custom_fields = OpenProjectCustomFieldService(client)
    service = OpenProjectProvenanceService(client)
    service.ensure_migration_project = lambda: project_id
    cf_ids = service.ensure_provenance_custom_fields()
    source_field = cf_ids[provenance_fields.ORIGIN_ID]
    single = service.record_entity(entity_type="type", jira_key="A", jira_id="00123", op_entity_id=7)
    assert single.get("success"), single
    bulk = service.bulk_record_entities("type", [{"jira_key": "B", "jira_id": "00456", "op_entity_id": 8}])
    assert bulk.get("success") == 1, bulk
    values = query(
        f"CustomValue.where(custom_field_id: {source_field}, value: %w[00123 00456]).order(:value).pluck(:value)"
    )
    assert values == ["00123", "00456"]
    restored = service.restore_entity_mappings("type")
    assert restored["A"]["j2o_origin_id"] == "00123"
    assert restored["B"]["j2o_origin_id"] == "00456"
