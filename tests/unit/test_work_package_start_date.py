"""Unit tests for WorkPackageMigration start date resolution."""

from types import SimpleNamespace

from src.migrations.work_package_migration import WorkPackageMigration


class DummyNormalizer:
    """Minimal helper providing _normalize_timestamp."""

    def __call__(self, value: str | None) -> str | None:
        return value

    def _normalize_timestamp(self, value: str | None) -> str | None:  # pragma: no cover - compatibility
        return self(value)


def _make_issue(fields: dict[str, object], raw_fields: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        fields=SimpleNamespace(**fields),
        raw={"fields": raw_fields or fields},
    )


def test_resolve_start_date_primary_field_precedence() -> None:
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.start_date_fields = WorkPackageMigration.START_DATE_FIELD_IDS_DEFAULT
    migration.enhanced_timestamp_migrator = DummyNormalizer()
    migration.status_category_by_id = {}
    migration.status_category_by_name = {}

    issue = _make_issue(
        {
            "customfield_18690": "2024-07-01T08:00:00+00:00",
            "customfield_12590": "2024-07-02T08:00:00+00:00",
        },
    )

    assert migration._resolve_start_date(issue) == "2024-07-01"


def test_resolve_start_date_fallback_to_secondary_field() -> None:
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.start_date_fields = WorkPackageMigration.START_DATE_FIELD_IDS_DEFAULT
    migration.enhanced_timestamp_migrator = DummyNormalizer()
    migration.status_category_by_id = {}
    migration.status_category_by_name = {}

    issue = _make_issue(
        {
            "customfield_18690": None,
            "customfield_12590": "2024-08-05T08:00:00+00:00",
        },
    )

    assert migration._resolve_start_date(issue) == "2024-08-05"


def test_resolve_start_date_from_raw_dict_issue() -> None:
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.start_date_fields = WorkPackageMigration.START_DATE_FIELD_IDS_DEFAULT
    migration.enhanced_timestamp_migrator = DummyNormalizer()
    migration.status_category_by_id = {}
    migration.status_category_by_name = {}

    issue = {
        "fields": {
            "customfield_18690": None,
            "customfield_12590": None,
            "customfield_11490": "2024-09-10T08:00:00+00:00",
        }
    }

    assert migration._resolve_start_date(issue) == "2024-09-10"


def test_resolve_start_date_none_when_no_fields() -> None:
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.start_date_fields = WorkPackageMigration.START_DATE_FIELD_IDS_DEFAULT
    migration.enhanced_timestamp_migrator = DummyNormalizer()
    migration.status_category_by_id = {}
    migration.status_category_by_name = {}

    issue = _make_issue(
        {
            "customfield_18690": None,
            "customfield_12590": None,
            "customfield_11490": None,
            "customfield_15082": None,
        },
    )

    assert migration._resolve_start_date(issue) is None


def test_resolve_start_date_history_fallback_dict() -> None:
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.start_date_fields = []
    migration.enhanced_timestamp_migrator = DummyNormalizer()
    migration.status_category_by_id = {
        "3": {"id": 4, "key": "indeterminate", "name": "In Progress"},
    }
    migration.status_category_by_name = {
        "in progress": {"id": 4, "key": "indeterminate", "name": "In Progress"},
    }

    issue = {
        "fields": {},
        "changelog": {
            "histories": [
                {
                    "created": "2024-09-10T08:00:00.000+0200",
                    "items": [
                        {"field": "status", "to": "3", "toString": "In Progress"},
                    ],
                },
            ],
        },
    }

    assert migration._resolve_start_date(issue) == "2024-09-10"


def test_resolve_start_date_history_ignores_non_progress() -> None:
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.start_date_fields = []
    migration.enhanced_timestamp_migrator = DummyNormalizer()
    migration.status_category_by_id = {
        "5": {"id": 5, "key": "done", "name": "Done"},
    }
    migration.status_category_by_name = {
        "done": {"id": 5, "key": "done", "name": "Done"},
    }

    issue = {
        "fields": {},
        "changelog": {
            "histories": [
                {
                    "created": "2024-09-10T08:00:00.000+0200",
                    "items": [
                        {"field": "status", "to": "5", "toString": "Done"},
                    ],
                },
            ],
        },
    }

    assert migration._resolve_start_date(issue) is None
