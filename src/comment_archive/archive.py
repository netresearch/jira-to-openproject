# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Freeze the verified reader views into native age envelopes. Only ciphertext reaches OpenProject."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyrage
from pyrage import x25519

from src.comment_archive.transport import (
    ArchiveError,
    Endpoint,
    check_core_metadata,
    core_comments,
    download,
    project_kind,
    publish,
    require,
    visible_comments,
)

if TYPE_CHECKING:
    from pathlib import Path


def encoded(value: Any) -> bytes:
    """Canonical encoding of the snapshot and enrollment records."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def proof(token: str, binding: dict) -> str:
    """Bind the owner-supplied recipient and both account identities to a personal token."""
    return hmac.digest(token.encode(), b"j2o-comment-enrollment-v1\0" + encoded(binding), "sha256").hex()


def enrollment(jira: Endpoint, op: Endpoint, recipient: str, jira_token: str, op_token: str) -> dict:
    """Use both authenticated accounts to register this reader's own public key."""
    key = jira.get("/rest/api/2/myself")["key"]
    principal = op.get("/api/v3/users/me")
    require(
        isinstance(key, str) and key and principal["_type"] == "User" and type(principal["id"]) is int,
        "Could not verify both reader identities",
    )
    binding = {
        "jira": jira.url,
        "openproject": op.url,
        "jira_key": key,
        "op_id": principal["id"],
        "recipient": str(x25519.Recipient.from_str(recipient)),
    }
    return {**binding, "jira_proof": proof(jira_token, binding), "op_proof": proof(op_token, binding)}


@dataclass
class Reader:
    """A verified pair of accounts and a personally enrolled encryption recipient."""

    jira: Endpoint
    op: Endpoint
    recipient: str
    key: str
    op_id: int


def readers_for(config: dict, ca: str | bool) -> list[Reader]:
    """Reject a swapped public key, credential, target user, server or duplicate identity."""
    readers = []
    for entry in config["readers"]:
        jt = os.environ[entry["jira_token_env"]]
        ot = os.environ[entry["op_token_env"]]
        jira = Endpoint(config["jira"], jt, jira=True, ca=ca)
        op = Endpoint(config["openproject"], ot, jira=False, ca=ca)
        record = entry["enrollment"]
        verified = enrollment(jira, op, record["recipient"], jt, ot)
        require(
            hmac.compare_digest(encoded(record), encoded(verified)), "Reader enrollment or current identity differs"
        )
        readers.append(Reader(jira, op, record["recipient"], record["jira_key"], record["op_id"]))
    require(
        readers
        and all(len({getattr(r, field) for r in readers}) == len(readers) for field in ("key", "op_id", "recipient")),
        "Empty roster or reused identity",
    )
    return readers


def unpack(blob: bytes, identity: x25519.Identity, wp: int) -> dict:
    """Open only the authenticated age envelope intended for this identity."""
    outer = json.loads(blob)
    require(outer["format"] == "j2o-comments/1", "Unsupported archive format")
    for envelope in outer["envelopes"]:
        try:
            plain = pyrage.decrypt(base64.b64decode(envelope, validate=True), [identity])
        except pyrage.DecryptError:
            continue
        result = json.loads(plain)
        require(result["wp"] == wp, "Archive identity mismatch")
        return result
    msg = "No readable comments for this key"
    raise ArchiveError(msg)


def cached_blob(db: sqlite3.Connection, wp: int, views: list[dict]) -> bytes | None:
    """Commit a stable ciphertext before upload, including the empty snapshot on a denial."""
    digest = hashlib.sha256(encoded(views)).hexdigest()
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT digest, blob FROM archives WHERE wp=?", (wp,)).fetchone()
        if existing is not None:
            require(existing[0] == digest, f"Jira audience or text changed for work package {wp}; keep target closed")
            return existing[1]
        # Identical views can share one age envelope. The encrypted payload
        # contains no list of peer readers.
        recipients: dict[bytes, list[x25519.Recipient]] = {}
        for row in views:
            payload = encoded({k: v for k, v in row.items() if k != "recipient"})
            recipients.setdefault(payload, []).append(x25519.Recipient.from_str(row["recipient"]))
        packets = [base64.b64encode(pyrage.encrypt(payload, keys)).decode() for payload, keys in recipients.items()]
        blob = encoded({"format": "j2o-comments/1", "envelopes": packets}) if packets else None
        db.execute("INSERT INTO archives(wp,digest,blob) VALUES(?,?,?)", (wp, digest, blob))
    return blob


