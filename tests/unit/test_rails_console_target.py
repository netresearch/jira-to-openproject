# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The Rails console client reaches its tmux pane also where tmux counts from 1."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from src.infrastructure.openproject.rails_console_client import RailsConsoleClient


def _client(window=None, pane=None) -> RailsConsoleClient:
    client = RailsConsoleClient.__new__(RailsConsoleClient)
    client.tmux_session_name, client.window, client.pane = "j2o", window, pane
    return client


@pytest.mark.parametrize(
    ("window", "pane", "target"),
    [(None, None, "j2o"), (2, None, "j2o:2"), (2, 3, "j2o:2.3"), (None, 3, "j2o:.3"), (0, 0, "j2o:0.0")],
)
def test_the_target_names_only_what_was_configured(window, pane, target):
    assert _client(window, pane)._get_target() == target


def test_the_default_target_exists_with_base_index_one(tmp_path, monkeypatch):
    # The pilot setup that failed: a tmux whose configuration counts windows and panes from 1.
    tmux = shutil.which("tmux")
    if not tmux:
        pytest.skip("tmux is required for this check")
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g base-index 1\nset -g pane-base-index 1\n")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    subprocess.run([tmux, "-f", str(conf), "new-session", "-d", "-s", "j2o-base1"], check=True)
    try:
        client = RailsConsoleClient(tmux_session_name="j2o-base1")
        found = subprocess.run(
            [tmux, "display-message", "-p", "-t", client._get_target(), "#{window_index}.#{pane_index}"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert found.stdout.strip() == "1.1"
    finally:
        subprocess.run([tmux, "kill-server"], check=False)
