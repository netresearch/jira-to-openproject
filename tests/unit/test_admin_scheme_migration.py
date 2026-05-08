"""Unit tests for AdminSchemeMigration component.

Covers happy path (user + group role assignment), unmapped role skip,
group fallback (refresh from OpenProject when mapping is empty), and
loud-fail behaviour when the OP client raises during user assignment.
"""

from __future__ import annotations

import pytest

from src.application.components.admin_scheme_migration import AdminSchemeMigration


class DummyJira:
    """Stub of the bits of JiraClient AdminSchemeMigration touches."""

    def __init__(self) -> None:
        self.roles = {
            "PROJ": [
                {
                    "name": "Administrators",
                    "actors": [
                        {"type": "atlassian-user-role-actor", "name": "alice", "accountId": "alice-id"},
                        {"type": "atlassian-group-role-actor", "name": "devs", "groupName": "devs"},
                    ],
                },
                {
                    "name": "Unknown Role Here",
                    "actors": [{"type": "atlassian-user-role-actor", "name": "bob"}],
                },
            ],
        }

    def get_project_roles(self, project_key: str):
        return self.roles.get(project_key, [])

    def get_project_permission_scheme(self, project_key: str):
        return {"id": 100, "name": "Default"}


class DummyOp:
    def __init__(self, *, fail_user_assign: bool = False) -> None:
        self.fail_user_assign = fail_user_assign
        self.user_calls: list[dict] = []
        self.group_calls: list[list[dict]] = []
        self.refresh_groups: list[dict] = []

    def get_roles(self):
        return [
            {"id": 3, "name": "Project admin"},
            {"id": 4, "name": "Project member"},
            {"id": 5, "name": "Reader"},
        ]

    def assign_user_roles(self, **kwargs):
        self.user_calls.append(kwargs)
        if self.fail_user_assign:
            return {"success": False, "error": "boom"}
        return {"success": True}

    def assign_group_roles(self, assignments):
        self.group_calls.append(assignments)
        return {"updated": len(assignments), "errors": 0}

    def get_groups(self):
        return self.refresh_groups


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    """Install DummyMappings via the cfg.mappings proxy seam."""
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PROJ": {"openproject_id": 11}},
                "user": {
                    "alice": {"openproject_id": 21},
                    "alice-id": {"openproject_id": 21},
                },
                "group": {"devs": {"openproject_id": 31}},
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_admin_scheme_migration_end_to_end_creates_user_and_group_assignments(
    _mock_mappings: None,
) -> None:
    """Happy path: one user role, one group role, plus an unmapped role skip."""
    op = DummyOp()
    mig = AdminSchemeMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert extracted.success is True
    assert extracted.total_count == 1  # one project
    assert mapped.success is True
    assert mapped.details["user_assignments"] == 1
    assert mapped.details["group_assignments"] == 1
    # The "Unknown Role Here" role and bob's actor (unmapped role) should be skipped.
    assert mapped.details["skipped"] >= 1
    assert result.success is True
    # Group + 1 user assignment counted as "updated"
    assert result.success_count == 2
    # Both side-effects were invoked once
    assert len(op.user_calls) == 1
    assert op.user_calls[0]["project_id"] == 11
    assert op.user_calls[0]["user_id"] == 21
    assert sorted(op.user_calls[0]["role_ids"]) == [3]
    assert len(op.group_calls) == 1


def test_admin_scheme_migration_skips_projects_without_op_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project with no openproject_id must not produce role data."""
    import src.config as cfg

    class EmptyProjectMappings:
        def get_mapping(self, name: str):
            return {} if name != "project" else {"PROJ": {}}

        def set_mapping(self, name: str, value):
            return None

    monkeypatch.setattr(cfg, "mappings", EmptyProjectMappings(), raising=False)

    mig = AdminSchemeMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    extracted = mig._extract()

    assert extracted.success is True
    # Project is filtered out because op mapping is missing.
    assert extracted.data == {"projects": []}


def test_admin_scheme_migration_run_refreshes_group_mapping_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() falls back to op_client.get_groups() when group mapping is empty."""
    import src.config as cfg

    class GrouplessMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PROJ": {"openproject_id": 11}},
                "user": {"alice": {"openproject_id": 21}, "alice-id": {"openproject_id": 21}},
                "group": {},
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    monkeypatch.setattr(cfg, "mappings", GrouplessMappings(), raising=False)

    op = DummyOp()
    op.refresh_groups = [{"id": 99, "name": "devs"}]
    mig = AdminSchemeMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    result = mig.run()

    assert result.success is True
    # Refreshed group mapping picked up the OP-side group.
    assert "devs" in mig.group_mapping


def test_admin_scheme_migration_load_propagates_user_assignment_errors(
    _mock_mappings: None,
) -> None:
    """When OP returns success=False on a user assignment, _load fails loud."""
    op = DummyOp(fail_user_assign=True)
    mig = AdminSchemeMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert result.success is False
    assert result.failed_count >= 1
