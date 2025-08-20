#!/usr/bin/env python3
"""Start a local tmux session connected to the OpenProject Rails console.

This mirrors the commonly used manual command, but reads configuration from
.env via python-dotenv:

  tmux new-session -s rails_console \; \
    pipe-pane -o 'cat >>~/tmux.log' \; \
    send-keys 'ssh -t <server> "docker exec -e IRBRC=/app/.irbrc \
      -e RELINE_OUTPUT_ESCAPES=false -e RELINE_INPUTRC=/dev/null -ti <container> \
      bundle exec rails console"' C-m

Usage:
  python scripts/start_rails_tmux.py [--attach]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", name], capture_output=True, text=True
    )
    return result.returncode == 0


def start_tmux_session(
    session: str,
    ssh_host: str,
    ssh_user: str | None,
    container: str,
    log_path: Path,
) -> None:
    # 1) Create session detached
    run(["tmux", "new-session", "-d", "-s", session])

    # 2) Pipe pane to log
    run(["tmux", "pipe-pane", "-o", "-t", session, f"cat >>{log_path.as_posix()}"])

    # 3) Build SSH + docker exec command
    ssh_target = ssh_host if not ssh_user else f"{ssh_user}@{ssh_host}"
    inner = (
        f"docker exec -e IRBRC=/app/.irbrc "
        f"-e RELINE_OUTPUT_ESCAPES=false -e RELINE_INPUTRC=/dev/null "
        f"-ti {container} bundle exec rails console"
    )
    ssh_cmd = f"ssh -t {ssh_target} \"{inner}\""

    # 4) Send command and press Enter
    run(["tmux", "send-keys", "-t", session, ssh_cmd, "C-m"])


def main() -> int:
    if shutil.which("tmux") is None:
        print("tmux is required on your host to run this script.", file=sys.stderr)
        return 1

    load_dotenv()

    session = os.environ.get("J2O_OPENPROJECT_TMUX_SESSION_NAME", "rails_console")
    server = os.environ.get("J2O_OPENPROJECT_SERVER")
    user = os.environ.get("J2O_OPENPROJECT_USER")
    container = os.environ.get("J2O_OPENPROJECT_CONTAINER")

    if not server or not container:
        print(
            "Missing configuration: J2O_OPENPROJECT_SERVER and J2O_OPENPROJECT_CONTAINER are required.",
            file=sys.stderr,
        )
        return 1

    parser = argparse.ArgumentParser(description="Start tmux Rails console session")
    parser.add_argument("--attach", action="store_true", help="Attach after starting")
    parser.add_argument(
        "--log",
        default=str(Path.home() / "rails_console.tmux.log"),
        help="Path to capture tmux pane output (default: ~/rails_console.tmux.log)",
    )
    args = parser.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if session_exists(session):
        print(f"tmux session '{session}' already exists.")
    else:
        try:
            start_tmux_session(session, server, user, container, log_path)
            print(
                f"Started tmux session '{session}'. Logs: {log_path}. Attach with: tmux attach -t {session}"
            )
        except subprocess.CalledProcessError as e:
            print(f"Failed to start tmux session: {e}", file=sys.stderr)
            return 1

    if args.attach:
        try:
            run(["tmux", "attach", "-t", session])
        except subprocess.CalledProcessError as e:
            print(f"Failed to attach to tmux session: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())


