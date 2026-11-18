# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Verify row-level failure, counters and fill-only reruns on OpenProject."""

import json

import pytest

from src.application.components.wp_metadata_backfill_migration import WpMetadataBackfillMigration
from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService
from tests.integration.test_openproject_agile_roundtrip import rails as rails

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_backfill_rolls_back_failed_row_and_preserves_values_on_retry(rails):
    client, query = rails
    field_id = OpenProjectCustomFieldService(client).ensure_custom_field("J2O Backfill Test")["id"]
    wp_id = query("""
      template = WorkPackage.first!
      WorkPackage.create!(project: template.project, type: template.type, status: template.status,
        priority: template.priority, author: template.author, subject: "J2O metadata test").id
    """)

    def backfill(fields):
        data = [{"work_package_id": wp_id, "custom_fields": fields}]
        script = WpMetadataBackfillMigration._rails_script()
        return query(f"input_data = JSON.parse({json.dumps(json.dumps(data))})\n{script}\nstats")

    def persisted_values():
        return query(
            f"CustomValue.where(customized: WorkPackage.find({wp_id}), custom_field_id: {field_id}).pluck(:value)"
        )

    valid_field = {"id": field_id, "value": "original"}
    failed = backfill([valid_field, {"id": -1, "value": "invalid"}])
    assert failed["failed"] == 1
    assert failed["updated_cf"] == 0
    assert persisted_values() == []
    assert backfill([valid_field])["updated_cf"] == 1
    assert backfill([{"id": field_id, "value": "different"}])["skipped"] == 1
    assert persisted_values() == ["original"]