def scoped_issues(jira: Endpoint, project: str, mapping: dict) -> list[tuple[str, str, int]]:
    """Reconcile every issue visible to the inventory account against the migration mapping."""
    mapped = {
        str(k): (v["jira_key"], int(v["openproject_id"]))
        for k, v in mapping.items()
        if isinstance(v, dict) and str(v.get("jira_key", "")).startswith(project + "-")
    }
    require(mapped and len({wp for _, wp in mapped.values()}) == len(mapped), "Missing or ambiguous project mapping")
    seen: dict[str, str] = {}
    expected = None
    while True:
        page = jira.get(
            "/rest/api/2/search",
            params={
                "jql": f'project = "{project}" ORDER BY key ASC',
                "fields": "key",
                "startAt": len(seen),
                "maxResults": 100,
            },
        )
        rows = page["issues"]
        total = page["total"]
        require(
            isinstance(rows, list)
            and type(total) is int
            and page["startAt"] == len(seen)
            and (expected is None or expected == total)
            and (rows or len(seen) == total),
            "Incomplete project inventory",
        )
        for row in rows:
            require(row["id"] not in seen, "Duplicate issue in project inventory")
            seen[row["id"]] = row["key"]
        require(len(seen) <= total, "Inconsistent project inventory total")
        if len(seen) == total:
            break
        expected = total
    require(seen == {i: k for i, (k, _) in mapped.items()}, "Jira issue inventory and OpenProject mapping differ")
    return sorted((key, i, wp) for i, (key, wp) in mapped.items())


def verified_inventory(inventory: Endpoint, key: str, kind: str, expected: list) -> tuple[dict, set[str]]:
    """Reconcile the independent frozen inventory and any service request restrictions."""
    master = visible_comments(inventory, key, kind, reader=False)
    require(
        isinstance(expected, list) and len(expected) == len(master) and set(expected) == set(master),
        f"Independent comment inventory differs for {key}",
    )
    scoped = set()
    if kind == "service_desk":
        # Portal customers use the service desk API. Also account for any Jira-only comments.
        raw = core_comments(inventory, key, reader=False)
        scoped = check_core_metadata(raw, service=True, request_view=master)
        core = {r["id"]: r for r in raw}
        require(
            {k: v["body"] for k, v in core.items()} == {k: v["body"] for k, v in master.items()},
            f"Service desk and Jira inventories disagree for {key}",
        )
    return master, scoped


def verified_reader_comments(reader: Reader, key: str, kind: str, master: dict, scoped: set[str]) -> dict:
    """Read the authenticated user view and corroborate service request restrictions."""
    rows = visible_comments(reader.jira, key, kind, reader=True)
    if kind == "service_desk":
        raw_core = core_comments(reader.jira, key, reader=True)
        reader_scoped = check_core_metadata(raw_core, service=True, request_view=rows)
        core_view = {row["id"]: row["body"] for row in raw_core}
        require(
            all(cid in rows and rows[cid]["body"] == body for cid, body in core_view.items())
            and scoped.union(reader_scoped).intersection(rows) <= core_view.keys(),
            f"Jira and service desk reader access disagree on {key}",
        )
    require(
        all(
            cid in master and row["body"] == master[cid]["body"] and row.get("public") == master[cid].get("public")
            for cid, row in rows.items()
        ),
        f"Reader and inventory revisions differ on {key}",
    )
    return rows


def capture_views(
    readers: list[Reader], key: str, wp: int, *, kind: str, master: dict, scoped: set[str]
) -> tuple[list[dict], list[Reader]]:
    """Build an envelope payload only for users with source and target access."""
    seen: set[str] = set()
    views = []
    approved = []
    for reader in readers:
        rows = verified_reader_comments(reader, key, kind, master, scoped)
        if rows:
            require(
                reader.op.get(f"/api/v3/work_packages/{wp}")["id"] == wp,
                f"An eligible reader lacks target access on {key}",
            )
            seen.update(rows)
            approved.append(reader)
            views.append(
                {
                    "wp": wp,
                    "issue": key,
                    "source": reader.jira.url,
                    "target": reader.op.url,
                    "recipient": reader.recipient,
                    "comments": [rows[cid] for cid in sorted(rows, key=int)],
                }
            )
    require(seen == set(master), f"Missing verified recipient for one or more comments on {key}")
    return views, approved


