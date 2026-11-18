# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT
"""Emit the allowed PyYAML distributions from the repository's uv lock."""

import tomllib
from pathlib import Path

if __name__ == "__main__":
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    package = next(row for row in lock["package"] if row["name"] == "pyyaml")
    archives = [package["sdist"], *package["wheels"]]
    hashes = " ".join("--hash=" + archive["hash"] for archive in archives)
    print(f"pyyaml=={package['version']} {hashes}")
