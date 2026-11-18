# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT
"""Read regular operator-selected inputs and atomically publish private exports."""

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, TextIO


def _selected_file(path: Path) -> Path:
    if not path.name or ".." in path.parts:
        msg = "Select a file directly in the intended directory"
        raise ValueError(msg)
    directory = path.parent.resolve()
    candidate = directory / path.name
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(directory):
        msg = "Expected a regular export or input file in the selected directory"
        raise ValueError(msg)
    return candidate


@contextmanager
def open_operator_text(path: Path) -> Iterator[TextIO]:
    """Open a regular UTF-8 input, checking the opened descriptor before reading."""
    source = _selected_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(source, flags), encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            msg = "Expected a regular input file"
            raise ValueError(msg)
        yield stream


def read_operator_text(path: Path) -> str:
    """Read a complete regular text file."""
    with open_operator_text(path) as stream:
        return stream.read()


@contextmanager
def _output(path: Path) -> Iterator[TextIO]:
    target = _selected_file(path)
    if target.exists() and not target.is_file():
        msg = "Expected a regular output file"
        raise ValueError(msg)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            yield stream
            stream.close()
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)


def write_operator_text(path: Path, text: str) -> None:
    """Replace the selected output only after writing the full text."""
    with _output(path) as stream:
        stream.write(text)


def write_operator_json(path: Path, obj: Any, *, indent: int | None = 2) -> None:
    """Stream a JSON export without replacing a previous result on failure."""
    with _output(path) as stream:
        json.dump(obj, stream, indent=indent)
