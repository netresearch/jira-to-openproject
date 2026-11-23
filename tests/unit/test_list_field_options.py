# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""List fields get the options J2O writes; sprint data stays with its own component (ADR-017)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from src import config
from src.application.components import list_field_options
from src.application.components.list_field_options import (
    GH_SPRINT_CUSTOM_TYPE,
    ensure_requests,
    list_fields,
    option_labels,
)
from src.infrastructure.exceptions import QueryExecutionError

# Imported at collection time, before conftest replaces the class with a mock factory.
from src.infrastructure.openproject.openproject_client import OpenProjectClient

SELECT = "com.atlassian.jira.plugin.system.customfieldtypes:select"
MULTISELECT = "com.atlassian.jira.plugin.system.customfieldtypes:multiselect"
LABELS = "com.atlassian.jira.plugin.system.customfieldtypes:labels"
DIGEST = "b" * 64


# ── label derivation ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "labels"),
    [
        pytest.param({"self": "https://jira.example.com/o/1", "value": "Blue", "id": "1"}, ["Blue"], id="option"),
        pytest.param([{"value": "Red", "id": "2"}, {"value": "Green", "id": "3"}], ["Red", "Green"], id="options"),
        pytest.param(["alpha", "beta", "alpha"], ["alpha", "beta"], id="labels"),
        pytest.param({"id": 7, "name": "Example Org"}, ["Example Org"], id="organization"),
        pytest.param({"value": "Parent", "id": "4", "child": {"value": "Leaf", "id": "5"}}, [], id="cascading"),
        pytest.param([{"value": "Parent", "child": {"value": "Leaf"}}, {"value": "Solo"}], ["Solo"], id="mixed"),
        pytest.param({"value": False, "name": "Fallback"}, ["Fallback"], id="false-value-uses-name"),
        pytest.param({"value": "", "name": "Ignored"}, [], id="empty-value-kept-as-ruby-does"),
        pytest.param({"id": "9"}, [], id="no-label"),
        pytest.param([None, "kept"], ["kept"], id="none-item"),
        pytest.param("plain", ["plain"], id="scalar"),
        pytest.param([1, True, "1"], [1, True, "1"], id="scalars-stay-distinct"),
        pytest.param([["nested"]], [], id="nested-list"),
    ],
)
def test_labels_follow_the_writer_rule(value: Any, labels: list[Any]) -> None:
    assert option_labels(value) == labels


# ── which fields take part ───────────────────────────────────────────────


def _entry(op_id: int, *, jira_type: str = "option", custom: str = SELECT, op_type: str = "list") -> dict:
    return {"openproject_id": op_id, "openproject_type": op_type, "jira_type": jira_type, "jira_custom_type": custom}


def test_multi_is_set_for_jira_arrays_only() -> None:
    mapping = {
        "customfield_1": _entry(11),
        "customfield_2": _entry(12, jira_type="array", custom=MULTISELECT),
        "customfield_3": _entry(13, jira_type="array", custom=LABELS),
    }
    assert list_fields(mapping) == {11: False, 12: True, 13: True}


def test_a_shared_target_is_multi_when_any_source_is_an_array() -> None:
    mapping = {"customfield_1": _entry(11), "customfield_2": _entry(11, jira_type="array", custom=MULTISELECT)}
    assert list_fields(mapping) == {11: True}


@pytest.mark.parametrize(
    "custom",
    [
        "com.atlassian.jira.plugin.system.customfieldtypes:cascadingselect",
        "com.atlassian.jira.plugin.system.customfieldtypes:project",
        "com.atlassian.jira.plugin.system.customfieldtypes:version",
        "com.atlassian.jira.plugin.system.customfieldtypes:multiversion",
        "com.pyxis.greenhopper.jira:greenhopper-releasedmultiversionhistory",
    ],
)
def test_types_the_writer_refuses_take_no_part(custom: str) -> None:
    mapping = {"customfield_1": _entry(11, custom=custom), "customfield_2": _entry(12)}
    assert list_fields(mapping) == {12: False}
    # A target shared with a refused type is left out as a whole.
    assert list_fields({**mapping, "customfield_3": _entry(12, custom=custom)}) == {}


