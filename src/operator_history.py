# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Keep recognized Jira field-change history in an operator-encrypted project snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pyrage
from pyrage import x25519

from src.comment_archive.archive import encoded
from src.comment_archive.cli import input_file
from src.comment_archive.transport import Endpoint, publish, require
from src.infrastructure.jira.field_history import ISSUE_KEY, custom_fields, is_complete_page, kept_events

_FORMAT = "j2o-operator-field-history/1"


@dataclass(frozen=True)
class Destination:
    """A private target project, work package and its Jira provenance fields."""

    wp: int
    project: int
    origin_id: int
    origin_url: int


def snapshot(issues: list[dict], fields: list[dict], project: str, jira: str, approved: set[str]) -> dict:
    """Require a single origin and complete histories for every issue in the supplied export."""
    require(re.fullmatch(r"[A-Z][A-Z\d_]*", project, re.ASCII), "Invalid Jira project key")
    require(isinstance(issues, list) and issues, "Expected issues from one Jira project")
    resolved = custom_fields(fields, approved)
    source = {}
    keys = set()
    jira = jira.rstrip("/")
    for issue in issues:
        sid, key = str(issue["id"]), issue["key"]
        require(
            sid.isascii() and sid.isdecimal() and ISSUE_KEY.fullmatch(key) and key.rsplit("-", 1)[0] == project,
            "Invalid issue identity",
        )
        require(
            issue["fields"]["project"]["key"] == project and issue["self"] == f"{jira}/rest/api/2/issue/{sid}",
            "Wrong Jira origin",
        )
        require(sid not in source and key not in keys, "Duplicate issue identity")
        keys.add(key)
        page = issue["changelog"]
        require(is_complete_page(page), "Invalid, partial or duplicate issue history")
        source[sid] = {"key": key, "events": kept_events(page["histories"], resolved)}
    return {"format": _FORMAT, "jira": jira, "project": project, "issues": source}


def _output_file(path: Path, suffix: str) -> Path:
    """Confine new files to the operator's current working directory."""
    require(
        path.parent == Path()
        and path.suffix == suffix
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.name, re.ASCII)
        and not path.is_symlink(),
        f"Expected a local {suffix} filename in the working directory",
    )
    return Path.cwd() / path.name


def _private_create(path: Path, data: bytes, suffix: str) -> None:
    """Publish a finished private file atomically, keeping an existing archive intact."""
    leaf = _output_file(path, suffix).name
    directory = Path.cwd()
    with tempfile.TemporaryDirectory(dir=directory) as staging:
        name = Path(staging) / "complete"
        with os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.link(name, leaf, dst_dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)


def decrypt(data: bytes, identity: x25519.Identity) -> dict:
    """Authenticate the sealed bytes and validate their format before restoring them."""
    value = json.loads(pyrage.decrypt(data, [identity]))
    require(value["format"] == _FORMAT, "Unrecognized archive")
    return value


def seal(value: dict, identity: x25519.Identity, path: Path) -> None:
    """Round-trip both new and previously sealed snapshots before reporting success."""
    target = _output_file(path, ".age")
    if not target.exists():
        data = pyrage.encrypt(encoded(value), [identity.to_public()])
        try:
            _private_create(path, data, ".age")
        except FileExistsError:
            pass
    require(
        decrypt(input_file(target).read_bytes(), identity) == value, "Sealed snapshot differs from the supplied export"
    )


def publish_snapshot(op: Endpoint, destination: Destination, value: dict, data: bytes) -> None:
    """Attach only to a work package whose original Jira issue and URL belong to this encrypted project."""
    require(re.fullmatch(r"[A-Z][A-Z\d_]*", value["project"], re.ASCII), "Invalid archived project key")
    target = op.get(f"/api/v3/work_packages/{destination.wp}")
    project_path = f"/api/v3/projects/{destination.project}"
    require(
        target["_links"]["project"]["href"] == project_path and op.get(project_path).get("public") is False,
        "Expected a work package in the specified private project",
    )
    origin = str(target.get(f"customField{destination.origin_id}"))
    issue = value["issues"].get(origin)
    require(
        isinstance(issue, dict)
        and target.get(f"customField{destination.origin_url}") == f"{value['jira']}/browse/{issue['key']}",
        "Target work package has the wrong Jira origin",
    )
    publish(op, destination.wp, data, filename=f"jira-{value['project']}-operator-field-history.age")


def main(argv: list[str] | None = None) -> int:
    """Seal, publish or restore an operator-only project history archive."""
    parser = argparse.ArgumentParser(prog="j2o-history")
    sub = parser.add_subparsers(dest="command", required=True)
    pack = sub.add_parser("seal")
    for arg in ("issues", "fields"):
        pack.add_argument("--" + arg, type=Path, required=True)
    pack.add_argument("--field-id", action="append", default=[], help="Include this reviewed standard custom field")
    pack.add_argument("--project", required=True)
    pack.add_argument("--jira", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--output", type=Path, required=True)
    send = sub.add_parser("publish")
    send.add_argument("--openproject", required=True)
    for arg in ("work-package", "project-id", "origin-id-field", "origin-url-field"):
        send.add_argument("--" + arg, type=int, required=True)
    for command in (pack, restore, send):
        command.add_argument("--identity", type=Path, required=True)
        command.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        identity = x25519.Identity.from_str(input_file(args.identity).read_text().strip())
        if args.command == "seal":
            issues = json.loads(input_file(args.issues).read_bytes())
            value = snapshot(
                issues,
                json.loads(input_file(args.fields).read_bytes()),
                args.project,
                args.jira,
                set(args.field_id),
            )
            seal(value, identity, args.archive)
            source_count = sum(len(h["items"]) for i in issues for h in i["changelog"]["histories"])
            stored_count = sum(len(e["changes"]) for i in value["issues"].values() for e in i["events"])
            sys.stdout.write(
                json.dumps(
                    {
                        "archive_sha256": hashlib.sha256(input_file(args.archive).read_bytes()).hexdigest(),
                        "issues": len(issues),
                        "source_changes": source_count,
                        "archived_changes": stored_count,
                        "withheld_changes": source_count - stored_count,
                    }
                )
                + "\n"
            )
        else:
            data = input_file(args.archive).read_bytes()
            value = decrypt(data, identity)
            if args.command == "restore":
                _private_create(args.output, encoded(value), ".json")
            else:
                op = Endpoint(
                    args.openproject,
                    os.environ["J2O_ARCHIVE_PUBLISHER_TOKEN"],
                    jira=False,
                    ca=os.getenv("J2O_ARCHIVE_CA_BUNDLE") or True,
                )
                publish_snapshot(
                    op,
                    Destination(args.work_package, args.project_id, args.origin_id_field, args.origin_url_field),
                    value,
                    data,
                )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        pyrage.DecryptError,
        pyrage.EncryptError,
        pyrage.IdentityError,
    ) as exc:
        sys.stderr.write(f"Could not verify history operation ({type(exc).__name__})\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
