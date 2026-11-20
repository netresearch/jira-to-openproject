# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Users created during the run must reach the user mapping with their OpenProject IDs."""

from __future__ import annotations

import logging

from src.application.components.user_migration import UserMigration
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.openproject_user_service import OpenProjectUserService


def _client(rows: list[list[dict]]) -> OpenProjectClient:
    client = OpenProjectClient.__new__(OpenProjectClient)
    client.logger = logging.getLogger("test")
    client._users_cache = None
    client._users_cache_time = None
    client._users_by_email_cache = {}
    client._generate_unique_temp_filename = lambda name: f"/tmp/{name}.json"
    client.execute_large_query_to_json_file = lambda *_args, **_kwargs: rows.pop(0)
    client.users = OpenProjectUserService(client)
    return client


def test_forced_extraction_reads_users_created_after_the_first_read(tmp_path) -> None:
    before = [{"id": 1, "login": "admin"}]
    after = [*before, {"id": 7, "login": "created"}]
    migration = UserMigration.__new__(UserMigration)
    migration.logger = logging.getLogger("test")
    migration.data_dir = tmp_path
    migration.op_users = []
    migration.op_client = _client([before, after])

    assert [u["login"] for u in migration.extract_openproject_users()] == ["admin"]
    assert [u["login"] for u in migration.extract_openproject_users(force=True)] == ["admin", "created"]


def test_restore_from_openproject_reads_the_current_user_list(tmp_path) -> None:
    client = _client([[{"id": 1, "login": "admin"}], [{"id": 1, "login": "admin"}, {"id": 7, "login": "created"}]])
    client.get_users()
    migration = UserMigration.__new__(UserMigration)
    migration.logger = logging.getLogger("test")
    migration.data_dir = tmp_path
    migration.op_client = client
    migration.ensure_j2o_custom_fields = dict

    assert "created" in migration.restore_mapping_from_openproject()
