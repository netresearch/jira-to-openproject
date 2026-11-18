# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Durable filesystem operations for work package transfer state."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def sync_directory(directory: Path) -> None:
    """Flush directory-entry changes before proceeding with a remote write."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_issue_snapshot(rows: list[dict[str, Any]], target: Path) -> None:
    """Replace the destination only after the complete snapshot and its file contents are synced."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", delete=False
    ) as output:
        staged = Path(output.name)
        try:
            json.dump(rows, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            staged.replace(target)
            sync_directory(target.parent)
        finally:
            staged.unlink(missing_ok=True)
