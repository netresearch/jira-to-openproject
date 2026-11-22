# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Completion waiting and error scoping of RailsConsoleClient.execute.

The pane frames follow a capture taken from an OpenProject 17.9.7 Rails console
started with contrib/openproject.irbrc (SIMPLE prompt: ``>>`` and ``?>``). IRB
prints the start marker before it echoes the ``begin`` block, so the echoed
wrapper (which contains the literals ``Ruby error:`` and ``--EXEC_END--<id>``)
sits between the start marker and the command's own output.
"""

from __future__ import annotations

import subprocess
import time as real_time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.infrastructure.openproject import rails_console_client as rcc
from src.infrastructure.openproject.rails_console_client import (
    CommandExecutionError,
    RailsConsoleClient,
    RubyError,
)

MARKER_ID = "20261120_011442_152258_54a4"
EARLIER_ID = "20261120_011441_942756_5be2"
SCREEN_LINES = 50
IRB_FULL_MESSAGE = "/app/vendor/bundle/ruby/4.0.0/gems/irb-1.18.0/lib/irb.rb:437:in 'full_message': "


def _echo(marker_id: str, expression: str) -> list[str]:
    """Lines IRB echoes for one execute() call, as captured from the live console."""
    return [
        "=> nil",
        ">> # j2o: rails/console ts=2026-11-20T01:14:42Z pid=2068558",
        "=> nil",
        ">>",
        f'>>         puts "--EXEC_START--{marker_id}"',
        f"--EXEC_START--{marker_id}",
        "=> nil",
        ">>         begin",
        "?>             result = nil  # Initialize result variable",
        f"?>             result = {expression}  # Assign the actual result",
        "?>             puts result.inspect",
        "?>           rescue => e",
        f'?>             begin; puts "--EXEC_ERROR--{marker_id}"; rescue; end',
        '?>             begin; puts "Ruby error: #{e.class}: #{e.message}"; rescue; end',
        "?>             begin",
        "?>                 bt = e.backtrace || []",
        '?>                 puts bt.join("\\\\n")[0..2000]',
        "?>               rescue",
        "?>               end",
        "?>           ensure",
        f'?>             begin; puts "--EXEC_END--{marker_id}"; rescue; end',
        f"?>           end # --SCRIPT_END--{marker_id}",
    ]


def _history(*, earlier_error: bool = False, earlier_fatal: bool = False) -> list[str]:
    """Scrollback longer than every capture window, optionally with an earlier command's failure."""
    lines = [str(i) for i in range(1, 601)]
    if earlier_error:
        lines += [
            *_echo(EARLIER_ID, '(puts "Ruby error: x"; 7)'),
            "Ruby error: x",
            "7",
            f"--EXEC_END--{EARLIER_ID}",
        ]
    if earlier_fatal:
        lines += [
            f"{IRB_FULL_MESSAGE}(open-project):136: syntax errors found (SyntaxError)",
            "/app/vendor/bundle/ruby/4.0.0/gems/irb-1.18.0/lib/irb.rb:1:in 'IRB::Irb#run'",
        ]
        lines += [f"filler {i}" for i in range(80)]
    return [*lines, "=> nil", ">>"]


