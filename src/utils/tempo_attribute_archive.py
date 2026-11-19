# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Seal operator-only Tempo values and verify matching packets returned by OpenProject."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from typing import Any
from urllib.parse import urlsplit

import pyrage
from pyrage import x25519

from src.comment_archive.archive import encoded

KEY_SETTING = "J2O_TEMPO_ATTRIBUTE_IDENTITY"


def _identity() -> x25519.Identity:
    filename = os.environ.get(KEY_SETTING)
    if not filename:
        msg = f"Set {KEY_SETTING} to the recovery operator's private age identity file."
        raise ValueError(msg)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(filename, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or (os.name == "posix" and info.st_mode & 0o077):
                raise ValueError
            keys = [line for line in stream.read(4097).decode().splitlines() if line and not line.startswith("#")]
            (secret,) = keys
            return x25519.Identity.from_str(secret.strip())
    except OSError, ValueError, pyrage.IdentityError:
        msg = f"{KEY_SETTING} requires a private file with one age X25519 identity."
        raise ValueError(msg) from None


_FLAT = ("work_attributes", "worklogAttributes", "workAttributes")
_NESTED = ("work_attribute_values", "workAttributeValues")


def has_values(row: dict) -> bool:
    """Recognize populated current and older Server worklog representations."""
    return any(row.get(name) for name in (*_FLAT, *_NESTED))


def _source_values(row: dict, aliases: tuple[str, ...]) -> list:
    values = [row[name] for name in aliases if row.get(name)]
    if not values:
        return []
    if any(not isinstance(value, list) or encoded(value) != encoded(values[0]) for value in values):
        msg = "Tempo returned inconsistent work attribute representations."
        raise ValueError(msg)
    return values[0]


def seal_values(row: dict[str, Any], jira_url: str) -> dict[str, Any]:
    """Replace clear source attributes with a packet for the recovery operator only."""
    flat = _source_values(row, _FLAT)
    nested = _source_values(row, _NESTED)
    if not (flat or nested):
        return row
    issue = row.get("issue_key") or (row.get("issue") or {}).get("key", "")
    worklog = str(row.get("jira_worklog_id", row.get("jiraWorklogId")))
    issue_id = str(row.get("issue_id") or (row.get("issue") or {}).get("id"))
    if any(str(item.get("worklogId")) != worklog for item in nested):
        msg = "A Tempo attribute refers to a different worklog."
        raise ValueError(msg)
    base = urlsplit(jira_url)
    if not (
        re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", issue)
        and re.fullmatch(r"[1-9][0-9]*", worklog)
        and re.fullmatch(r"[1-9][0-9]*", issue_id)
        and base.scheme in {"http", "https"}
        and base.hostname
        and not (base.username or base.password or base.query or base.fragment)
    ):
        msg = "Cannot bind Tempo attributes to a Jira site, issue and worklog."
        raise ValueError(msg)
    payload = {
        "project": issue.rpartition("-")[0],
        "issue": issue,
        "source_worklog": worklog,
        "worklogAttributes": flat,
        "workAttributeValues": nested,
    }
    sealed = base64.b64encode(pyrage.encrypt(encoded(payload), [_identity().to_public()])).decode("ascii")
    result = {k: v for k, v in row.items() if k not in (*_FLAT, *_NESTED)}
    result.update(issue_key=issue, jira_worklog_id=worklog)
    result["tempo_archive"] = {
        "packet": sealed,
        "issue_id": issue_id,
        "issue_url": f"{jira_url.rstrip('/')}/browse/{issue}",
    }
    return result


def _open_packet(packet: str, key: x25519.Identity) -> dict:
    return json.loads(pyrage.decrypt(base64.b64decode(packet, validate=True), [key]))


def verify_packets(expected: str, stored: str, origin: str) -> None:
    """Authenticate the stored ciphertext, source worklog binding and original value types."""
    key = _identity()
    try:
        payload = _open_packet(expected, key)
        expected_origin = f"{payload['issue']}:{payload['source_worklog']}"
        if expected_origin != origin or encoded(payload) != encoded(_open_packet(stored, key)):
            raise ValueError
    except ValueError, TypeError, KeyError, pyrage.DecryptError:
        msg = "Stored Tempo attributes differ from the expected worklog or cannot be decrypted."
        raise ValueError(msg) from None