def test_the_sprint_field_takes_no_part() -> None:
    mapping = {
        "customfield_1": _entry(11, jira_type="array", custom=GH_SPRINT_CUSTOM_TYPE),
        "customfield_2": _entry(12),
    }
    assert list_fields(mapping) == {12: False}


def test_a_type_name_inside_another_name_does_not_exclude() -> None:
    mapping = {"customfield_1": _entry(11, custom="com.example.plugin:project-picker-select")}
    assert list_fields(mapping) == {11: False}


def test_only_mapped_list_fields_take_part() -> None:
    mapping = {
        "customfield_1": _entry(11, op_type="text"),
        "customfield_2": _entry(0),
        "customfield_3": {"openproject_id": None, "openproject_type": "list"},
        "customfield_4": 14,
        "customfield_5": _entry(15),
    }
    assert list_fields(mapping) == {15: False}


def test_requests_collect_the_labels_of_all_rows_per_field() -> None:
    rows = [
        {"id": 1, "customField11": {"value": "Blue"}},
        {"id": 2, "customField11": {"value": "Red"}},
        {"id": 2, "customField12": ["alpha", "beta"]},
        {"id": 3, "customField12": ["beta", "gamma"]},
        {"id": 3, "customField99": "not a list field"},
        {"id": 4, "description": "text"},
    ]
    assert ensure_requests(rows, {11: False, 12: True}) == {
        11: {"id": 11, "labels": ["Blue", "Red"], "multi": False},
        12: {"id": 12, "labels": ["alpha", "beta", "gamma"], "multi": True},
    }


# ── sprint ownership ─────────────────────────────────────────────────────


@pytest.fixture
def _mappings(monkeypatch: pytest.MonkeyPatch) -> dict:
    store: dict = {}

    class DummyMappings:
        def get_mapping(self, name: str) -> dict:
            return store.get(name, {})

        def set_mapping(self, name: str, value: dict) -> None:
            store[name] = value

    monkeypatch.setattr(config, "mappings", DummyMappings(), raising=False)
    return store


SPRINT_FIELD = {
    "id": "customfield_10100",
    "name": "Sprint",
    "schema": {"type": "array", "items": "string", "custom": GH_SPRINT_CUSTOM_TYPE, "customId": 10100},
}
COLOR_FIELD = {
    "id": "customfield_10200",
    "name": "Color",
    "schema": {"type": "option", "custom": SELECT, "customId": 10200},
}


OTHER_OP_FIELD = {"id": 1, "name": "Other", "field_format": "text"}


@pytest.mark.parametrize(
    "op_fields", [[OTHER_OP_FIELD], [OTHER_OP_FIELD, {"id": 300, "name": "Sprint", "field_format": "list"}]]
)
def test_the_sprint_field_gets_no_target_field(_mappings, op_fields) -> None:
    from src.application.components.custom_field_migration import CustomFieldMigration

    mig = CustomFieldMigration(jira_client=MagicMock(), op_client=MagicMock())
    mig.jira_custom_fields = [SPRINT_FIELD, COLOR_FIELD]
    mig.op_custom_fields = op_fields

    mapping = mig.create_custom_field_mapping()

    sprint = mapping["customfield_10100"]
    assert sprint["openproject_id"] is None
    assert sprint["matched_by"] == list_field_options.SPRINT_OWNER
    assert sprint["jira_custom_type"] == GH_SPRINT_CUSTOM_TYPE
    assert mapping["customfield_10200"]["matched_by"] == "create"
    records, _ = mig._custom_field_records([f for f in mapping.values() if f.get("matched_by") == "create"])
    assert [record["name"] for record in records] == ["Color"]


