# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Inventory Jira issues with a security level and seal the non-static ones for the operator.

``inventory`` makes no OpenProject request. Issues with a level stay out of the migration
as before; it only records who can read them and keeps every issue whose readers are
not the level's static readers in an age-encrypted archive (ADR-009). ``admins`` reads
that archive, detects the OpenProject edition and reports the OpenProject administrators
who are not static readers of a level; it warns and changes nothing (ADR-009, decision 4).
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
    PER_ISSUE_KINDS,
    STATIC_KINDS,
    Directory,
    ProjectInventory,
    SecurityLevelError,
    archived_issue,
    restricted_total,
    scan_project,
)
from src.infrastructure.openproject.openproject_edition import (
    Edition,
    EditionError,
    RailsReader,
    administrators,
    detect_edition,
)
from src.operator_history import _output_file, _private_create

ARCHIVE_FORMAT = "j2o-restricted-issues/1"
REPORT_FORMAT = "j2o-restricted-report/1"
ADMIN_REPORT_FORMAT = "j2o-restricted-admins/1"
KINDS = ("static", "per_issue", "unexplained")
_AGE = ".age"
_JSON = ".json"


def migration_account(jira: Endpoint, explicit: str | None) -> str:
    """The account whose temporary single-user grants are not counted; it must own the token.

    The default is the configured Jira user, ``J2O_JIRA_USERNAME`` (``jira.username``).
    """
    account = explicit or os.environ.get("J2O_JIRA_USERNAME") or ""
    require(account, "Set --migration-account or J2O_JIRA_USERNAME")
    found = jira.request("GET", "/rest/api/2/user", denial=True, params={"username": account})
    user = found.json() if found.status_code == 200 else None
    require(isinstance(user, dict) and user.get("name") == account, "Migration account not found in Jira")
    myself = jira.get("/rest/api/2/myself")
    require(
        isinstance(myself, dict) and myself.get("name") == account,
        "The inventory token does not belong to the migration account",
    )
    return account


def all_projects(jira: Endpoint) -> list[str]:
    """Every project key visible to the inventory account."""
    rows = jira.get("/rest/api/2/project")
    require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "Malformed project list")
    keys = [str(row["key"]) for row in rows]
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


def _mapped_jira_names(user_mapping: object) -> dict[int, set[str]]:
    """Casefolded Jira user names per OpenProject user ID; an empty or malformed mapping raises.

    Entries restored from OpenProject (``restored_from_op``) guess the Jira name from the
    login and do not count.
    """
    if not isinstance(user_mapping, dict):
        msg = "Malformed user mapping"
        raise ArchiveError(msg)
    jira_names: dict[int, set[str]] = {}
    for entry in user_mapping.values():
        if isinstance(entry, dict) and type(entry.get("openproject_id")) is int and not entry.get("restored_from_op"):
            name = entry.get("jira_name")
            if not isinstance(name, str) or not name:
                msg = "User mapping entry without a Jira user name"
                raise ArchiveError(msg)
            jira_names.setdefault(entry["openproject_id"], set()).add(name.casefold())
    # Without one usable entry every administrator would look unmapped; that is a missing input.
    require(jira_names, "User mapping has no Jira-to-OpenProject entry")
    return jira_names


def admin_report(archive: dict, user_mapping: dict, admins: list[dict], edition: Edition, sha256: str) -> dict:
    """Per level, the administrators whose mapped Jira users are not static readers; logins only.

    An administrator maps to the Jira users whose user-mapping entry names its
    OpenProject ID; it reads a level when any of them is a static reader.
    """
    jira_names = _mapped_jira_names(user_mapping)
    shown = {row["id"]: {"login": row["login"], "status": row["status"]} for row in admins}
    levels = []
    for key in sorted(archive["levels"]):
        level = archive["levels"][key]
        computed = all(kind in STATIC_KINDS | PER_ISSUE_KINDS for kind, _ in level["grants"])
        readers = {name.casefold() for name in level["static_readers"]}
        outside = [shown[a] for a in shown if a in jira_names and not jira_names[a] & readers] if computed else []
        levels.append(
            {
                "project": level["project"],
                "level_id": level["id"],
                "level": level["name"],
                "readers_computed": computed,
                "admins_not_readers": len(outside),
                "admins": outside,
            }
        )
    return {
        "format": ADMIN_REPORT_FORMAT,
        "archive_sha256": sha256,
        "edition": edition.name,
        "rails_features": edition.rails,
        "available_features": list(edition.available_features),
        "administrators": len(admins),
        "mapped_openproject_users": len(jira_names),
        "unmapped_admins": [shown[a] for a in shown if a not in jira_names],
        "levels": levels,
    }


def admins(args: argparse.Namespace, identity: x25519.Identity, rails: RailsReader) -> dict:
    """Detect the edition and write the administrator report; never stops the import."""
    require(not _output_file(args.report, _JSON).exists(), "Report already exists")
    user_mapping = json.loads(input_file(args.user_mapping).read_bytes())
    data = input_file(args.archive).read_bytes()
    archive = decrypt(data, identity)
    api = Endpoint(
        args.openproject,
        os.environ.get("J2O_OPENPROJECT_API_KEY", ""),
        jira=False,
        ca=os.getenv("J2O_ARCHIVE_CA_BUNDLE") or True,
    )
    edition = detect_edition(rails, api)
    value = admin_report(archive, user_mapping, administrators(rails), edition, hashlib.sha256(data).hexdigest())
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
    check = sub.add_parser("admins", help="Detect the OpenProject edition and report administrators per level")
    check.add_argument("--openproject", required=True, help="OpenProject base URL for /api/v3/configuration")
    check.add_argument("--user-mapping", type=Path, required=True, help="The migration's user_mapping.json")
    check.add_argument("--report", type=Path, required=True)
    for command in (scan, restore, check):
        command.add_argument("--identity", type=Path, required=True)
        command.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        identity = x25519.Identity.from_str(input_file(args.identity).read_text().strip())
        if args.command == "inventory":
            sys.stdout.write(json.dumps(inventory(args, identity)["totals"]) + "\n")
        elif args.command == "admins":
            # The Rails console connection comes from the j2o configuration (config/config.yaml).
            from src.infrastructure.openproject.openproject_client import OpenProjectClient

            value = admins(args, identity, OpenProjectClient())
            listed = {"unmapped": len(value["unmapped_admins"])}
            listed |= {f"{row['project']}:{row['level_id']}": row["admins_not_readers"] for row in value["levels"]}
            sys.stderr.write(f"Administrators who are not static readers, per level: {json.dumps(listed)}\n")
            sys.stdout.write(
                json.dumps({"edition": value["edition"], "administrators": value["administrators"]}) + "\n"
            )
        else:
            value = decrypt(input_file(args.archive).read_bytes(), identity)
            _private_create(args.output, encoded(value), _JSON)
    except (ArchiveError, SecurityLevelError, EditionError) as exc:
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
