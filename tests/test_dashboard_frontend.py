# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Exercise the shipped dashboard in Chromium with deterministic API responses."""

import hashlib
import json
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "src/dashboard"


def dump_page(profile: Path, url: str) -> str:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        pytest.skip("Chromium is required for this browser regression test")
    options = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
    result = subprocess.run(
        [
            chrome,
            "--headless",
            *options,
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--virtual-time-budget=1000",
            "--dump-dom",
            f"--user-data-dir={profile}",
            url,
        ],
        check=True,
        capture_output=True,
        timeout=25,
        text=True,
    )
    return result.stdout.replace("&nbsp;", "\u00a0")


def test_published_values_and_system_memory_are_displayed(tmp_path: Path) -> None:
    assets = DASHBOARD
    progress = {
        "migration_id": "test-run",
        "status": "pending",
        "elapsed_seconds": 380,
        "phases": [{"name": "Work packages", "status": "completed"}],
        "verified_counts": {"Source issues": 12, "Rejected files": 0},
        "pending_checks": ["Verify comment permissions"],
        "memory_usage_mb": 48,
    }
    metrics = {"memory_usage_mb": 7301.453125, "cpu_usage_percent": None}
    responses = json.dumps({"/api/progress": progress, "/api/metrics": metrics})
    harness = (
        "<script>const responses=" + responses + ";"
        "window.fetch=async path=>({ok:true,json:async()=>responses[path]});"
        "window.WebSocket=class{};</script>"
    )
    shell = (assets / "templates/dashboard.html").read_text(encoding="utf-8")
    shell = shell.replace('"/static/', '"' + (assets / "static").as_uri() + "/")
    page = tmp_path / "dashboard.html"
    page.write_text(shell.replace("</head>", harness + "</head>"), encoding="utf-8")
    dom = dump_page(tmp_path / "profile", page.as_uri())
    assert '<dd id="system-ram">7.1\u00a0GiB</dd>' in dom
    assert '<dd id="pilot-ram">48\u00a0MiB</dd>' in dom
    assert '<dd id="system-cpu">—</dd>' in dom
    assert "<dt>Elapsed</dt><dd>6m 20s</dd>" in dom
    assert "<dt>Source issues</dt><dd>12</dd>" in dom
    assert "<dt>Rejected files</dt><dd>0</dd>" in dom
    assert "<li>Verify comment permissions</li>" in dom
    assert 'aria-valuetext="1 of 1 phases completed"' in dom


def test_template_asset_fingerprints_match_packaged_files() -> None:
    shell = (DASHBOARD / "templates/dashboard.html").read_text(encoding="utf-8")
    for name in ("css/dashboard.css", "js/dashboard.js"):
        digest = hashlib.sha256((DASHBOARD / "static" / name).read_bytes()).hexdigest()[:12]
        assert f'/static/{name}?v={digest}"' in shell


def test_upgrade_with_previous_dashboard_in_browser_cache(tmp_path: Path) -> None:
    upgraded = Event()
    old_requests = []
    legacy_html = (
        '<!doctype html><link rel="stylesheet" href="/static/css/dashboard.css">'
        '<script defer src="/static/js/dashboard.js"></script><body>Old dashboard</body>'
    )
    shell = (DASHBOARD / "templates/dashboard.html").read_text(encoding="utf-8")
    # Capture the applied stylesheet as well as the JavaScript result in the dumped DOM.
    css_probe = "<script>addEventListener('load',()=>document.body.dataset.currentStyles=Boolean(getComputedStyle(document.body).getPropertyValue('--accent').trim()))</script>"
    shell = shell.replace("</body>", css_probe + "</body>")
    legacy = {
        "/static/css/dashboard.css": ("text/css", "body{background:red}"),
        "/static/js/dashboard.js": ("text/javascript", "document.body.dataset.oldScript='yes'"),
    }
    assets = {
        name: (kind, (DASHBOARD / name.lstrip("/")).read_text(encoding="utf-8")) for name, (kind, _) in legacy.items()
    }
    api = {
        "/api/progress": {"status": "pending", "migration_id": "upgrade-run", "verified_counts": {"Imported rows": 23}},
        "/api/metrics": {"memory_usage_mb": 7301.453125},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            cache = "no-store"
            if path == "/":
                kind, content = "text/html", shell if upgraded.is_set() else legacy_html
            elif path in api:
                kind, content = "application/json", json.dumps(api[path])
            elif path in assets:
                cache = "public, max-age=86400, immutable"
                if self.path in legacy:
                    old_requests.append(path)
                    kind, content = assets[path] if upgraded.is_set() else legacy[path]
                else:
                    kind, content = assets[path]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode())

        def log_message(self, *_args) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            profile = tmp_path / "profile"
            assert 'data-old-script="yes"' in dump_page(profile, url)
            upgraded.set()
            refreshed = dump_page(profile, url)
        finally:
            server.shutdown()
            worker.join()
    assert sorted(old_requests) == sorted(legacy)
    assert 'data-current-styles="true"' in refreshed
    assert "<dt>Imported rows</dt><dd>23</dd>" in refreshed
    assert '<dd id="system-ram">7.1\u00a0GiB</dd>' in refreshed


def test_failed_initial_refresh_keeps_snapshot_and_warning_visible(tmp_path: Path) -> None:
    shell = (DASHBOARD / "templates/dashboard.html").read_text(encoding="utf-8")
    shell = shell.replace('"/static/', '"' + (DASHBOARD / "static").as_uri() + "/")
    summary = '<section id="published-run-summary">Latest published run</section>'
    shell = shell.replace('{% include "published_run.html" %}', summary)
    harness = """<script>
        window.fetch = async () => { throw new Error('Test connection unavailable'); };
        window.WebSocket = class {};
        addEventListener('load', () => setTimeout(() => {
            const summary = document.getElementById('published-run-summary');
            const warning = document.getElementById('load-error');
            document.body.dataset.snapshotVisible = String(summary.checkVisibility());
            document.body.dataset.warningVisible = String(warning.checkVisibility());
            const brand = document.querySelector('.brand');
            brand.focus({focusVisible: true});
            document.body.dataset.focusOutline = getComputedStyle(brand).outlineColor;
        }, 20));
    </script>"""
    page = tmp_path / "dashboard.html"
    page.write_text(shell.replace("</head>", harness + "</head>"), encoding="utf-8")
    dom = dump_page(tmp_path / "profile", page.as_uri())
    assert 'data-snapshot-visible="true"' in dom
    assert 'data-warning-visible="true"' in dom
    assert 'data-focus-outline="rgb(255, 255, 255)"' in dom
    assert "Migration data could not be refreshed." in dom
