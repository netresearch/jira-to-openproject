# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Archive unresolved project references or replay them on a private cutover target."""

import argparse
import json
import sys
from pathlib import Path

from pyrage import x25519

from src.comment_archive.cli import input_file, load
from src.deferred_references import inventory, mappings, read_snapshot, write_snapshot


def main(argv: list[str] | None = None) -> None:
    """Run the operator's offline cross-project reference workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, action="append", default=[])
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--issues", type=Path, required=True)
    capture.add_argument("--project", required=True)
    capture.add_argument("--jira", required=True)
    capture.add_argument("--epic-field", required=True)
    capture.add_argument("--known-ids", type=Path, help="Optional verified Jira key-to-ID lookup for epic keys")
    commands.add_parser("apply")
    commands.add_parser("read", help="Write the authenticated reference snapshot to standard output")
    args = parser.parse_args(argv)
    identity = x25519.Identity.from_str(input_file(args.identity).read_text().strip())
    if args.command == "read":
        sys.stdout.write(json.dumps(read_snapshot(args.archive, identity), ensure_ascii=False, indent=2) + "\n")
        return
    if not args.mapping:
        parser.error("capture and apply require --mapping")
    mapped = mappings(args.mapping)
    if args.command == "capture":
        raw = load(args.issues)
        source = list(raw.values()) if isinstance(raw, dict) else raw
        known = load(args.known_ids) if args.known_ids else {}
        data = inventory(
            source, mapped, project=args.project, jira=args.jira, epic_field=args.epic_field, identities=known
        )
        write_snapshot(args.archive, data, identity)
        result = {"archived": len(data["references"])}
    else:
        from src.deferred_references_replay import apply
        from src.infrastructure.openproject.openproject_client import OpenProjectClient

        data = read_snapshot(args.archive, identity)
        result = apply(data, mapped, OpenProjectClient())
    sys.stdout.write(json.dumps(result) + "\n")
