# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Inventory Jira issues with a security level and seal the non-static ones for the operator.

No OpenProject request is made. Issues with a level stay out of the migration as
before; this command only records who can read them and keeps every issue whose
readers are not the level's static readers in an age-encrypted archive (ADR-009).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pyrage
from pyrage import x25519

from src.comment_archive.archive import encoded
from src.comment_archive.cli import input_file
from src.comment_archive.transport import ArchiveError, Endpoint, require
from src.infrastructure.jira.security_levels import (
    Directory,
    ProjectInventory,
    SecurityLevelError,
    archived_issue,
    restricted_total,
    scan_project,
)
from src.operator_history import _output_file, _private_create

ARCHIVE_FORMAT = "j2o-restricted-issues/1"
REPORT_FORMAT = "j2o-restricted-report/1"
KINDS = ("static", "per_issue", "unexplained")
_AGE = ".age"
_JSON = ".json"


def migration_account(jira: Endpoint, explicit: str | None) -> str:
    """The account whose temporary single-user grants are not counted; it must exist in Jira.

    The default is the configured Jira user, ``J2O_JIRA_USERNAME`` (``jira.username``).
    """
    account = explicit or os.environ.get("J2O_JIRA_USERNAME") or ""
    require(account, "Set --migration-account or J2O_JIRA_USERNAME")
    found = jira.request("GET", "/rest/api/2/user", denial=True, params={"username": account})
    require(found.status_code == 200 and found.json().get("name") == account, "Migration account not found in Jira")
    return account


def all_projects(jira: Endpoint) -> list[str]:
    """Every project key visible to the inventory account."""
    keys = [str(row["key"]) for row in jira.get("/rest/api/2/project")]
    require(keys and len(set(keys)) == len(keys), "Empty or ambiguous project list")
    return sorted(keys)


def report(jira: str, scans: list[ProjectInventory], sha256: str) -> dict:
    """Counts per project and level; issue keys of archived issues; never reader names."""
    levels: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []
    for scan in scans:
        for readers in scan.levels:
            rows = scan.issues[readers.level.id]
            counts = {kind: sum(1 for row in rows if row.kind == kind) for kind in KINDS}
            levels.append(
                {
                    "project": scan.project,
                    "level_id": readers.level.id,
                    "level": readers.level.name,
                    "supported_grants": readers.level.supported,
                    "static_readers": len(readers.readers),
                    "migration_account_single_user_grants": readers.account_grants,
                    "issues": len(rows),
                    **counts,
                    "migration_account_not_counted": sum(1 for row in rows if row.account_dropped),
                }
            )
            archived.extend(
                {
                    "key": row.issue["key"],
                    "level_id": readers.level.id,
                    "class": row.kind,
                    "extra_readers": len(row.extra),
                    "missing_readers": len(row.missing),
                }
                for row in rows
                if row.kind != "static"
            )
    totals = {key: sum(row[key] for row in levels) for key in ("issues", *KINDS)}
    return {
        "format": REPORT_FORMAT,
        "jira": jira,
        "projects": len(scans),
        "projects_with_levels": sum(1 for scan in scans if scan.levels),
        "totals": totals,
        "levels": levels,
        "archived": sorted(archived, key=lambda row: row["key"]),
        "archive_sha256": sha256,
    }


def archive_value(jira: Endpoint, scans: list[ProjectInventory], account: str) -> dict:
    """The operator archive: every non-static issue in full, with its readers and level."""
    issues: dict[str, dict] = {}
    levels: dict[str, dict] = {}
    for scan in scans:
        for readers in scan.levels:
            level = readers.level
            levels[f"{scan.project}:{level.id}"] = {
                "project": scan.project,
                "id": level.id,
                "name": level.name,
                "grants": [[g.kind, g.value] for g in level.grants],
                "static_readers": sorted(readers.readers),
            }
            for row in scan.issues[level.id]:
                if row.kind == "static":
                    continue
                require(row.issue["id"] not in issues, "Duplicate archived issue")
                issues[row.issue["id"]] = {
                    "key": row.issue["key"],
                    "project": scan.project,
                    "level_id": level.id,
                    "class": row.kind,
                    "readers": sorted(row.readers),
                    "extra_readers": sorted(row.extra),
                    "missing_readers": sorted(row.missing),
                    "issue": archived_issue(jira, row.issue["id"]),
                }
    return {
        "format": ARCHIVE_FORMAT,
        "jira": jira.url,
        "migration_account": account,
        "levels": levels,
        "issues": issues,
    }


