#!/usr/bin/env python3
"""Containerised rehearsal runner for Jira→OpenProject migrations.

This helper script can spin up the mock Jira/OpenProject stack via
``docker compose`` (test profile), run the specified migration
components sequentially (either locally or inside the test container),
and collect key artefacts under ``var/rehearsal/<timestamp>`` for later
inspection.

Usage examples
--------------

Run components locally against your existing dev stack:

.. code-block:: bash

    python scripts/run_rehearsal.py --components users groups

Run in the Docker test container against mock services, stop them when
done, and gather artefacts:

.. code-block:: bash

    python scripts/run_rehearsal.py \
        --use-container --collect --stop \
        --components users groups projects work_packages
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR_DIR = ROOT / "var"
DATA_DIR = VAR_DIR / "data"
LOG_DIR = VAR_DIR / "logs"

DEFAULT_COMPONENTS = ["users", "groups", "projects", "work_packages"]


def run(cmd: Iterable[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a shell command, streaming output to the console."""
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, text=True)


def start_mocks() -> None:
    run(["docker", "compose", "--profile", "test", "up", "-d", "mock-jira", "mock-openproject", "redis", "postgres"])


def stop_mocks() -> None:
    run(["docker", "compose", "--profile", "test", "down"])


def run_component(component: str, *, reset_checkpoints: bool, container: bool) -> None:
    if container:
        cmd = [
            "docker",
            "compose",
            "--profile",
            "test",
            "run",
            "--rm",
            "test",
            "python",
            "-m",
            "src.main",
            "migrate",
            "--components",
            component,
            "--no-confirm",
        ]
        if component == "work_packages" and reset_checkpoints:
            cmd.append("--reset-wp-checkpoints")
        run(cmd)
    else:
        cmd = [
            "uv",
            "run",
            "--active",
            "--no-cache",
            "python",
            "-m",
            "src.main",
            "migrate",
            "--components",
            component,
            "--no-confirm",
        ]
        if component == "work_packages" and reset_checkpoints:
            cmd.append("--reset-wp-checkpoints")
        run(cmd)


def collect_artefacts(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    sources = [
        DATA_DIR / "group_mapping.json",
        DATA_DIR / "work_package_mapping.json",
        VAR_DIR / ".migration_checkpoints.db",
        LOG_DIR / "migration.log",
    ]
    for src in sources:
        if src.exists():
            print(f"Collecting {src.relative_to(ROOT)}")
            shutil.copy2(src, destination / src.name)
        else:
            print(f"Skipping {src.relative_to(ROOT)} (not found)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a rehearsal migration")
    parser.add_argument("--components", nargs="*", default=DEFAULT_COMPONENTS, help="Components to execute in order")
    parser.add_argument("--skip-compose", action="store_true", help="Assume compose services are already running")
    parser.add_argument("--stop", action="store_true", help="Stop compose services after completion")
    parser.add_argument("--collect", action="store_true", help="Collect artefacts under var/rehearsal/<timestamp>")
    parser.add_argument("--reset-wp-checkpoints", action="store_true", help="Reset work-package checkpoints before running")
    parser.add_argument("--use-container", action="store_true", help="Run migration components inside the Docker test container")
    args = parser.parse_args()

    if args.use_container and not args.skip_compose:
        start_mocks()

    try:
        for component in args.components:
            run_component(component, reset_checkpoints=args.reset_wp_checkpoints, container=args.use_container)
    except subprocess.CalledProcessError as exc:
        print(f"❌ Migration component '{component}' failed (exit code {exc.returncode})")
        sys.exit(exc.returncode)
    finally:
        if args.use_container and args.stop and not args.skip_compose:
            stop_mocks()

    if args.collect:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")
        dest = VAR_DIR / "rehearsal" / stamp
        collect_artefacts(dest)
        print(f"Artefacts stored in {dest}")


if __name__ == "__main__":
    main()
