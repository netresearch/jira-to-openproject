# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The pilot publishes complete, private snapshots that every dashboard transport can read."""

import csv
import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.dashboard.app import app
from src.dashboard.progress import write_progress


@pytest.fixture
def progress_path(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    monkeypatch.setenv("J2O_DASHBOARD_PROGRESS_FILE", str(path))
    return path


def publication(status):
    start = "2026-11-18T10:00:00Z"
    updated = "2026-11-18T10:00:12Z"
    return {
        "migration_id": "test-run-17",
        "status": status,
        "start_time": start,
        "last_update": updated,
        "finished_at": updated if status == "pending" else None,
        "current_component": "Review",
        "verified_counts": {"Checked records": 24},
        "phases": [{"name": "Read", "status": "completed", "started_at": start, "finished_at": updated}],
        "pending_checks": ["Review target membership"],
        "memory_usage_mb": 12.5,
    }


def test_atomic_failure_preserves_last_valid_private_file(progress_path, monkeypatch):
    write_progress(publication("running"))
    initial = progress_path.read_bytes()
    if os.name == "posix":
        assert progress_path.stat().st_mode & 0o777 == 0o600

    def fail_write(*_args, **_kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(Path, "replace", fail_write)
    next_publication = publication("pending")
    with pytest.raises(OSError, match="publication failure"):
        write_progress(next_publication)
    assert progress_path.read_bytes() == initial
    assert list(progress_path.parent.iterdir()) == [progress_path]


def test_validates_before_publication_and_is_opt_in(progress_path, monkeypatch):
    initial = publication("running")
    write_progress(initial)
    with pytest.raises(ValidationError):
        write_progress({**initial, "source_payload": "not public"})
    with pytest.raises(ValidationError):
        write_progress({**initial, "verified_counts": {"Rows": True}})
    with pytest.raises(ValidationError):
        write_progress({**initial, "start_time": "2026-11-20T01:00:00Z"})
    assert "not public" not in progress_path.read_text()
    monkeypatch.delenv("J2O_DASHBOARD_PROGRESS_FILE")
    write_progress({"should_not_be_used": True})


def test_http_metrics_and_csv_share_persistent_progress(progress_path):
    with TestClient(app) as client:
        idle = client.get("/api/progress").json()
        assert idle["status"] == "idle"
        assert idle["processed_entities"] is None
        write_progress(publication("pending"))
    # The previous lifetime finished and the producer has exited.
    with TestClient(app) as client:
        progress = client.get("/api/progress").json()
        assert progress["status"] == "pending"
        assert progress["elapsed_seconds"] == 12
        assert progress["verified_counts"] == {"Checked records": 24}
        assert progress["total_entities"] is None
        assert progress["success_rate"] is None
        assert progress["memory_usage_mb"] == 12.5
        status = client.get("/api/migration/status").json()
        assert status["is_running"] is False
        assert status["pending_checks"] == progress["pending_checks"]
        metrics = client.get("/api/metrics").json()
        assert metrics["migration_id"] == progress["migration_id"]
        assert metrics["entities_per_second"] is None
        assert metrics["pilot_memory_usage_mb"] == progress["memory_usage_mb"]
        assert metrics["memory_usage_mb"] > 0
        rows = list(csv.DictReader(io.StringIO(client.get("/api/metrics/csv").json()["csv_content"])))
        measures = {row["metric"]: row["value"] for row in rows}
        assert measures["migration_id"] == progress["migration_id"]
        assert measures["status"] == "pending"
        assert measures["verified_counts.Checked records"] == "24"
        assert measures["pilot_memory_usage_mb"] == "12.5"
        assert measures["phase.Read"] == "completed"
        assert measures["pending_check"] == "Review target membership"
        assert "entities_per_second" not in measures
        assert client.get("/api/progress?migration_id=unrelated").status_code == 404
        assert client.post("/api/migration/start", json={"action": "start"}).status_code == 409


@pytest.mark.parametrize("route", ["/ws/dashboard", "/ws/progress"])
def test_websocket_replays_current_data_and_delivers_external_terminal_write(progress_path, route):
    write_progress(publication("running"))
    with TestClient(app) as client, client.websocket_connect(route) as ws:
        while True:
            message = ws.receive_json()
            if message["type"] == "progress_update":
                assert message["data"] == client.get("/api/progress").json()
                break
        ws.send_json({"type": "request_status"})
        while True:
            message = ws.receive_json()
            if message["type"] == "status_update":
                assert message["data"] == client.get("/api/migration/status").json()
                break
        write_progress(publication("pending"))
        while True:
            message = ws.receive_json()
            if message["type"] == "progress_update" and message["data"]["status"] == "pending":
                assert message["data"] == client.get("/api/progress").json()
                break


def test_broken_publication_is_reported_without_echoing_its_contents(progress_path):
    progress_path.write_text('{"unused-secret":"fixture",')
    with TestClient(app) as client:
        for url in ("/api/progress", "/api/migration/status", "/api/metrics", "/api/metrics/csv"):
            result = client.get(url)
            assert result.status_code == 503
            assert "fixture" not in result.text


def test_root_renders_latest_publication_as_escaped_html_before_javascript(progress_path):
    snapshot = publication("pending")
    snapshot["current_component"] = None
    snapshot["phases"][0]["name"] = "Read <closed> & verified"
    snapshot["verified_counts"] = {"Verified <issues>": 1122, "Rejected": 0}
    snapshot["pending_checks"] = ["Check <mark>result</mark>"]
    write_progress(snapshot)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        summary = response.text.split('id="published-run-summary"', 1)[1].split("</section>", 1)[0]
        assert "<dd>Pending</dd>" in summary
        assert "<dd>Read &lt;closed&gt; &amp; verified</dd>" in summary
        assert "<dt>Verified &lt;issues&gt;</dt><dd>1122</dd>" in summary
        assert "<dt>Rejected</dt><dd>0</dd>" in summary
        assert "Check &lt;mark&gt;result&lt;/mark&gt;" in summary
        assert 'id="live-dashboard" hidden' in response.text
        assert "<script" not in summary
        for url in ("/api/progress", "/api/metrics", "/api/migration/status", "/api/metrics/csv"):
            assert client.get(url).headers["cache-control"] == "no-store"
        write_progress({**snapshot, "verified_counts": {"Verified issues": 1123}})
        refreshed = client.get("/", headers={"If-None-Match": "obsolete-dashboard"})
        assert refreshed.status_code == 200
        assert "<dt>Verified issues</dt><dd>1123</dd>" in refreshed.text


def test_root_shows_unavailable_snapshot_without_echoing_invalid_data(progress_path):
    progress_path.write_text('{"raw":"private-source-content",')
    with TestClient(app) as client:
        result = client.get("/")
        assert result.status_code == 503
        assert "The latest migration progress is unavailable." in result.text
        assert result.headers["cache-control"] == "no-store"
        assert "private-source-content" not in result.text


def test_published_api_errors_are_in_the_openapi_contract():
    paths = app.openapi()["paths"]
    for route in ("/api/progress", "/api/metrics", "/api/metrics/csv"):
        assert {"404", "503"} <= paths[route]["get"]["responses"].keys()
    for route in ("/", "/api/migration/status"):
        assert "503" in paths[route]["get"]["responses"]
    for action in ("start", "stop", "pause", "resume"):
        assert "409" in paths[f"/api/migration/{action}"]["post"]["responses"]
