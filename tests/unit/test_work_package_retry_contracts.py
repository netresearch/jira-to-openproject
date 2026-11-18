# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT
"""Exercise page retries through the Jira wrapper and recover partial provenance lookups."""

import time
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import requests
from jira.exceptions import JIRAError

from src.application.components import work_package_migration as legacy
from src.domain import provenance_fields
from src.infrastructure.jira.jira_client import JiraApiError, JiraClient


def page_reader(monkeypatch, replies):
    app = legacy.WorkPackageMigration.__new__(legacy.WorkPackageMigration)
    client = JiraClient.__new__(JiraClient)
    client.request_count = 0
    session = SimpleNamespace(request=Mock(side_effect=replies))
    client.jira = SimpleNamespace(_session=session)
    client._patch_jira_client()

    def search(_jql, **params):
        session.request("GET", "https://test.invalid/search", params=params)
        return ["TEST-4"]

    client.jira.search_issues = Mock(side_effect=search)
    app.jira_client = client
    sleep = Mock()
    monkeypatch.setattr(time, "sleep", sleep)
    return app, sleep


def fetch_page(app):
    return app._fetch_issues_with_retry(
        jql="project=TEST", start_at=3, max_results=1, fields="key", expand=None, project_key="TEST"
    )


def test_sdk_rate_limit_retries_same_page_through_jira_wrapper(monkeypatch):
    ok = requests.Response()
    ok.status_code = 200
    app, sleep = page_reader(monkeypatch, [JIRAError(text="limited", status_code=429), ok])
    assert fetch_page(app) == ["TEST-4"]
    expected = call("project=TEST", startAt=3, maxResults=1, fields="key", expand=None, json_result=False)
    assert app.jira_client.jira.search_issues.call_args_list == [expected, expected]
    sleep.assert_called_once_with(1)


def test_mixed_transport_and_sdk_rate_limits_spend_one_retry_budget(monkeypatch):
    errors = [requests.Timeout(), JIRAError(text="limited", status_code=429)] * 3
    app, sleep = page_reader(monkeypatch, errors)
    with pytest.raises(JiraApiError) as caught:
        fetch_page(app)
    assert caught.value.__cause__ is errors[-1]
    assert app.jira_client.request_count == 6
    assert sleep.call_args_list == [call(delay) for delay in (1, 2, 4, 8, 16)]


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_other_sdk_statuses_propagate_without_retry(monkeypatch, status):
    error = JIRAError(text="HTTP Error 429 in upstream detail", status_code=status)
    app, sleep = page_reader(monkeypatch, [error])
    with pytest.raises(JiraApiError) as caught:
        fetch_page(app)
    assert caught.value.__cause__ is error
    assert app.jira_client.request_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("initial_result", [TimeoutError("unavailable"), {}, {"id": "bad"}, {"id": 0}])
def test_next_row_retries_only_missing_provenance_field(initial_result):
    app = legacy.WorkPackageMigration.__new__(legacy.WorkPackageMigration)
    app.logger = Mock()
    lookup = Mock(side_effect=[{"id": 7}, initial_result, {"id": "9"}, {"id": 8}])
    app.op_client = SimpleNamespace(ensure_custom_field=lookup)
    assert app._dict_provenance({"id": "001", "key": "TEST-1"}) == [
        {"id": 7, "value": "jira"},
        {"id": 9, "value": "TEST-1"},
    ]
    app.logger.warning.assert_called_once()
    assert provenance_fields.EXTERNAL_ID in app.logger.warning.call_args.args
    assert app._dict_provenance({"id": "002", "key": "TEST-2"}) == [
        {"id": 7, "value": "jira"},
        {"id": 8, "value": "002"},
        {"id": 9, "value": "TEST-2"},
    ]
    app._dict_provenance({"id": "003", "key": "TEST-3"})
    assert [entry.args[0] for entry in lookup.call_args_list] == [
        provenance_fields.ORIGIN_SYSTEM,
        provenance_fields.EXTERNAL_ID,
        provenance_fields.EXTERNAL_KEY,
        provenance_fields.EXTERNAL_ID,
    ]


def test_sdk_row_retries_missing_origin_field_only():
    app = legacy.WorkPackageMigration.__new__(legacy.WorkPackageMigration)
    app.logger = Mock()
    specs = provenance_fields.WORK_PACKAGE_FIELD_SPECS
    ids = {name: index for index, (name, _, _) in enumerate(specs, 1)}
    returns = [
        TimeoutError("unavailable") if name == provenance_fields.ORIGIN_ID else {"id": ids[name]} for name in ids
    ]
    lookup = Mock(side_effect=[*returns, {"id": ids[provenance_fields.ORIGIN_ID]}])
    app.op_client = SimpleNamespace(ensure_custom_field=lookup)
    app._ensure_provenance_field_ids()
    assert provenance_fields.ORIGIN_ID not in app._j2o_wp_cf_ids_full
    app._ensure_provenance_field_ids()
    assert app._j2o_wp_cf_ids_full == ids
    app._ensure_provenance_field_ids()
    assert lookup.call_count == len(specs) + 1
    assert lookup.call_args.args == (provenance_fields.ORIGIN_ID,)
