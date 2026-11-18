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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pyrage
from pyrage import x25519

from src.comment_archive.archive import encoded
from src.comment_archive.cli import input_file
from src.comment_archive.transport import Endpoint, publish, require
from src.infrastructure.jira.comment_audience import has_comment_audience, may_copy_history_item

_FORMAT = "j2o-operator-field-history/1"
_CORE_ADDITIONAL_FIELDS = {"WorklogId", "WorklogTimeSpent", "RemoteIssueLink"}
_SYSTEM_CUSTOM = "com.atlassian.jira.plugin.system.customfieldtypes:"
_AGILE_CUSTOM = "com.pyxis.greenhopper.jira:"
_SCHEMA_TYPES = {
    *(_SYSTEM_CUSTOM + kind for kind in ("textarea", "textfield", "select", "multicheckboxes", "multiselect", "float")),
    *(
        _AGILE_CUSTOM + kind
        for kind in ("gh-sprint", "gh-epic-link", "gh-epic-label", "gh-epic-status", "gh-epic-color", "gh-lexo-rank")
    ),
    "com.tempoplugin.tempo-accounts:accounts.customfield",
}
_FIELDS = ("field", "fieldId", "fieldtype", "from", "fromString", "to", "toString")
_ISSUE_KEY = re.compile(r"[A-Z][A-Z\d_]*-\d+", re.ASCII)


@dataclass(frozen=True)
class Destination:
    """A private target project, work package and its Jira provenance fields."""

    wp: int
    project: int
    origin_id: int
    origin_url: int


def custom_fields(fields: list[dict], approved: set[str]) -> set[tuple[str, str | None]]:
    """Resolve unambiguous custom names and IDs from Jira's field catalogue."""
    names = Counter(field["name"] for field in fields)
    result: set[tuple[str, str | None]] = set()
    for field in fields:
        name, fid = field["name"], field["id"]
        schema = field.get("schema", {}).get("custom", "")
        if schema.startswith(_SYSTEM_CUSTOM) and fid not in approved:
            continue
        if re.fullmatch(r"customfield_\d+", fid, re.ASCII) and schema in _SCHEMA_TYPES:
            result.add((name, fid))
            if names[name] == 1:
                result.add((name, None))
    return result


def _synthetic_reference(item: dict) -> bool:
    if item.get("fieldId") or item["field"] not in {"Epic Child", "Parent Issue"}:
        return False
    for side in ("from", "to"):
        value, label = item.get(side), item.get(side + "String")
        if value is None and label is None:
            continue
        if not isinstance(label, str) or not _ISSUE_KEY.fullmatch(label):
            return False
        if not isinstance(value, str) or not ((value.isascii() and value.isdecimal()) or _ISSUE_KEY.fullmatch(value)):
            return False
    return item.get("from") is not None or item.get("to") is not None


def _keep(item: dict, history: dict, fields: set[tuple[str, str | None]]) -> bool:
    if "raw" in item or has_comment_audience(history) or has_comment_audience(item):
        return False
    if any(value is not None and type(value) not in (str, int) for value in (item.get(k) for k in _FIELDS)):
        return False
    name, fid = item.get("field"), item.get("fieldId")
    if not isinstance(name, str):
        return False
    if item.get("fieldtype") == "jira":
        if name.startswith("customfield_") or (fid and str(fid).casefold() != name.casefold()):
            return False
        return may_copy_history_item(item, history) or name in _CORE_ADDITIONAL_FIELDS
    if item.get("fieldtype") != "custom" or not may_copy_history_item(item, history):
        return False
    return (name, fid) in fields or _synthetic_reference(item)


def _event(history: dict, fields: set[tuple[str, str | None]]) -> dict:
    values = [
        {"position": index, **{k: item[k] for k in _FIELDS if k in item}}
        for index, item in enumerate(history["items"])
        if _keep(item, history, fields)
    ]
    return {
        "id": str(history["id"]),
        "created": history["created"],
        "author": (history.get("author") or {}).get("key"),
        "changes": values,
    }


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
            sid.isascii() and sid.isdecimal() and _ISSUE_KEY.fullmatch(key) and key.rsplit("-", 1)[0] == project,
            "Invalid issue identity",
        )
        require(
            issue["fields"]["project"]["key"] == project and issue["self"] == f"{jira}/rest/api/2/issue/{sid}",
            "Wrong Jira origin",
        )
        require(sid not in source and key not in keys, "Duplicate issue identity")
        keys.add(key)
        page = issue["changelog"]
        histories = page["histories"]
        require(
            type(page["startAt"]) is int
            and type(page["total"]) is int
            and isinstance(histories, list)
            and page.get("isLast") is not False,
            "Invalid or partial history page",
        )
        require(
            page["startAt"] == 0
            and len(histories) == page["total"]
            and len({str(h["id"]) for h in histories}) == len(histories),
            "Incomplete or duplicate issue history",
        )
        events = [_event(h, resolved) for h in histories]
        source[sid] = {"key": key, "events": [event for event in events if event["changes"]]}
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
