"""Smoke tests ensuring legacy migration run() implementations exercise extract→map→load."""

from unittest.mock import MagicMock

import pytest

from src.application.components.affects_versions_migration import AffectsVersionsMigration
from src.application.components.components_migration import ComponentsMigration
from src.application.components.customfields_generic_migration import CustomFieldsGenericMigration
from src.application.components.versions_migration import VersionsMigration
from src.models import ComponentResult

# Only migrations that use the standard ETL pipeline (extract→map→load via _run_etl_pipeline).
# AttachmentsMigration and AttachmentProvenanceMigration have custom run() methods
# that process per-project and don't follow the ETL pattern.
LEGACY_MIGRATIONS = (
    ComponentsMigration,
    VersionsMigration,
    AffectsVersionsMigration,
    CustomFieldsGenericMigration,
)


def _make_migration(cls):
    jira_client = MagicMock()
    op_client = MagicMock()
    return cls(jira_client=jira_client, op_client=op_client)


@pytest.mark.parametrize("migration_cls", LEGACY_MIGRATIONS)
def test_run_propagates_extract_failure(migration_cls):
    migration = _make_migration(migration_cls)
    failure = ComponentResult(success=False, message="extract failed")
    migration._extract = MagicMock(return_value=failure)  # type: ignore[attr-defined]
    migration._map = MagicMock()  # type: ignore[attr-defined]
    migration._load = MagicMock()  # type: ignore[attr-defined]

    result = migration.run()

    assert result is failure
    migration._map.assert_not_called()
    migration._load.assert_not_called()


@pytest.mark.parametrize("migration_cls", LEGACY_MIGRATIONS)
def test_run_happy_path_invokes_pipeline(migration_cls):
    migration = _make_migration(migration_cls)
    extracted = ComponentResult(success=True, data={})
    mapped = ComponentResult(success=True, data={})
    loaded = ComponentResult(success=True, updated=1, failed=0)

    migration._extract = MagicMock(return_value=extracted)  # type: ignore[attr-defined]
    migration._map = MagicMock(return_value=mapped)  # type: ignore[attr-defined]
    migration._load = MagicMock(return_value=loaded)  # type: ignore[attr-defined]

    result = migration.run()

    assert result is loaded
    migration._extract.assert_called_once()
    migration._map.assert_called_once_with(extracted)
    migration._load.assert_called_once_with(mapped)
