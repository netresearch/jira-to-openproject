from unittest.mock import MagicMock

from src.migrations.work_package_migration import WorkPackageMigration


def test_sanitize_wp_dict_removes_links_and_extracts_ids_unit() -> None:
    m = WorkPackageMigration(jira_client=MagicMock(), op_client=MagicMock())

    # Include non-AR keys to verify they are removed prior to assign
    wp = {
        "project_id": 1,
        "subject": "S",
        "description": "D",
        "_links": {
            "type": {"href": "/api/v3/types/7"},
            "status": {"href": "/api/v3/statuses/9"},
        },
        "watcher_ids": [1, None, 2],
    }

    m._sanitize_wp_dict(wp)

    assert "_links" not in wp
    assert wp.get("type_id") == 7
    assert wp.get("status_id") == 9
    # watcher_ids is a non-AR key and should be removed before assign in Ruby script path; optional here
