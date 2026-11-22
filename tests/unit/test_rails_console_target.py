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
    client.tmux_session_name, client.window, client.pane, client._pane_id = "j2o", window, pane, None
    client._tmux_path = "tmux"
    return client


@pytest.mark.parametrize(
    ("window", "pane", "target"),
    [(2, None, "j2o:2"), (2, 3, "j2o:2.3"), (None, 3, "j2o:.3"), (0, 0, "j2o:0.0")],
)
def test_an_explicit_window_or_pane_is_targeted_as_configured(window, pane, target):
    assert _client(window, pane)._get_target() == target


def test_the_default_target_is_the_pane_id_of_the_session_resolved_once(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="%7\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    client = _client()
    assert (client._get_target(), client._get_target()) == ("%7", "%7")
    # "<session>:" names the session, never a window or pane called like it; one lookup only.
    assert calls == [["tmux", "display-message", "-p", "-t", "j2o:", "#{pane_id}"]]


def test_an_answer_without_a_pane_id_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    )
    client = _client()
    with pytest.raises(subprocess.CalledProcessError):
        client._get_target()


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
        # A window named like the session in the same session must not catch the commands.
        subprocess.run([tmux, "new-window", "-d", "-t", "j2o-base1", "-n", "j2o-base1"], check=True)
        found = subprocess.run(
            [tmux, "display-message", "-p", "-t", client._get_target(), "#{window_index}.#{pane_index}"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert found.stdout.strip() == "1.1"
    finally:
        subprocess.run([tmux, "kill-server"], check=False)
