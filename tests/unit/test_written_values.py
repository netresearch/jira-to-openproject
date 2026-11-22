# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""J2O writes a value again only while the target holds what J2O wrote last (ADR-013)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import config
from src.application.components import written_values
from src.application.components.written_values import WrittenValues

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _answer(row, status, digest=None):
    answer = {"id": row["id"], "status": status, "attr": row["_j2o_guard"]["attr"]}
    return answer | ({"digest": digest} if digest else {})


def test_a_recorded_digest_is_what_rails_must_find(tmp_path):
    store = WrittenValues(tmp_path / "w.sqlite")
    store.record([(5, "description", DIGEST_A)])
    rows = written_values.guarded(
        [{"id": 5, "description": "new"}, {"id": 6, "customField9": "x"}], store, overwrite=False
    )
    assert rows == [
        {"id": 5, "description": "new", "_j2o_guard": {"attr": "description", "expect": DIGEST_A, "overwrite": False}},
        {"id": 6, "customField9": "x", "_j2o_guard": {"attr": "customField9", "expect": None, "overwrite": False}},
    ]


def test_a_row_with_two_attributes_is_refused(tmp_path):
    store = WrittenValues(tmp_path / "w.sqlite")
    with pytest.raises(ValueError, match="exactly one attribute"):
        written_values.guarded([{"id": 5, "description": "x", "subject": "y"}], store, overwrite=False)


def test_written_and_unchanged_rows_record_their_digest_and_changed_rows_keep_theirs(tmp_path):
    store = WrittenValues(tmp_path / "w.sqlite")
    store.record([(7, "description", DIGEST_A)])
    rows = written_values.guarded(
        [{"id": 5, "description": "a"}, {"id": 6, "description": "b"}, {"id": 7, "description": "c"}],
        store,
        overwrite=False,
    )
    result = {
        "updated": 1,
        "results": [
            _answer(rows[0], "updated", DIGEST_B),
            _answer(rows[1], "unchanged", DIGEST_A),
            _answer(rows[2], "target_changed"),
        ],
    }
    outcome = written_values.settle(store, rows, result)
    assert (outcome.updated, outcome.unchanged, outcome.changed) == (1, 1, ["7:description"])
    assert outcome.acknowledged == 3
    keys = [(5, "description"), (6, "description"), (7, "description")]
    assert store.expected(keys) == {
        (5, "description"): DIGEST_B,
        (6, "description"): DIGEST_A,
        (7, "description"): DIGEST_A,
    }


def test_a_failed_row_is_not_acknowledged_and_records_nothing(tmp_path):
    store = WrittenValues(tmp_path / "w.sqlite")
    rows = written_values.guarded([{"id": 5, "description": "a"}], store, overwrite=False)
    outcome = written_values.settle(store, rows, {"updated": 0, "results": [{"id": 5, "status": "failed"}]})
    assert outcome.acknowledged == 0
    assert store.expected([(5, "description")]) == {}


@pytest.mark.parametrize(
    "result",
    [
        {"updated": 1, "results": [{"id": 5, "status": "updated", "attr": "description"}]},
        {"updated": 1, "results": [{"id": 9, "status": "updated", "attr": "description", "digest": DIGEST_A}]},
        {"updated": 2, "results": [{"id": 5, "status": "updated", "attr": "description", "digest": DIGEST_A}]},
    ],
    ids=["no-digest", "unsent-row", "counts-disagree"],
)
def test_an_answer_that_does_not_fit_the_rows_is_refused(tmp_path, result):
    store = WrittenValues(tmp_path / "w.sqlite")
    rows = written_values.guarded([{"id": 5, "description": "a"}], store, overwrite=False)
    with pytest.raises(ValueError, match="Rails"):
        written_values.settle(store, rows, result)
    assert store.expected([(5, "description")]) == {}


def test_the_store_survives_a_new_process(tmp_path):
    WrittenValues(tmp_path / "w.sqlite").record([(5, "description", DIGEST_A)])
    assert WrittenValues(tmp_path / "w.sqlite").expected([(5, "description")]) == {(5, "description"): DIGEST_A}


@pytest.mark.parametrize(("value", "allowed"), [(True, True), (None, False), ("true", False), (1, False)])
def test_only_an_explicit_true_allows_overwriting(monkeypatch, value, allowed):
    monkeypatch.setattr(config, "migration_config", {written_values.SETTING: value})
    assert written_values.overwrite_allowed() is allowed


def _content(tmp_path, monkeypatch, answer):
    from src.application.components.work_package_content_migration import WorkPackageContentMigration

    monkeypatch.setattr(config, "migration_config", {"batch_size": 10})
    mappings = MagicMock()
    mappings.get_mapping.return_value = {}
    monkeypatch.setattr(config, "mappings", mappings, raising=False)
    mig = WorkPackageContentMigration(jira_client=MagicMock(), op_client=MagicMock())
    mig.op_client.batch_update_work_packages.side_effect = lambda rows: {
        "updated": sum(answer(row) == "updated" for row in rows),
        "results": [_answer(row, answer(row), None if answer(row) == "target_changed" else DIGEST_B) for row in rows],
    }
    return mig


