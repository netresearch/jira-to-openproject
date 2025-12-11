import types


class _StubOPClient:
    def __init__(self, types=None, statuses=None, priorities=None, admins=None):
        self._types = types or []
        self._statuses = statuses or []
        self._priorities = priorities or []
        self._admins = admins or []

    def execute_large_query_to_json_file(
        self, query: str, container_file: str = "/tmp/j2o_query.json", timeout: int | None = None
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


def test_choose_default_type_id_uses_first_available(monkeypatch):
    import sys

    # Bypass heavy base_migration import by injecting minimal module for the test
    sys.modules.setdefault("src.migrations.base_migration", types.ModuleType("base_migration"))
    bm = sys.modules["src.migrations.base_migration"]
    bm.BaseMigration = object
    bm.register_entity_types = lambda *a, **k: lambda cls: cls

    from src.migrations import wp_defaults as wpm

    op = _StubOPClient(types=[5, 7, 9])
    assert wpm.choose_default_type_id(op) == 5


def test_apply_required_defaults_sets_missing_fields(monkeypatch):
    import sys

    sys.modules.setdefault("src.migrations.base_migration", types.ModuleType("base_migration"))
    bm = sys.modules["src.migrations.base_migration"]
    bm.BaseMigration = object
    bm.register_entity_types = lambda *a, **k: lambda cls: cls

    from src.migrations import wp_defaults as wpm

    op = _StubOPClient(types=[11, 22], statuses=[101, 202], priorities=[301, 302], admins=[401])

    records = [
        {"project_id": 123, "subject": "X"},
        {"project_id": 123, "type_id": 22, "status_id": 202, "priority_id": 302, "author_id": 401},
    ]

    # Apply with no configured fallback admin
    wpm.apply_required_defaults(records, project_id=123, op_client=op, fallback_admin_user_id=None)

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
