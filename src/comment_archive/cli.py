# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Enroll readers, migrate a frozen Jira project and read comments from OpenProject alone."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pyrage
from pyrage import x25519

from src.comment_archive.archive import enrollment, migrate, unpack
from src.comment_archive.transport import ArchiveError, Endpoint, attachments, download, require


def input_file(path: Path) -> Path:
    """Resolve an explicitly selected local input and reject devices, pipes and directories."""
    resolved = path.resolve(strict=True)
    require(resolved.is_file(), "Expected a regular local file")
    return resolved


def load(path: Path) -> Any:
    """Read a local operator manifest."""
    return json.loads(input_file(path).read_bytes())


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; credentials and private keys stay out of command arguments."""
    parser = argparse.ArgumentParser(prog="j2o-comments")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("keygen", help="Generate a personal age identity on the reader's device")
    create.add_argument("--identity", type=Path, required=True)
    enroll = sub.add_parser("enroll", help="Bind both authenticated accounts to a personal public key")
    enroll.add_argument("--identity", type=Path, required=True)
    enroll.add_argument("--jira", required=True)
    enroll.add_argument("--openproject", required=True)
    run = sub.add_parser("migrate", help="Encrypt, publish and verify every enrolled reader's comments")
    for arg in ("config", "mapping", "directory", "inventory", "state"):
        run.add_argument("--" + arg, type=Path, required=True)
    run.add_argument("--project", required=True)
    read = sub.add_parser("read", help="Read from OpenProject or a downloaded attachment without Jira")
    read.add_argument("--identity", type=Path, required=True)
    read.add_argument("--work-package", type=int, required=True)
    origin = read.add_mutually_exclusive_group(required=True)
    origin.add_argument("--openproject")
    origin.add_argument("--file", type=Path)
    args = parser.parse_args(argv)
    ca = os.environ.get("J2O_ARCHIVE_CA_BUNDLE") or True
    try:
        if args.command == "keygen":
            identity = x25519.Identity.generate()
            destination = args.identity.parent.resolve(strict=True) / args.identity.name
            flags = os.O_EXCL | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(destination, flags, 0o600), "w") as stream:
                stream.write(str(identity) + "\n")
            result = {"recipient": str(identity.to_public())}
        elif args.command == "enroll":
            identity = x25519.Identity.from_str(input_file(args.identity).read_text().strip())
            jt = os.environ["J2O_ARCHIVE_JIRA_TOKEN"]
            ot = os.environ["J2O_ARCHIVE_OP_TOKEN"]
            result = enrollment(
                Endpoint(args.jira, jt, jira=True, ca=ca),
                Endpoint(args.openproject, ot, jira=False, ca=ca),
                str(identity.to_public()),
                jt,
                ot,
            )
        elif args.command == "migrate":
            config = load(args.config)
            result = migrate(
                config,
                load(args.mapping) if args.mapping.exists() else None,
                args.project,
                args.state,
                ca,
                directory=load(args.directory),
                inventory_file=load(args.inventory),
            )
        else:
            identity = x25519.Identity.from_str(input_file(args.identity).read_text().strip())
            if args.file:
                blob = input_file(args.file).read_bytes()
            else:
                op = Endpoint(args.openproject, os.environ["J2O_ARCHIVE_OP_TOKEN"], jira=False, ca=ca)
                files = attachments(op, args.work_package)
                require(len(files) == 1, "Expected one archive on the work package")
                blob = download(op, files[0])
            result = unpack(blob, identity, args.work_package)
            if args.openproject:
                require(result["target"] == op.url, "Archive belongs to another OpenProject installation")
        sys.stdout.write(json.dumps(result, ensure_ascii=True, indent=2) + "\n")
    except ArchiveError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except (
        KeyError,
        ValueError,
        TypeError,
        OSError,
        sqlite3.Error,
        pyrage.DecryptError,
        pyrage.RecipientError,
        pyrage.IdentityError,
        pyrage.EncryptError,
    ) as exc:
        # Error messages from libraries can include tokens, response bodies or private input.
        sys.stderr.write(f"Archive operation could not be verified ({type(exc).__name__})\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