def migrate(
    config: dict, mapping: dict, project: str, state: Path, ca: str | bool, *, directory: list, inventory_file: dict
) -> dict:
    """Stage every reader view, publish ciphertext, verify downloads as the intended users."""
    inventory = Endpoint(config["jira"], os.environ["J2O_ARCHIVE_INVENTORY_TOKEN"], jira=True, ca=ca)
    writer = Endpoint(config["openproject"], os.environ["J2O_ARCHIVE_PUBLISHER_TOKEN"], jira=False, ca=ca)
    readers = sorted(readers_for(config, ca), key=lambda r: r.key)
    require(
        isinstance(directory, list)
        and len(directory) == len(readers)
        and {(row["jira_key"], row["op_id"]) for row in directory} == {(r.key, r.op_id) for r in readers},
        "Enroll every independently mapped Jira and OpenProject account",
    )
    require(
        inventory_file["jira"] == inventory.url and inventory_file["project"] == project,
        "Independent comment inventory has the wrong origin or project",
    )
    expected_comments = inventory_file["issues"]
    kind = project_kind(inventory, project)
    issues = scoped_issues(inventory, project, mapping)
    require(set(expected_comments) == {i for _, i, _ in issues}, "Independent issue inventory differs")
    scope = encoded([inventory.url, writer.url, project, issues, [(r.key, r.op_id, r.recipient) for r in readers]])
    # Give SQLite a literal absolute filesystem name (including for names such as ":memory:").
    state = state.absolute()
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not state.is_symlink(), "Use a private regular file for archive state")
    # The database contains ciphertext and verification hashes; restrict newly created files.
    fd = os.open(state, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    db = sqlite3.connect(state, timeout=120, uri=False)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS scope(id INTEGER PRIMARY KEY CHECK(id=1), config BLOB)")
        db.execute("CREATE TABLE IF NOT EXISTS archives(wp INTEGER PRIMARY KEY, digest TEXT NOT NULL, blob BLOB)")
        with db:
            db.execute("INSERT OR IGNORE INTO scope VALUES(1,?)", (scope,))
            require(
                db.execute("SELECT config FROM scope").fetchone()[0] == scope,
                "Snapshot scope changed; keep target closed",
            )
        counts = {"issues": len(issues), "comments": 0, "reader_copies": 0, "archives": 0}
        size_limit = writer.get("/api/v3/configuration")["maximumAttachmentFileSize"]
        require(type(size_limit) is int and size_limit > 0, "Unverified attachment size limit")
        approved: dict[int, list[Reader]] = {}
        for key, issue_id, wp in issues:
            target = writer.get(f"/api/v3/work_packages/{wp}")
            require(
                str(target.get(f"customField{int(config['origin_id_field'])}")) == issue_id
                and target.get(f"customField{int(config['origin_url_field'])}") == f"{inventory.url}/browse/{key}",
                f"Work package {wp} does not match the Jira origin",
            )
            master, scoped = verified_inventory(inventory, key, kind, expected_comments[issue_id])
            views, approved[wp] = capture_views(readers, key, wp, kind=kind, master=master, scoped=scoped)
            counts["reader_copies"] += sum(len(view["comments"]) for view in views)
            counts["comments"] += len(master)
            blob = cached_blob(db, wp, views)
            require(blob is None or len(blob) <= size_limit, f"Encrypted archive exceeds the attachment limit on {wp}")
        # Finish all source checks before the first target write.
        for _, _, wp in issues:
            with db:
                db.execute("BEGIN IMMEDIATE")
                blob = db.execute("SELECT blob FROM archives WHERE wp=?", (wp,)).fetchone()[0]
                attachment = publish(writer, wp, blob)
                if attachment:
                    for reader in approved[wp]:
                        require(download(reader.op, attachment) == blob, f"Reader download verification failed on {wp}")
                    counts["archives"] += 1
        return counts
    finally:
        db.close()
