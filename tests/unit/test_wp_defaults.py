# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from src.application.components import wp_defaults


class _StubOPClient:
    def __init__(self, types=None, statuses=None, priorities=None, admins=None):
        self._types = types or []
        self._statuses = statuses or []
        self._priorities = priorities or []
        self._admins = admins or []

    def execute_large_query_to_json_file(
        self,
        query: str,
        container_file: str = "/tmp/j2o_query.json",
        timeout: int | None = None,
    ):
        if "Type.order(:position).pluck(:id)" in query:
            return list(self._types)
        if "Status.order(:position).pluck(:id)" in query:
            return list(self._statuses)
        if "IssuePriority.order(:position).pluck(:id)" in query:
            return list(self._priorities)
        if "User.where(admin: true).limit(1).pluck(:id)" in query:
            return list(self._admins)
        return []


def test_choose_default_type_id_uses_first_available():
    op = _StubOPClient(types=[5, 7, 9])
    assert wp_defaults.choose_default_type_id(op) == 5


def test_apply_required_defaults_sets_missing_fields():
    op = _StubOPClient(types=[11, 22], statuses=[101, 202], priorities=[301, 302], admins=[401])

    records = [
        {"project_id": 123, "subject": "X"},
        {"project_id": 123, "type_id": 22, "status_id": 202, "priority_id": 302, "author_id": 401},
    ]

    # Apply with no configured fallback admin
    wp_defaults.apply_required_defaults(records, project_id=123, op_client=op, fallback_admin_user_id=None)

    # First record should have all defaulted
    r0 = records[0]
    assert r0["type_id"] == 11
    assert r0["status_id"] == 101
    assert r0["priority_id"] == 301
    assert r0["author_id"] == 401

    # Second record should remain unchanged
    r1 = records[1]
    assert r1["type_id"] == 22
    assert r1["status_id"] == 202
    assert r1["priority_id"] == 302
    assert r1["author_id"] == 401


def test_status_lookup_failure_is_distinct_from_empty_results(caplog):
    class UnavailableStatusClient(_StubOPClient):
        def execute_large_query_to_json_file(self, query, **kwargs):
            if query.startswith("Status."):
                raise TimeoutError
            return super().execute_large_query_to_json_file(query, **kwargs)

    defaults, statuses = wp_defaults.work_package_defaults(_StubOPClient(), 7, 3)
    assert (defaults["status_id"], statuses) == (1, [])
    assert not [record for record in caplog.records if record.name == wp_defaults.logger.name]

    defaults, statuses = wp_defaults.work_package_defaults(UnavailableStatusClient(), 7, 3)
    assert (defaults["status_id"], statuses) == (1, [])
    messages = [record.getMessage() for record in caplog.records if record.name == wp_defaults.logger.name]
    assert len(messages) == 1
    assert "Status.order" in messages[0]
    assert "TimeoutError" in messages[0]
