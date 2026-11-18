# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Archive unresolved project references or replay them on a private cutover target."""

import argparse
import json
import sys
from pathlib import Path

from pyrage import x25519

from src.deferred_references import inventory, mappings, read_snapshot, write_snapshot
from src.deferred_references_replay import apply


def main(argv: list[str] | None = None) -> None:
    """Run the operator's offline cross-project reference workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, action="append", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--issues", type=Path, required=True)
    capture.add_argument("--project", required=True)
    capture.add_argument("--jira", required=True)
    capture.add_argument("--epic-field", required=True)
    capture.add_argument("--known-ids", type=Path, help="Optional verified Jira key-to-ID lookup for epic keys")
    commands.add_parser("apply")
    args = parser.parse_args(argv)
    identity = x25519.Identity.from_str(args.identity.read_text().strip())
    mapped = mappings(args.mapping)
    if args.command == "capture":
        raw = json.loads(args.issues.read_bytes())
        source = list(raw.values()) if isinstance(raw, dict) else raw
        known = json.loads(args.known_ids.read_bytes()) if args.known_ids else {}
        data = inventory(
            source, mapped, project=args.project, jira=args.jira, epic_field=args.epic_field, identities=known
        )
        write_snapshot(args.archive, data, identity)
        result = {"archived": len(data["references"])}
    else:
        from src.infrastructure.openproject.openproject_client import OpenProjectClient

        data = read_snapshot(args.archive, identity)
        result = apply(data, mapped, OpenProjectClient())
    sys.stdout.write(json.dumps(result) + "\n")
