"""Unit tests for ReportingMigration component.

Covers happy path (filter → query, dashboard → wiki page), dashboard
without sharePermissions falling back to the reporting wiki project,
extraction-failure propagation, and load-side error counting when OP
returns success=False.
"""

from __future__ import annotations

import pytest

from src.application.components.reporting_migration import ReportingMigration
from src.models import ComponentResult


class DummyJira:
    def __init__(
        self,
        filters: list[dict] | None = None,
        dashboards: list[dict] | None = None,
        boom_filters: bool = False,
    ) -> None:
        self._filters = filters or []
        self._dashboards = dashboards or []
        self._boom_filters = boom_filters

    def get_filters(self):
        if self._boom_filters:
            raise RuntimeError("filters api down")
        return self._filters

    def get_dashboards(self):
        return self._dashboards

    def get_dashboard_details(self, dash_id: int):
        # Production callers pass detail through; default to looking up the
        # matching dashboard from the listing so tests can place metadata
        # (sharePermissions, gadgets, …) on the listing entries directly.
        for d in self._dashboards:
            if d.get("id") == dash_id:
                return d
        return {"id": dash_id}


class DummyOp:
    def __init__(self, *, fail_query: bool = False, fail_wiki: bool = False) -> None:
        self.fail_query = fail_query
        self.fail_wiki = fail_wiki
        self.queries: list[dict] = []
        self.wikis: list[dict] = []
        self.reporting_calls: list[tuple[str, str]] = []

    def ensure_reporting_project(self, identifier: str, name: str):
        self.reporting_calls.append((identifier, name))
        return 555

    def create_or_update_query(self, **payload):
        self.queries.append(payload)
        if self.fail_query:
            return {"success": False, "error": "no"}
        return {"success": True, "id": 700 + len(self.queries)}

    def create_or_update_wiki_page(self, **payload):
        self.wikis.append(payload)
        if self.fail_wiki:
            return {"success": False, "error": "no"}
        return {"success": True, "id": 800 + len(self.wikis)}


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {"project": {"PROJ": {"openproject_id": 11}}}

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_reporting_migration_end_to_end_creates_query_and_wiki(_mock_mappings: None) -> None:
    """One filter → one query, one dashboard with PROJ share → one wiki page."""
    filters = [{"id": 10, "name": "My filter", "jql": "project = PROJ", "owner": {"displayName": "Alice"}}]
    dashboards = [
        {
            "id": 20,
            "name": "Ops",
            "sharePermissions": [{"project": {"key": "PROJ"}}],
            "gadgets": [{"title": "Pie chart"}],
        },
    ]
    op = DummyOp()
    mig = ReportingMigration(
        jira_client=DummyJira(filters=filters, dashboards=dashboards),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert extracted.success is True
    assert extracted.total_count == 2
    assert mapped.success is True
    assert mapped.details["filters"] == 1
    assert mapped.details["dashboards"] == 1
    assert result.success is True
    # One filter-query + one dashboard-wiki = 2 successful artefacts.
    assert result.success_count == 2
    # Wiki page got the matched project_id from sharePermissions (11), not the
    # reporting fallback (555).
    assert op.wikis[0]["project_id"] == 11


def test_reporting_migration_dashboard_without_share_uses_reporting_project(
    _mock_mappings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard with no share + ensure_reporting_project succeeds → falls back to reporting project."""
    # Isolate from any developer .env.local that pre-configures
    # reporting_wiki_project / reporting_wiki_project_name in
    # config.openproject_config — those would override the
    # ensure_reporting_project() fallback (555) and break the assertion.
    import src.config as cfg

    monkeypatch.setitem(cfg.openproject_config, "reporting_wiki_project", "")
    monkeypatch.setitem(cfg.openproject_config, "reporting_wiki_project_name", "")

    dashboards = [{"id": 30, "name": "Lonely", "sharePermissions": []}]
    op = DummyOp()
    mig = ReportingMigration(
        jira_client=DummyJira(dashboards=dashboards),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)

    assert mapped.success is True
    # Reporting project ensured at least once.
    assert len(op.reporting_calls) == 1
    # Wiki payload built using fallback reporting project id (555).
    assert mapped.data["wiki_pages"][0]["project_id"] == 555
    assert mapped.details["skipped_dashboards"] == 0


def test_reporting_migration_map_returns_failure_when_extract_failed(
    _mock_mappings: None,
) -> None:
    """A failed extract result short-circuits the map phase."""
    op = DummyOp()
    mig = ReportingMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    failed = ComponentResult(success=False, message="upstream went away")
    mapped = mig._map(failed)

    assert mapped.success is False
    assert "extraction failed" in (mapped.message or "").lower()


def test_reporting_migration_load_counts_op_failures(_mock_mappings: None) -> None:
    """When OP returns success=False on query create, _load reflects the failure count."""
    filters = [{"id": 1, "name": "x", "jql": "project=PROJ"}]
    dashboards = [{"id": 2, "name": "d", "sharePermissions": [{"project": {"key": "PROJ"}}]}]
    op = DummyOp(fail_query=True)
    mig = ReportingMigration(
        jira_client=DummyJira(filters=filters, dashboards=dashboards),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert result.success is False
    assert result.failed_count >= 1
    # Wiki page still got created in this scenario.
    assert result.success_count >= 1
