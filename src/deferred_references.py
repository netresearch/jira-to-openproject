# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Capture unresolved Jira edges without copying the linked issue's content."""

from __future__ import annotations

import hmac
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit

import pyrage
from pyrage import x25519

from src.comment_archive.archive import encoded
from src.comment_archive.cli import input_file, load

_ISSUE = re.compile(r"[A-Z][A-Z\d_]*-\d+", re.ASCII)


def _key(value: Any) -> str:
    if not isinstance(value, str) or _ISSUE.fullmatch(value) is None:
        msg = "Invalid Jira issue key"
        raise ValueError(msg)
    return value


def _id(value: Any) -> str:
    text = str(value)
    if not text.isascii() or not text.isdecimal() or int(text) <= 0:
        msg = "Expected a positive Jira or OpenProject ID"
        raise ValueError(msg)
    return text


def mappings(paths: list[Path]) -> dict[str, tuple[str, int]]:
    """Read numeric-ID work-package mappings, rejecting ambiguous or conflicting identities."""
    result: dict[str, tuple[str, int]] = {}
    for path in paths:
        for outer, value in load(path).items():
            if "security_level_project" in value:
                # A level work package lies in a private sub-project; OpenProject shows a relation
                # or parent whose other end a user cannot see as a placeholder row (ADR-009).
                msg = "Level work package mappings are not replayed"
                raise ValueError(msg)
            key = _key(value["jira_key"])
            pair = (_id(value.get("jira_id", outer)), int(_id(value["openproject_id"])))
            if result.setdefault(key, pair) != pair:
                msg = "Conflicting work-package mappings"
                raise ValueError(msg)
    if any(len(set(column)) != len(result) for column in zip(*result.values(), strict=True)):
        msg = "Multiple keys map to the same Jira or OpenProject ID"
        raise ValueError(msg)
    return result


def _linked_reference(link: dict, source: dict, mapped: dict) -> dict | None:
    directions = [side for side in ("inward", "outward") if side + "Issue" in link]
    if len(directions) != 1:
        msg = "Ambiguous Jira link direction"
        raise ValueError(msg)
    direction = directions[0]
    target = link[direction + "Issue"]
    key = _key(target["key"])
    target_id = _id(target["id"])
    if key in mapped:
        if mapped[key][0] != target_id:
            msg = "Linked issue does not match its immutable Jira ID"
            raise ValueError(msg)
        return None
    kind = link["type"]
    labels = {name: kind[name] for name in ("name", "inward", "outward")}
    if not all(isinstance(label, str) for label in labels.values()):
        msg = "Malformed Jira link type"
        raise ValueError(msg)
    return {
        "kind": "link",
        "source": source,
        "target": {"key": key, "id": target_id},
        "link_id": _id(link["id"]),
        "direction": direction,
        "type": {"id": _id(kind["id"]), **labels},
    }


def _epic_id(epic: Any, key: str, mapped: dict, identities: dict) -> str | None:
    embedded = epic.get("id") if isinstance(epic, dict) else None
    ids = {_id(value) for value in (embedded, identities.get(key)) if value is not None}
    if len(ids) > 1 or (key in mapped and ids and mapped[key][0] not in ids):
        msg = "Epic key belongs to conflicting immutable Jira IDs"
        raise ValueError(msg)
    return next(iter(ids), None)


def _case_links(issue: dict, mapped: dict, epic_field: str, identities: dict) -> list[dict]:
    fields = issue["fields"]
    source = {"key": _key(issue["key"]), "id": _id(issue["id"])}
    result = [ref for link in fields["issuelinks"] if (ref := _linked_reference(link, source, mapped))]
    if epic := fields[epic_field]:
        key = _key(epic["key"] if isinstance(epic, dict) else epic)
        target_id = _epic_id(epic, key, mapped, identities)
        if key not in mapped:
            result.append(
                {
                    "kind": "epic",
                    "source": source,
                    "target": {"key": key, "id": target_id},
                    "direct_parent": _key(fields["parent"]["key"]) if fields.get("parent") else None,
                }
            )
    return result


def inventory(issues: list[dict], mapped: dict, *, project: str, jira: str, epic_field: str, identities: dict) -> dict:
    """Collect only edges of mapped issues in the selected project. Never follow a Jira link."""
    origin = urlsplit(jira)
    if (
        not origin.hostname
        or origin.scheme not in {"http", "https"}
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
    ):
        msg = "Expected a Jira base URL"
        raise ValueError(msg)
    expected = {k for k in mapped if k.partition("-")[0] == project}
    seen: set[str] = set()
    references = []
    for issue in issues:
        key = issue["key"]
        if key not in expected:
            continue
        if key in seen or mapped[key][0] != _id(issue["id"]) or issue["fields"]["project"]["key"] != project:
            msg = "Source issue does not match its project or mapping"
            raise ValueError(msg)
        seen.add(key)
        references.extend(_case_links(issue, mapped, epic_field, identities))
    if not expected or expected != seen:
        msg = "Expected a complete source snapshot for all mapped issues in the selected project"
        raise ValueError(msg)
    ids = [row["link_id"] for row in references if row["kind"] == "link"]
    if len(set(ids)) != len(ids):
        msg = "Duplicate deferred Jira link IDs"
        raise ValueError(msg)
    return {"version": 1, "jira": jira.rstrip("/"), "project": project, "references": sorted(references, key=encoded)}


def write_snapshot(path: Path, data: dict, identity: x25519.Identity) -> None:
    """Never overwrite an earlier snapshot or accept a conflicting retry."""
    if path.name in {"", ".", ".."} or path.is_symlink():
        msg = "Expected a regular archive destination"
        raise ValueError(msg)
    directory = path.parent.resolve(strict=True)
    destination = directory / path.name
    plaintext = encoded(data)
    signed = {"snapshot": data, "proof": _proof(plaintext, identity)}
    packet = pyrage.encrypt(encoded(signed), [identity.to_public()])
    with NamedTemporaryFile(dir=directory) as temp:
        temp.write(packet)
        temp.flush()
        os.fsync(temp.fileno())
        try:
            os.link(temp.name, destination)
        except FileExistsError:
            if read_snapshot(destination, identity) != data:
                msg = "Existing archive differs; select a new path for this snapshot"
                raise ValueError(msg) from None


def _proof(payload: bytes, identity: x25519.Identity) -> str:
    return hmac.digest(str(identity).encode(), b"j2o-deferred-references-v1\0" + payload, "sha256").hex()


def read_snapshot(path: Path, identity: x25519.Identity) -> dict:
    """Verify the operator wrote the archive before using it to change a target."""
    bundle = json.loads(pyrage.decrypt(input_file(path).read_bytes(), [identity]))
    data = bundle["snapshot"]
    if data["version"] != 1 or not hmac.compare_digest(bundle["proof"], _proof(encoded(data), identity)):
        msg = "Invalid deferred reference snapshot"
        raise ValueError(msg)
    return data