def test_a_created_field_named_sprint_is_not_bound_to_the_sprint_entry(_mappings) -> None:
    from src.application.components.custom_field_migration import CustomFieldMigration

    mig = CustomFieldMigration(jira_client=MagicMock(), op_client=MagicMock())
    mig.jira_custom_fields = [SPRINT_FIELD]
    mig.op_custom_fields = [OTHER_OP_FIELD]
    mig.create_custom_field_mapping()

    mig._update_mapping_with_op_ids([], [{"id": 300, "name": "Sprint"}], [])

    assert mig.mapping["customfield_10100"]["openproject_id"] is None


def _content_migration(monkeypatch: pytest.MonkeyPatch, mapping: dict):
    from src.application.components.work_package_content_migration import WorkPackageContentMigration

    monkeypatch.setattr(config, "migration_config", {"batch_size": 10})
    mappings = MagicMock()
    mappings.get_mapping.return_value = {}
    monkeypatch.setattr(config, "mappings", mappings, raising=False)
    mig = WorkPackageContentMigration(jira_client=MagicMock(), op_client=MagicMock())
    mig.custom_field_mapping = mapping
    return mig


def test_the_content_step_writes_no_sprint_value(monkeypatch) -> None:
    # A mapping file of an earlier run still binds the sprint field to a list field.
    mapping = {
        "customfield_10100": {**_entry(300, jira_type="array", custom=GH_SPRINT_CUSTOM_TYPE), "matched_by": "name"},
        "customfield_10200": _entry(301),
    }
    mig = _content_migration(monkeypatch, mapping)
    issue = SimpleNamespace(
        key="DEMO-1",
        raw={"fields": {"customfield_10100": ["Sprint 4"], "customfield_10200": {"value": "Blue", "id": "1"}}},
    )

    assert mig._custom_field_updates(issue) == {"customField301": {"value": "Blue", "id": "1"}}


def test_the_generic_step_writes_no_sprint_value() -> None:
    from src.application.components.customfields_generic_migration import CustomFieldsGenericMigration

    mig = CustomFieldsGenericMigration.__new__(CustomFieldsGenericMigration)
    fields = SimpleNamespace(customfield_10100=["Sprint 4"], customfield_10300="free text")
    mapping = {
        "customfield_10100": {"jira_custom_type": GH_SPRINT_CUSTOM_TYPE, "openproject_id": None, "matched_by": "x"}
    }

    assert mig._unmapped_values(fields, mapping) == [("customfield_10300", "text", "free text")]


# ── the content step ensures options before it writes ───────────────────


def _writes(rows: list[dict]) -> dict:
    answers = [{"id": r["id"], "status": "updated", "attr": r["_j2o_guard"]["attr"], "digest": DIGEST} for r in rows]
    return {"updated": len(rows), "failed": 0, "results": answers}


def _confirm(fields: list[dict], errors: dict | None = None, created: dict | None = None) -> dict:
    errors = errors or {}
    answers = {
        f["id"]: {"created": (created or {}).get(f["id"], []), "multi_changed": False, "placeholder_removed": False}
        for f in fields
        if f["id"] not in errors
    }
    return {"fields": answers, "errors": errors}


CONTENT_MAPPING = {
    "customfield_1": _entry(21),
    "customfield_2": _entry(22, jira_type="array", custom=MULTISELECT),
    "customfield_3": _entry(23, op_type="string", custom="textfield"),
}


def _items(base: int) -> list[dict]:
    return [
        {
            "wp_id": base,
            "description_update": None,
            "custom_field_updates": {
                "customField21": {"value": "Blue", "id": "1"},
                "customField22": [{"value": "Red"}, {"value": "Green"}],
                "customField23": "free",
            },
        },
        {"wp_id": base + 1, "description_update": None, "custom_field_updates": {"customField21": {"value": "Red"}}},
    ]


def _written_fields(mig) -> list[str]:
    return [
        key
        for call in mig.op_client.batch_update_work_packages.call_args_list
        for row in call.args[0]
        for key in row
        if key.startswith("customField")
    ]


