# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Jira's "no value" placeholders are skipped, not written as refused values."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Jira Server returns this for a select list set to "None" (live run, NR-58).
NONE_OPTION = {
    "self": "https://jira.example.com/rest/api/2/customFieldOption/-1",
    "value": "",
    "id": "-1",
    "disabled": False,
}


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.config as cfg

    class DummyMappings:
        def get_mapping(self, name: str) -> dict:
            return {}

        def set_mapping(self, name: str, value: object) -> None:
            pass

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def _build_mig(tmp_path: Path):
    from src.application.components.work_package_content_migration import WorkPackageContentMigration

    jira = MagicMock()
    jira.select_comments_for_migration.return_value.comments = []
    jira.select_comments_for_migration.return_value.held = 0
    mig = WorkPackageContentMigration(jira_client=jira, op_client=MagicMock())
    mig.data_dir = tmp_path
    return mig


def test_empty_placeholders_are_not_sent_and_not_counted(tmp_path, _mock_mappings, monkeypatch) -> None:
    from src import config

    monkeypatch.setattr(config, "migration_config", {"batch_size": 10})
    mig = _build_mig(tmp_path)
    mig.work_package_mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}
    mig.custom_field_mapping = {
        "customfield_1": {"openproject_id": 91, "openproject_type": "list"},
        "customfield_2": {"openproject_id": 92, "openproject_type": "list"},
        "customfield_3": {"openproject_id": 93, "openproject_type": "list"},
        "customfield_4": {"openproject_id": 94, "openproject_type": "list"},
        "customfield_5": {"openproject_id": 95, "openproject_type": "string"},
        "customfield_6": {"openproject_id": 96, "openproject_type": "list"},
    }
    mig.jira_client.get_projects.return_value = [{"key": "PROJ"}]
    issue = SimpleNamespace(
        key="PROJ-1",
        id="1001",
        fields=SimpleNamespace(description=None),
        raw={
            "fields": {
                "customfield_1": NONE_OPTION,
                "customfield_2": [NONE_OPTION],
                "customfield_3": [],
                "customfield_4": {"value": "Yes", "id": "10"},
                "customfield_5": "",
                "customfield_6": [NONE_OPTION, {"value": "Blue", "id": "11"}],
            }
        },
    )
    mig.jira_client.jira.search_issues.return_value = [issue]
    monkeypatch.setattr(mig, "iter_project_issues", lambda project: iter([issue]))
    mig.op_client.batch_update_work_packages.return_value = {"updated": 2, "failed": 0, "results": []}

    result = mig.run()

    mig.op_client.batch_update_work_packages.assert_called_once_with(
        [
            {"id": 5001, "customField94": {"value": "Yes", "id": "10"}},
            {"id": 5001, "customField96": [{"value": "Blue", "id": "11"}]},
        ]
    )
    assert result.details["custom_fields_failed"] == 0
    assert result.details["custom_fields_updated"] == 2


def test_selection_with_child_or_real_option_is_kept(tmp_path, _mock_mappings) -> None:
    mig = _build_mig(tmp_path)
    mig.custom_field_mapping = {
        "customfield_1": {"openproject_id": 91, "openproject_type": "list"},
        "customfield_2": {"openproject_id": 92, "openproject_type": "list"},
        "customfield_3": {"openproject_id": 93, "openproject_type": "text"},
    }
    cascading = {"value": "", "id": "-1", "child": {"value": "Leaf", "id": "12"}}
    account = {"id": "72", "key": "NR", "name": "Netresearch"}
    issue = SimpleNamespace(
        key="PROJ-1",
        raw={
            "fields": {"customfield_1": cascading, "customfield_2": {"value": "0", "id": "0"}, "customfield_3": account}
        },
    )

    assert mig._custom_field_updates(issue) == {
        "customField91": cascading,
        "customField92": {"value": "0", "id": "0"},
        "customField93": account,
    }


def test_legacy_work_package_creation_skips_empty_placeholders() -> None:
    from src.application.components.work_package_migration import WorkPackageMigration

    mig = WorkPackageMigration.__new__(WorkPackageMigration)
    mig.logger = MagicMock()
    mig._load_custom_field_mapping = lambda: {  # type: ignore[method-assign]
        "customfield_1": {"openproject_id": 91},
        "customfield_2": {"openproject_id": 92},
        "customfield_3": {"openproject_id": 93},
    }

    values = mig._source_custom_values({"customfield_1": NONE_OPTION, "customfield_2": "", "customfield_3": 0})

    assert values == [{"id": 93, "value": 0}]


def test_each_placeholder_shape_counts_as_empty(tmp_path, _mock_mappings) -> None:
    mig = _build_mig(tmp_path)
    mig.custom_field_mapping = {
        "customfield_1": {"openproject_id": 91, "openproject_type": "list"},
        "customfield_2": {"openproject_id": 92, "openproject_type": "list"},
    }
    issue = SimpleNamespace(key="PROJ-1", raw={"fields": {"customfield_1": {"id": -1}, "customfield_2": {"value": ""}}})

    assert mig._custom_field_updates(issue) == {}
