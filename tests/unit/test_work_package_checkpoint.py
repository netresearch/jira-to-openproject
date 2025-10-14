"""Tests for WorkPackageMigration checkpoint helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.migrations.work_package_migration import WorkPackageMigration

pytestmark = pytest.mark.unit


@pytest.fixture
def migration(tmp_path: Path) -> WorkPackageMigration:
    instance = WorkPackageMigration.__new__(WorkPackageMigration)
    instance.logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        success=lambda *args, **kwargs: None,
        notice=lambda *args, **kwargs: None,
    )
    instance.data_dir = tmp_path
    instance._checkpoint_migration_id = "work_package_migration"
    instance._checkpoint_db_path = tmp_path / ".migration_checkpoints.db"
    instance._project_latest_issue_ts = {}
    return instance


def test_update_project_checkpoint_persists_entry(migration: WorkPackageMigration) -> None:
    ts = datetime(2024, 10, 5, 12, 0, tzinfo=UTC)

    migration._update_project_checkpoint("PROJ", ts, migrated_count=3)

    db_path = migration._checkpoint_db_path
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT data FROM migration_checkpoints
            WHERE migration_id = ? AND entity_id = ?
            """,
            (migration._checkpoint_migration_id, "PROJ"),
        ).fetchone()
    assert row is not None
    assert ts.isoformat() in row[0]


def test_get_checkpoint_timestamp_reads_latest(migration: WorkPackageMigration) -> None:
    ts = datetime(2024, 10, 5, 12, 0, tzinfo=UTC)
    migration._update_project_checkpoint("PROJ", ts, migrated_count=3)

    loaded = migration._get_checkpoint_timestamp("PROJ")
    assert loaded == ts


def test_get_checkpoint_timestamp_handles_missing_db(migration: WorkPackageMigration) -> None:
    migration._checkpoint_db_path = Path("/nonexistent/db.sqlite")
    assert migration._get_checkpoint_timestamp("PROJ") is None


def test_reset_checkpoint_store_rotates_file(migration: WorkPackageMigration, tmp_path: Path) -> None:
    migration._checkpoint_db_path.write_text("not a database", encoding="utf-8")

    migration._reset_checkpoint_store()

    assert not migration._checkpoint_db_path.exists()
    backups = list(tmp_path.glob(".migration_checkpoints.db.*.bak"))
    assert backups


def test_handle_corrupt_checkpoint_db_resets_store(migration: WorkPackageMigration) -> None:
    migration._checkpoint_db_path.write_text("corrupt", encoding="utf-8")

    migration._handle_corrupt_checkpoint_db(RuntimeError("boom"))

    assert not migration._checkpoint_db_path.exists()