def test_options_are_ensured_before_the_rows_are_written(monkeypatch, caplog) -> None:
    mig = _content_migration(monkeypatch, CONTENT_MAPPING)
    order: list[str] = []
    mig.op_client.ensure_list_field_options.side_effect = lambda fields: (
        order.append("ensure") or _confirm(fields, created={21: ["Blue", "Red"]})
    )
    mig.op_client.batch_update_work_packages.side_effect = lambda rows: order.append("write") or _writes(rows)

    with caplog.at_level(logging.INFO):
        mig._write_work_package_fields(_items(7101), results := {})

    assert order == ["ensure", "write"]
    mig.op_client.ensure_list_field_options.assert_called_once_with(
        [
            {"id": 21, "labels": ["Blue", "Red"], "multi": False},
            {"id": 22, "labels": ["Red", "Green"], "multi": True},
        ]
    )
    assert results["custom_fields_updated"] == 4
    assert results["custom_fields_failed"] == 0
    assert "Created list field options (field id: count): {21: 2, 22: 0}" in caplog.text


def test_a_failed_ensure_call_fails_every_list_row(monkeypatch, caplog) -> None:
    mig = _content_migration(monkeypatch, CONTENT_MAPPING)
    mig.op_client.ensure_list_field_options.side_effect = QueryExecutionError("console gone")
    mig.op_client.batch_update_work_packages.side_effect = _writes

    mig._write_work_package_fields(_items(7201), results := {})

    assert _written_fields(mig) == ["customField23"]
    assert results["custom_fields_updated"] == 1
    assert results["custom_fields_failed"] == 3
    assert "Could not ensure the options of list fields [21, 22]" in caplog.text
    assert "Not writing 3 values of list fields [21, 22]" in caplog.text


def test_a_refused_field_fails_only_its_own_rows(monkeypatch, caplog) -> None:
    mig = _content_migration(monkeypatch, CONTENT_MAPPING)
    mig.op_client.ensure_list_field_options.side_effect = lambda fields: _confirm(fields, errors={22: "not a list"})
    mig.op_client.batch_update_work_packages.side_effect = _writes

    mig._write_work_package_fields(_items(7301), results := {})

    assert sorted(_written_fields(mig)) == ["customField21", "customField21", "customField23"]
    assert results["custom_fields_updated"] == 3
    assert results["custom_fields_failed"] == 1
    assert "List field 22 refused its options: not a list" in caplog.text


def test_a_field_with_an_error_counts_as_failed_even_with_an_answer(monkeypatch) -> None:
    mig = _content_migration(monkeypatch, CONTENT_MAPPING)
    mig.op_client.ensure_list_field_options.side_effect = lambda fields: {
        "fields": _confirm(fields)["fields"],
        "errors": {22: "commit failed"},
    }
    mig.op_client.batch_update_work_packages.side_effect = _writes

    mig._write_work_package_fields(_items(7801), results := {})

    assert sorted(_written_fields(mig)) == ["customField21", "customField21", "customField23"]
    assert results["custom_fields_failed"] == 1


def test_a_field_missing_from_the_answer_counts_as_failed(monkeypatch) -> None:
    mig = _content_migration(monkeypatch, CONTENT_MAPPING)
    mig.op_client.ensure_list_field_options.side_effect = lambda fields: _confirm(fields[:1])
    mig.op_client.batch_update_work_packages.side_effect = _writes

    mig._write_work_package_fields(_items(7401), results := {})

    assert sorted(_written_fields(mig)) == ["customField21", "customField21", "customField23"]
    assert results["custom_fields_failed"] == 1


def test_rows_held_back_count_when_nothing_is_left_to_write(monkeypatch) -> None:
    mig = _content_migration(monkeypatch, CONTENT_MAPPING)
    mig.op_client.ensure_list_field_options.side_effect = QueryExecutionError("console gone")
    items = [{"wp_id": 7501, "description_update": None, "custom_field_updates": {"customField21": {"value": "X"}}}]

    mig._write_work_package_fields(items, results := {})

    mig.op_client.batch_update_work_packages.assert_not_called()
    assert results["custom_fields_failed"] == 1


