"""Tests for fast-forward helpers in WorkPackageMigration."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import config
from src.migrations.work_package_migration import WorkPackageMigration

pytestmark = pytest.mark.unit


@pytest.fixture
def migration(tmp_path):
    """Create a minimal WorkPackageMigration instance for helper testing."""
    instance = WorkPackageMigration.__new__(WorkPackageMigration)
    instance._checkpoint_migration_id = "test_wp_migration"
    instance._checkpoint_db_path = tmp_path / ".migration_checkpoints.db"
    instance._project_latest_issue_ts = {}
    # Provide a lightweight logger with the attributes used by helper methods
    instance.logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        success=lambda *args, **kwargs: None,
        notice=lambda *args, **kwargs: None,
    )
    return instance


def test_build_key_exclusion_clause_limits_payload():
    """Ensure the exclusion clause caps the list size and skips empty keys."""
    raw_keys = {f"KEY-{i}" for i in range(950)}
    raw_keys.update({"", "   ", "KEY-1"})  # include blanks and duplicates

    clause = WorkPackageMigration._build_key_exclusion_clause(raw_keys)

    assert clause.startswith("key NOT IN (")
    # Expect at most 900 comma-separated values inside the clause
    values = clause.removeprefix("key NOT IN (").removesuffix(")").split(",")
    assert len(values) == 900
    assert all(value.startswith("KEY-") for value in values)


def test_parse_datetime_handles_diverse_inputs():
    """Verify _parse_datetime normalizes various timestamp formats to UTC."""
    iso_with_z = WorkPackageMigration._parse_datetime("2024-01-02T03:04:05Z")
    assert iso_with_z is not None and iso_with_z.tzinfo == UTC

    iso_with_offset = WorkPackageMigration._parse_datetime("2024-01-02T04:04:05+01:00")
    assert iso_with_offset is not None
    assert iso_with_offset.tzinfo == UTC
    # 04:04:05+01:00 should map to 03:04:05 UTC
    assert iso_with_offset.hour == 3

    date_without_tz = WorkPackageMigration._parse_datetime("2024-01-02 03:04")
    assert date_without_tz is not None and date_without_tz.tzinfo == UTC

    assert WorkPackageMigration._parse_datetime("not-a-date") is None
    assert WorkPackageMigration._parse_datetime(None) is None


def test_checkpoint_round_trip(migration: WorkPackageMigration):
    """Persist and reload checkpoint metadata via the helper utilities."""
    project_key = "FOO"
    migrated_at = datetime(2024, 5, 1, 12, 30, tzinfo=UTC)

    migration._update_project_checkpoint(project_key, migrated_at, migrated_count=42)

    # Ensure the timestamp can be loaded back via the public helper
    loaded = migration._get_checkpoint_timestamp(project_key)
    assert loaded is not None
    assert loaded == migrated_at

    # Inspect underlying database payload for completeness
    with sqlite3.connect(str(migration._checkpoint_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT data
            FROM migration_checkpoints
            WHERE migration_id = ?
              AND entity_id = ?
            """,
            (migration._checkpoint_migration_id, project_key),
        ).fetchone()

    assert row is not None
    payload = json.loads(row["data"])
    assert payload["project_key"] == project_key
    assert payload["migrated_count"] == 42
    assert payload["last_success_at"] == migrated_at.isoformat()


def test_prepare_work_package_collects_watcher_counters(monkeypatch):
    """Ensure watcher warnings are aggregated onto work_package _log_counters."""

    class DummyTimestampMigrator:
        def migrate_timestamps(self, *args, **kwargs):
            return {"warnings": [], "errors": []}

        def _normalize_timestamp(self, value: str) -> str | None:
            return value

    fields = SimpleNamespace(
        issuetype=SimpleNamespace(id="10", name="Story"),
        status=SimpleNamespace(id="5"),
        summary="Story summary",
        description="Story description",
        watches=SimpleNamespace(watchCount=0),
    )
    issue = SimpleNamespace(
        id="12345",
        key="PROJ-1",
        fields=fields,
        raw={"fields": {}},
    )

    class DummyMappings:
        issue_type_id_mapping: dict[str, int] = {}

        def get_mapping(self, name: str):
            if name == "issue_type_id":
                return self.issue_type_id_mapping
            return {}

    dummy_mappings = DummyMappings()
    monkeypatch.setattr(config, "mappings", dummy_mappings)
    monkeypatch.setattr(config, "get_mappings", lambda: dummy_mappings)
    monkeypatch.setattr(config, "jira_config", {"url": "https://jira.test"})

    instance = WorkPackageMigration.__new__(WorkPackageMigration)
    instance.logger = SimpleNamespace(
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        success=lambda *args, **kwargs: None,
        notice=lambda *args, **kwargs: None,
    )
    instance.project_mapping = {}
    instance.user_mapping = {}
    instance.issue_type_mapping = {"10": {"openproject_id": 8}}
    instance.issue_type_id_mapping = {}
    instance.status_mapping = {"5": {"openproject_id": 22}}
    instance.data_dir = Path(".")
    instance.markdown_converter = SimpleNamespace(convert=lambda text: text)
    instance.start_date_fields = []
    instance._j2o_wp_cf_ids_full = {}
    instance.op_client = SimpleNamespace(ensure_custom_field=lambda *args, **kwargs: {"id": 1})
    instance.jira_client = SimpleNamespace(get_issue_watchers=lambda key: [])
    instance.enhanced_audit_trail_migrator = SimpleNamespace(
        extract_changelog_from_issue=lambda *_args, **_kwargs: [],
        extract_comments_from_issue=lambda *_args, **_kwargs: [],
        changelog_data={},
    )

    def fake_user_associations(jira_issue, work_package_data, preserve_creator_via_rails=True):
        work_package_data["watcher_ids"] = [101, 202]
        return {
            "warnings": [
                "Watcher foo unmapped, skipping",
                "Watcher foo unmapped, skipping",
                "Watcher bar unmapped, skipping",
            ],
            "errors": [],
        }

    instance.enhanced_user_migrator = SimpleNamespace(
        migrate_user_associations=fake_user_associations,
    )
    instance.enhanced_timestamp_migrator = DummyTimestampMigrator()

    work_package = WorkPackageMigration._prepare_work_package(
        instance,
        jira_issue=issue,
        project_id=1,
    )

    counters = work_package.get("_log_counters", {})
    assert counters.get("watcher_unmapped") == 3