def decrypt(data: bytes, identity: x25519.Identity) -> dict:
    """Authenticate the archive and check its format."""
    value = json.loads(pyrage.decrypt(data, [identity]))
    require(value["format"] == ARCHIVE_FORMAT, "Unrecognized archive")
    return value


def seal(value: dict, identity: x25519.Identity, path: Path) -> bytes:
    """Write a new archive for the operator key and verify it decrypts to the same value."""
    target = _output_file(path, _AGE)
    _private_create(path, pyrage.encrypt(encoded(value), [identity.to_public()]), _AGE)
    data = input_file(target).read_bytes()
    require(decrypt(data, identity) == value, "Sealed archive differs from the inventory")
    return data


def inventory(args: argparse.Namespace, identity: x25519.Identity) -> dict:
    """Scan the selected projects, seal the archive and return the shared report."""
    require(not _output_file(args.archive, _AGE).exists(), "Archive already exists")
    require(not _output_file(args.report, _JSON).exists(), "Report already exists")
    jira = Endpoint(
        args.jira,
        os.environ["J2O_ARCHIVE_INVENTORY_TOKEN"],
        jira=True,
        ca=os.getenv("J2O_ARCHIVE_CA_BUNDLE") or True,
    )
    account = migration_account(jira, args.migration_account)
    projects = sorted({key.upper() for key in args.project}) if args.project else all_projects(jira)
    directory = Directory(jira)
    scans = []
    for key in projects:
        scans.append(scan_project(jira, key, account, directory))
        sys.stderr.write(f"{key}: {sum(len(rows) for rows in scans[-1].issues.values())} restricted issues\n")
    if not args.project:
        counted = sum(len(rows) for scan in scans for rows in scan.issues.values())
        require(restricted_total(jira) == counted, "Restricted issues outside the scanned projects")
    data = seal(archive_value(jira, scans, account), identity, args.archive)
    value = report(jira.url, scans, hashlib.sha256(data).hexdigest())
    _private_create(args.report, encoded(value), _JSON)
    return value


def main(argv: list[str] | None = None) -> int:
    """Inventory restricted issues into an operator archive, or restore that archive."""
    parser = argparse.ArgumentParser(prog="j2o-restricted")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("inventory", help="Read levels and readers from Jira and seal non-static issues")
    scan.add_argument("--jira", required=True)
    scan.add_argument("--project", action="append", default=[], help="Limit to this Jira project (default: all)")
    scan.add_argument("--migration-account", help="Jira user name of the temporary grant (default: J2O_JIRA_USERNAME)")
    scan.add_argument("--report", type=Path, required=True)
    restore = sub.add_parser("restore", help="Decrypt the archive into a new private JSON file")
    restore.add_argument("--output", type=Path, required=True)
    for command in (scan, restore):
        command.add_argument("--identity", type=Path, required=True)
        command.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        identity = x25519.Identity.from_str(input_file(args.identity).read_text().strip())
        if args.command == "inventory":
            sys.stdout.write(json.dumps(inventory(args, identity)["totals"]) + "\n")
        else:
            value = decrypt(input_file(args.archive).read_bytes(), identity)
            _private_create(args.output, encoded(value), _JSON)
    except (ArchiveError, SecurityLevelError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        pyrage.DecryptError,
        pyrage.EncryptError,
        pyrage.IdentityError,
    ) as exc:
        # Library messages can carry tokens, response bodies or reader names.
        sys.stderr.write(f"Could not verify the restricted-issue inventory ({type(exc).__name__})\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
