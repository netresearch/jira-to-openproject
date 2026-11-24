# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Verify the one retry of a Jira read that was refused with HTTP 401."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from jira.exceptions import JIRAError

from src.infrastructure.jira.jira_client import JiraApiError, JiraClient

URL = "https://test.invalid/search"


@pytest.fixture
def connect(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(time, "sleep", sleep)

    def build(replies):
        sent = []
        session = SimpleNamespace(cookies=requests.cookies.RequestsCookieJar())
        session.cookies.set("JSESSIONID", "stale")
        outcomes = iter(replies)

        def request(method, url, **kwargs):
            sent.append((method, url, kwargs, dict(session.cookies)))
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        session.request = request
        client = JiraClient.__new__(JiraClient)
        client.request_count = 0
        client.jira = SimpleNamespace(_session=session)
        client._patch_jira_client()
        return session, sent, sleep

    return build


def ok():
    response = requests.Response()
    response.status_code = 200
    return response


def refused(status=401):
    return JIRAError(text="refused", status_code=status)


def test_refused_read_is_repeated_without_the_session_cookies(connect):
    reply = ok()
    session, sent, sleep = connect([refused(), reply])

    assert session.request("get", URL, params={"startAt": 200}) is reply

    assert sent == [
        ("get", URL, {"params": {"startAt": 200}}, {"JSESSIONID": "stale"}),
        ("get", URL, {"params": {"startAt": 200}}, {}),
    ]
    sleep.assert_called_once_with(2.0)


def test_second_refusal_is_reported(connect):
    errors = [refused(), refused()]
    session, sent, _ = connect(errors)

    with pytest.raises(JiraApiError) as caught:
        session.request("GET", URL)

    assert caught.value.__cause__ is errors[1]
    assert len(sent) == 2


@pytest.mark.parametrize(("method", "status"), [("POST", 401), ("GET", 403), ("GET", 404)])
def test_other_refusals_are_not_repeated(connect, method, status):
    session, sent, sleep = connect([refused(status), ok()])

    with pytest.raises(JiraApiError):
        session.request(method, URL)

    assert len(sent) == 1
    sleep.assert_not_called()


def test_successful_read_keeps_the_session_cookies(connect):
    session, sent, sleep = connect([ok()])

    session.request("GET", URL)

    assert len(sent) == 1
    assert dict(session.cookies) == {"JSESSIONID": "stale"}
    sleep.assert_not_called()
