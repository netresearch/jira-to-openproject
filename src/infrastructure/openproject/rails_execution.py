# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Protect one dispatched Rails operation across console and runner transports."""

from pathlib import Path


def once_per_staging_directory(script: str, directory: Path) -> str:
    """Keep a runner fallback from repeating work already dispatched to the console."""
    from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

    state = escape_ruby_single_quoted((directory / "execution.state").as_posix())
    return (
        f"File.open('{state}', File::RDWR | File::CREAT, 0600) do |j2o_lock|\n"
        "  j2o_lock.flock(File::LOCK_EX)\n"
        "  case j2o_lock.read\n"
        "  when 'done'\n    nil\n"
        "  when ''\n"
        "    j2o_lock.write('started'); j2o_lock.flush; j2o_lock.fsync\n"
        f"{script}\n"
        "    j2o_lock.rewind; j2o_lock.write('done'); j2o_lock.truncate(4); j2o_lock.flush; j2o_lock.fsync\n"
        "  else\n    raise 'Previous Rails execution did not finish; verify the target before retrying'\n"
        "  end\nend\n"
    )
