# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""JiraClient.get_issue_remote_links reads the per-issue remotelink resource (#501)."""

from unittest.mock import MagicMock

import pytest

from src.infrastructure.jira.jira_client import JiraApiError, JiraClient


def _client(answer):
    client = JiraClient.__new__(JiraClient)
    client._make_request = MagicMock(return_value=MagicMock(json=MagicMock(return_value=answer)))
    return client


def test_the_resource_of_the_issue_is_read():
    client = _client([{"id": 1, "object": {"url": "https://a.example"}}])
    assert client.get_issue_remote_links("A B-1") == [{"id": 1, "object": {"url": "https://a.example"}}]
    assert client._make_request.call_args.args == ("/rest/api/2/issue/A%20B-1/remotelink",)


@pytest.mark.parametrize("answer", [{"errorMessages": []}, [1], None])
def test_a_malformed_answer_raises(answer):
    client = _client(answer)
    with pytest.raises(JiraApiError):
        client.get_issue_remote_links("APP-1")