def test_a_rerun_sends_the_digest_of_the_first_run(tmp_path, monkeypatch):
    mig = _content(tmp_path, monkeypatch, lambda row: "updated")
    item = {"wp_id": 5, "description_update": "Text", "custom_field_updates": {"customField9": "x"}}
    for _ in range(2):
        mig._write_work_package_fields([item], (results := {}))
    calls = [call.args[0] for call in mig.op_client.batch_update_work_packages.call_args_list]
    assert [row["_j2o_guard"]["expect"] for call in calls[:2] for row in call] == [None, None]
    assert [row["_j2o_guard"]["expect"] for call in calls[2:] for row in call] == [DIGEST_B, DIGEST_B]
    assert results["descriptions_updated"] == results["custom_fields_updated"] == 1


def test_a_changed_target_is_reported_and_not_counted_as_failure(tmp_path, monkeypatch):
    mig = _content(tmp_path, monkeypatch, lambda row: "target_changed" if "description" in row else "unchanged")
    item = {"wp_id": 5, "description_update": "Text", "custom_field_updates": {"customField9": "x"}}
    mig._write_work_package_fields([item], (results := {}))
    assert results["descriptions_target_changed"] == 1
    assert results["descriptions_failed"] == results["custom_fields_failed"] == 0
    assert results["custom_fields_unchanged"] == 1
    assert results["target_changed"] == ["5:description"]
    assert written_values.store().expected([(5, "description"), (5, "customField9")]) == {(5, "customField9"): DIGEST_B}


def test_the_operator_flag_reaches_every_row(tmp_path, monkeypatch):
    mig = _content(tmp_path, monkeypatch, lambda row: "updated")
    monkeypatch.setattr(config, "migration_config", {written_values.SETTING: True})
    item = {"wp_id": 5, "description_update": "Text", "custom_field_updates": {"customField9": "x"}}
    mig._write_work_package_fields([item], {})
    rows = [row for call in mig.op_client.batch_update_work_packages.call_args_list for row in call.args[0]]
    assert [row["_j2o_guard"]["overwrite"] for row in rows] == [True, True]


def test_inline_refs_send_the_recorded_digest_and_record_the_new_one(monkeypatch):
    from src.application.components.inline_refs_migration import InlineRefsMigration

    written_values.store().record([(5, "description", DIGEST_A)])
    op = MagicMock()
    op.execute_script_with_data.return_value = {
        "status": "success",
        "data": {"updated": 1, "failed": 0, "digests": {"5": DIGEST_B}},
    }
    mig = InlineRefsMigration(jira_client=MagicMock(), op_client=op)
    result = mig._load(SimpleNamespace(data={"work_package_ids": [5, 6]}))
    assert result.success
    assert op.execute_script_with_data.call_args.args[1] == [{"id": 5, "expect": DIGEST_A}, {"id": 6, "expect": None}]
    assert written_values.store().expected([(5, "description"), (6, "description")]) == {(5, "description"): DIGEST_B}


def test_tasklists_send_the_recorded_digest_and_record_the_new_one(monkeypatch):
    from src.application.components import simpletasks_migration
    from src.application.components.simpletasks_migration import SimpleTasksMigration

    written_values.store().record([(5, "description", DIGEST_A)])
    sent = []

    def apply(client, project):
        sent.append(project["rows"])
        return {"updated": 1, "tasks": 2, "digests": {"5": DIGEST_B}}

    monkeypatch.setattr(simpletasks_migration, "apply_tasklists", apply)
    mig = SimpleTasksMigration.__new__(SimpleTasksMigration)
    mig.op_client = MagicMock()
    rows = [{"id": "1", "wp": 5}, {"id": "2", "wp": 6}]
    mig._load(
        SimpleNamespace(
            data=[{"project": 1, "rows": rows}], details={"unverified_task_audience": 0, "unconfirmed_issues": 0}
        )
    )
    assert sent == [[{"id": "1", "wp": 5, "expect": DIGEST_A}, {"id": "2", "wp": 6, "expect": None}]]
    assert written_values.store().expected([(5, "description")]) == {(5, "description"): DIGEST_B}


@pytest.mark.parametrize(
    "answer",
    [{"status": "error", "message": "JSON markers not found in Rails output"}, {"updated": 1, "failed": 0}],
    ids=["no-markers", "unwrapped"],
)
def test_an_unconfirmed_inline_reference_rewrite_fails(answer):
    from src.application.components.inline_refs_migration import InlineRefsMigration

    op = MagicMock()
    op.execute_script_with_data.return_value = answer
    result = InlineRefsMigration(jira_client=MagicMock(), op_client=op)._load(
        SimpleNamespace(data={"work_package_ids": [5, 6]})
    )
    assert (result.success, result.failed) == (False, 2)