def test_held_back_rows_add_to_a_failed_write(monkeypatch) -> None:
    mig = _content_migration(monkeypatch, CONTENT_MAPPING)
    mig.op_client.ensure_list_field_options.side_effect = lambda fields: _confirm(fields, errors={21: "refused"})
    mig.op_client.batch_update_work_packages.side_effect = QueryExecutionError("write failed")

    mig._write_work_package_fields(_items(7601), results := {})

    assert results["custom_fields_failed"] == 4
    assert results.get("custom_fields_updated", 0) == 0


def test_no_ensure_call_without_list_rows(monkeypatch) -> None:
    mig = _content_migration(monkeypatch, CONTENT_MAPPING)
    mig.op_client.batch_update_work_packages.side_effect = _writes
    items = [{"wp_id": 7701, "description_update": None, "custom_field_updates": {"customField23": "free"}}]

    mig._write_work_package_fields(items, results := {})

    mig.op_client.ensure_list_field_options.assert_not_called()
    assert results["custom_fields_updated"] == 1


# ── OpenProject adapters ─────────────────────────────────────────────────


class _Client:
    def __init__(self, json_result: Any = None, script_result: Any = None) -> None:
        self.logger = logging.getLogger("test.list_field_options")
        self.json_result = json_result
        self.script_result = script_result
        self.scripts: list[tuple[str, Any]] = []

    def execute_json_query(self, query: str, timeout: int | None = None) -> Any:
        return self.json_result

    def execute_script_with_data(self, script: str, data: Any, timeout: int | None = None) -> Any:
        self.scripts.append((script, data))
        return self.script_result


def test_the_client_delegates_the_ensure_call() -> None:
    client = OpenProjectClient.__new__(OpenProjectClient)
    client.custom_fields = MagicMock()
    client.custom_fields.ensure_list_field_options.return_value = {"fields": {}, "errors": {}}
    request = [{"id": 21, "labels": ["Blue"], "multi": False}]

    assert client.ensure_list_field_options(request) == {"fields": {}, "errors": {}}
    client.custom_fields.ensure_list_field_options.assert_called_once_with(request)


def test_a_same_named_field_of_another_format_is_refused() -> None:
    from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService

    service = OpenProjectCustomFieldService(_Client(json_result={"id": 300, "field_format": "list"}))

    with pytest.raises(
        ValueError, match=r"Custom field 300 named 'Sprint' has format 'list', not the requested 'text'"
    ):
        service.ensure_wp_custom_field_id("Sprint", "text")


def test_a_same_named_field_of_the_requested_format_is_returned() -> None:
    from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService

    service = OpenProjectCustomFieldService(_Client(json_result={"id": 300, "field_format": "text"}))

    assert service.ensure_wp_custom_field_id("Sprint", "text") == 300


def test_the_ensure_answer_is_read_per_field() -> None:
    from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService

    answer = {"created": ["Blue"], "multi_changed": True, "placeholder_removed": True}
    client = _Client(script_result={"status": "success", "data": {"fields": {"21": answer}, "errors": {"22": "no"}}})
    request = [{"id": 21, "labels": ["Blue"], "multi": True}, {"id": 22, "labels": [], "multi": False}]

    assert OpenProjectCustomFieldService(client).ensure_list_field_options(request) == {
        "fields": {21: answer},
        "errors": {22: "no"},
    }
    assert client.scripts[0][1] == {"fields": request}


@pytest.mark.parametrize(
    "script_result",
    [
        {"status": "error", "message": "JSON markers not found in Rails output"},
        {"status": "error", "data": {"fields": {}, "errors": {}}},
        {"status": "success", "data": {"fields": {}}},
        {"status": "success", "data": None},
        None,
    ],
)
def test_an_unconfirmed_ensure_answer_raises(script_result: Any) -> None:
    from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService

    service = OpenProjectCustomFieldService(_Client(script_result=script_result))

    with pytest.raises(QueryExecutionError, match="did not confirm the list field options"):
        service.ensure_list_field_options([{"id": 21, "labels": ["Blue"], "multi": False}])
