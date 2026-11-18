# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Publish a complete local JSON export in the operator-selected directory."""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def write_json_export(destination: Path, records: Any, *, indent: int | None = 2) -> None:
    directory = destination.parent.resolve()
    path = directory / destination.name
    if not path.resolve().is_relative_to(directory) or path.is_symlink() or (path.exists() and not path.is_file()):
        msg = f"Expected a regular export file in {directory}"
        raise ValueError(msg)
    directory.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as staged:
        temporary = Path(staged.name)
        try:
            json.dump(records, staged, indent=indent)
            staged.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