class FakeClock:
    """Replaces the client's ``time`` module; sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.01)

    @staticmethod
    def strftime(fmt: str, *args: object) -> str:
        return real_time.strftime(fmt, *args)  # type: ignore[arg-type]


class FakePane:
    """tmux stand-in: frames appear at fixed times after the command is sent."""

    def __init__(self, clock: FakeClock, before: list[str], frames: list[tuple[float, list[str]]]) -> None:
        self.clock = clock
        self.before = before
        self.frames = frames
        self.sent_at: float | None = None
        self.captures = 0

    def lines(self) -> list[str]:
        if self.sent_at is None:
            return self.before
        elapsed = self.clock.now - self.sent_at
        current = self.before
        for appears_at, frame in self.frames:
            if elapsed >= appears_at:
                current = frame
        return current

    def run(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "capture-pane":
            self.captures += 1
            history = int(cmd[cmd.index("-S") + 1].lstrip("-"))
            window = self.lines()[-(history + SCREEN_LINES) :]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(window) + "\n", "")
        if cmd[1] == "display-message":
            # The client resolves its pane once (RailsConsoleClient._get_target).
            return subprocess.CompletedProcess(cmd, 0, "%0\n", "")
        if cmd[1] == "send-keys" and self.sent_at is None and any("--EXEC_START--" in part for part in cmd):
            self.sent_at = self.clock.now
        return subprocess.CompletedProcess(cmd, 0, "", "")


class FakeFiles:
    def __init__(self, root: Path) -> None:
        self.root = root

    def generate_unique_id(self) -> str:
        return MARKER_ID

    def create_debug_session(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def add_to_debug_log(self, _session_dir: Path, _message: str) -> None:
        return None

    @staticmethod
    def join(*parts: Path | str) -> Path:
        return Path(*parts)


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    before: list[str],
    frames: list[tuple[float, list[str]]],
) -> tuple[RailsConsoleClient, FakePane, FakeClock]:
    clock = FakeClock()
    pane = FakePane(clock, before, frames)
    fake_subprocess = SimpleNamespace(
        run=pane.run,
        SubprocessError=subprocess.SubprocessError,
        CalledProcessError=subprocess.CalledProcessError,
    )
    monkeypatch.setattr(rcc, "subprocess", fake_subprocess)
    monkeypatch.setattr(rcc, "time", clock)
    monkeypatch.setattr(rcc, "FileManager", lambda: FakeFiles(tmp_path))
    monkeypatch.delenv("J2O_FORCE_RAILS_RUNNER", raising=False)
    return RailsConsoleClient(tmux_session_name="j2o-test"), pane, clock


def _running(before: list[str], expression: str) -> list[str]:
    return [*before[:-1], *_echo(MARKER_ID, expression), ""]


def _finished(running: list[str], *output: str) -> list[str]:
    return [*running[:-1], *output, f"--EXEC_END--{MARKER_ID}", "=> nil", ">>"]


def test_waits_for_end_marker_of_slow_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _history()
    running = _running(before, "(sleep 3; 42)")
    client, _pane, clock = _client(monkeypatch, tmp_path, before, [(0.0, running), (3.0, _finished(running, "42"))])
    started = clock.now

    assert client.execute("(sleep 3; 42)", timeout=30) == "42"
    assert clock.now - started >= 3.0


def test_echoed_end_marker_is_not_completion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _history()
    client, _pane, clock = _client(monkeypatch, tmp_path, before, [(0.0, _running(before, "(sleep 60; 42)"))])
    started = clock.now

    with pytest.raises(CommandExecutionError) as caught:
        client.execute("(sleep 60; 42)", timeout=5)

    assert not isinstance(caught.value, RubyError)
    assert f"--EXEC_END--{MARKER_ID}" in str(caught.value)
    assert clock.now - started >= 5


def test_earlier_ruby_error_does_not_fail_current_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _history(earlier_error=True)
    running = _running(before, "(sleep 3; 42)")
    client, _pane, _clock = _client(monkeypatch, tmp_path, before, [(0.0, running), (3.0, _finished(running, "42"))])

    assert client.execute("(sleep 3; 42)", timeout=30) == "42"


def test_earlier_fatal_output_does_not_fail_current_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _history(earlier_fatal=True)
    running = _running(before, "(sleep 3; 42)")
    client, _pane, _clock = _client(monkeypatch, tmp_path, before, [(0.0, running), (3.0, _finished(running, "42"))])

    assert client.execute("(sleep 3; 42)", timeout=30) == "42"


def test_error_of_current_command_raises_with_its_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _history(earlier_error=True)
    expression = '(sleep 2; raise "boom-current")'
    running = _running(before, expression)
    failed = _finished(
        running,
        f"--EXEC_ERROR--{MARKER_ID}",
        "Ruby error: RuntimeError: boom-current",
        "(open-project):117:in '<top (required)>'",
    )
    client, _pane, clock = _client(monkeypatch, tmp_path, before, [(0.0, running), (2.0, failed)])
    started = clock.now

    with pytest.raises(RubyError) as caught:
        client.execute(expression, timeout=30)

    assert "Ruby error: RuntimeError: boom-current" in str(caught.value)
    assert clock.now - started >= 2.0


def test_stack_overflow_of_current_command_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # SystemStackError is not a StandardError: the wrapper's ensure prints the end marker, then IRB the error.
    before = _history(earlier_error=True)
    running = _running(before, "deep_recursion")
    crashed = [
        *running[:-1],
        f"--EXEC_END--{MARKER_ID}",
        "(open-project):3:in 'deep_recursion': stack level too deep (SystemStackError)",
        ">>",
    ]
    client, _pane, _clock = _client(monkeypatch, tmp_path, before, [(0.0, running), (1.0, crashed)])

    with pytest.raises(RubyError) as caught:
        client.execute("deep_recursion", timeout=60)

    assert "stack level too deep" in str(caught.value)
    assert "Ruby error: x" not in str(caught.value)


def test_backtrace_text_in_running_output_does_not_end_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _history()
    running = _running(before, "(log_rescued_error; sleep 3; 42)")
    logged = [
        *running[:-1],
        "WARN rescued: stack level too deep (SystemStackError)",
        "/app/vendor/bundle/ruby/4.0.0/gems/irb-1.18.0/lib/irb.rb:192:in 'IRB::Irb#run'",
        "",
    ]
    client, _pane, clock = _client(
        monkeypatch, tmp_path, before, [(0.0, running), (1.0, logged), (3.0, _finished(logged, "42"))]
    )
    started = clock.now

    assert client.execute("(log_rescued_error; sleep 3; 42)", timeout=30).endswith("42")
    assert clock.now - started >= 3.0


def test_missing_end_marker_reads_only_own_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _history(earlier_error=True)
    output = "\n".join([*_running(before, "6 * 7")[:-1], "partial", ">>"])
    client, _pane, _clock = _client(monkeypatch, tmp_path, before, [])

    result = client._command_result(
        output,
        f"--EXEC_START--{MARKER_ID}",
        f"--EXEC_END--{MARKER_ID}",
        f"--EXEC_ERROR--{MARKER_ID}",
        f"--SCRIPT_END--{MARKER_ID}",
    )

    assert result.startswith("partial")


def test_syntax_error_reports_syntax_error_not_stale_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # IRB rejects the begin block and then runs the remaining wrapper lines one by one, so the
    # error and end markers are printed, with a "Ruby error:" line naming the previous `e`.
    before = _history(earlier_error=True)
    full_message = IRB_FULL_MESSAGE
    frame = [
        *before[:-1],
        *_echo(MARKER_ID, "(1 + )")[:11],
        f"{full_message}(open-project):288: syntax errors found (SyntaxError)",
        ">>         rescue => e",
        f"{full_message}(open-project):290: syntax errors found (SyntaxError)",
        f'>>           begin; puts "--EXEC_ERROR--{MARKER_ID}"; rescue; end',
        f"--EXEC_ERROR--{MARKER_ID}",
        "=> nil",
        '>>           begin; puts "Ruby error: #{e.class}: #{e.message}"; rescue; end',
        "Ruby error: RuntimeError: boom-previous",
        "=> nil",
        ">>         ensure",
        f"{full_message}(open-project):298: syntax error found (SyntaxError)",
        f'>>           begin; puts "--EXEC_END--{MARKER_ID}"; rescue; end',
        f"--EXEC_END--{MARKER_ID}",
        "=> nil",
        f">>         end # --SCRIPT_END--{MARKER_ID}",
        f"{full_message}(open-project):300: syntax error found (SyntaxError)",
        ">>",
    ]
    client, _pane, _clock = _client(monkeypatch, tmp_path, before, [(0.0, frame)])

    with pytest.raises(RubyError) as caught:
        client.execute("(1 + )", timeout=30)

    assert "syntax errors found (SyntaxError)" in str(caught.value)


def test_missing_start_marker_ignores_earlier_ruby_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = _history(earlier_error=True)
    frame = [*before[:-1], "42", f"--EXEC_END--{MARKER_ID}", "=> nil", ">>"]
    client, _pane, _clock = _client(monkeypatch, tmp_path, before, [(0.0, frame)])

    with pytest.raises(CommandExecutionError) as caught:
        client.execute("6 * 7", timeout=30)

    assert not isinstance(caught.value, RubyError)
    assert "Start marker" in str(caught.value)
