# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Anonymous Jira proofs trust the same private CA as the migration's Jira session."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import HTTPAdapter

from src.application.components.reporting_migration import ReportingMigration
from src.infrastructure.jira.comment_audience import select_comments

BASE = "https://jira.example.invalid"


@pytest.fixture
def transport(monkeypatch):
    """Record the verify value requests hands to the transport and answer anonymously."""
    seen: list[object] = []
    bodies: list[dict] = []

    def send(self, request, **kwargs):
        seen.append(kwargs["verify"])
        response = requests.Response()
        response.status_code = 200
        response.headers.update({"Content-Type": "application/json", "X-AUSERNAME": "anonymous"})
        response._content = json.dumps(bodies.pop(0)).encode()
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return seen, bodies


def _jira(*, verify: bool = True) -> SimpleNamespace:
    session = requests.Session()
    session.verify = verify
    return SimpleNamespace(base_url=BASE, jira=SimpleNamespace(_session=session))


def test_public_report_uses_the_jira_ca_bundle(monkeypatch, tmp_path, transport) -> None:
    seen, bodies = transport
    bundle = str(tmp_path / "private-ca.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", bundle)
    migration = ReportingMigration.__new__(ReportingMigration)
    migration.jira_client = _jira()
    bodies.append({"id": "28", "name": "Shared"})

    public, held = migration._public_records("filter", [{"id": "28"}])

    assert seen == [bundle]
    assert [row["id"] for row in public] == ["28"]
    assert held == []


def test_comment_proof_uses_the_jira_ca_bundle(monkeypatch, tmp_path, transport) -> None:
    seen, bodies = transport
    bundle = str(tmp_path / "private-ca.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", bundle)
    monkeypatch.setenv("J2O_COPY_PUBLIC_COMMENTS", "1")
    row = {"id": "1", "body": "public", "author": {"name": "writer"}}
    client = _jira()
    privileged = requests.Response()
    privileged.status_code = 200
    privileged._content = json.dumps({"startAt": 0, "comments": [row], "total": 1}).encode()
    client._make_request = lambda _path, params: privileged
    bodies.append({"startAt": 0, "comments": [row], "total": 1})

    selection = select_comments(client, "DEMO-1")

    assert seen == [bundle]
    assert [comment.id for comment in selection.comments] == ["1"]


def test_disabled_jira_verification_never_reaches_the_anonymous_proof(monkeypatch, transport) -> None:
    seen, bodies = transport
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    migration = ReportingMigration.__new__(ReportingMigration)
    migration.jira_client = _jira(verify=False)
    bodies.append({"id": "28"})

    migration._public_records("filter", [{"id": "28"}])

    assert seen == [True]
