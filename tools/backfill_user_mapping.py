r"""Backfill ``user_mapping.json`` for Jira users that the watcher / TE / etc.
migrations could not resolve.

The watcher migration (and its siblings) records each unmapped Jira
identity in ``ComponentResult.details["unmapped_users"]``. Live
2026-05-07 NRS run captured 18 distinct names (anne.geissler, ...,
yannic.thieme). Most of them are real OP users with matching
``login`` / ``mail`` — they just never made it into
``user_mapping.json`` because the source-of-truth user iteration
(``user_migration.create_user_mapping``) was scoped to
``/rest/api/2/users`` which excludes locked / inactive accounts.

This tool closes the loop:

1. Read the current ``user_mapping.json``.
2. Take a list of Jira identifiers (CLI args OR read from a file)
   and skip any that are already mapped.
3. For each remaining identifier:

   a. Query Jira for the full user profile.
   b. Query OP for a matching user (by ``login``, then ``mail``).
   c. If found → append to ``user_mapping``.
   d. If not found → record under ``not_found_in_op`` for manual
      remediation (operator either creates the OP user or accepts
      the loss as expected for fully-deleted Jira identities).

4. Write the updated mapping back atomically.

Usage::

    .venv/bin/python -m tools.backfill_user_mapping \\
        anne.geissler caroline.kuhn ...

    # Or read from a file (one name per line):
    .venv/bin/python -m tools.backfill_user_mapping --names-file unmapped.txt

    # Or read from a migration_results.json's watcher details:
    .venv/bin/python -m tools.backfill_user_mapping \\
        --from-migration-results var/results/migration_results_*.json
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from src.infrastructure.openproject.openproject_client import OpenProjectClient

_DEFAULT_USER_MAPPING_FILE = Path("var/data/user_mapping.json")


def _load_user_mapping(path: Path) -> dict[str, dict[str, Any]]:
    """Return the current user mapping, or ``{}`` when the file is absent.

    A missing file is a legitimate state on a fresh checkout / CI
    environment — treat as empty rather than failing.
    """
    if not path.exists():
        sys.stderr.write(f"[backfill] user_mapping not found at {path}, starting from empty\n")
        return {}
    with path.open() as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        msg = f"user_mapping at {path} is not a dict (got {type(raw).__name__})"
        raise TypeError(msg)
    # Pass through; downstream is permissive on shape (legacy
    # ``int`` rows, dict rows with ``openproject_id``, etc.).
    return raw


def _save_user_mapping(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: tmp + rename. Avoids a half-written mapping if the
    process crashes mid-dump (the very class of silent-failure
    PR #197 caught for ``work_package_mapping``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(path)


def _read_names_from_file(path: Path) -> list[str]:
    """One name per line; ``#`` comments + blank lines stripped."""
    out: list[str] = []
    with path.open() as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def _read_names_from_migration_results(path: Path) -> list[str]:
    """Extract ``unmapped_users`` from a migration_results.json's watcher
    details.

    Returns the de-duplicated, sorted list of distinct identities the
    watcher migration could not resolve. Other components (relation,
    time_entry, …) may grow similar fields in the future; the function
    walks every component and unions any ``unmapped_users`` it finds.
    """
    with path.open() as f:
        d = json.load(f)
    components = d.get("components") or d.get("component_results") or {}
    if not isinstance(components, dict):
        msg = f"unexpected components shape in {path}: {type(components).__name__}"
        raise TypeError(msg)
    out: set[str] = set()
    for comp in components.values():
        if not isinstance(comp, dict):
            continue
        details = comp.get("details") or {}
        if not isinstance(details, dict):
            continue
        names = details.get("unmapped_users") or []
        if isinstance(names, list):
            for name in names:
                if isinstance(name, str) and name.strip():
                    out.add(name.strip())
    return sorted(out)


def _candidate_keys(jira_user: dict[str, Any]) -> list[str]:
    """Identifier ladder for matching Jira user → OP user.

    Order mirrors :meth:`AttachmentProvenanceMigration._resolve_user_id`
    so a backfilled mapping is reachable by every probe path the
    migration uses. Skips empty / ``None`` values so the caller's
    ``mapping[key] = entry`` doesn't write a ``""`` key.
    """
    out: list[str] = []
    for k in ("name", "key", "accountId", "emailAddress"):
        v = jira_user.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def _find_op_user(op_client: OpenProjectClient, jira_user: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort: locate an OP user matching the Jira user.

    Probe order:

    1. ``login == jira.name`` — Server/DC fast path.
    2. ``mail == jira.emailAddress`` — Cloud + cross-instance.

    Returns ``None`` when no probe matches; caller records the user
    under ``not_found_in_op`` for manual handling. ``get_user`` raises
    on miss; we catch + continue so one missing probe doesn't cancel
    the next.
    """
    name = jira_user.get("name") or jira_user.get("key")
    if isinstance(name, str) and name:
        try:
            user = op_client.get_user(name)
        except Exception:
            user = None
        if user:
            return user

    email = jira_user.get("emailAddress")
    if isinstance(email, str) and email:
        try:
            user = op_client.get_user_by_email(email)
        except Exception:
            user = None
        if user:
            return user
    return None


def backfill(
    names: list[str],
    mapping_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the backfill; return a structured report."""
    from src.infrastructure.jira.jira_client import JiraClient

    user_mapping = _load_user_mapping(mapping_path)
    sys.stderr.write(f"[backfill] loaded {len(user_mapping)} existing user_mapping entries\n")

    jira_client = JiraClient()
    op_client = OpenProjectClient()

    added: list[dict[str, Any]] = []
    already_mapped: list[str] = []
    not_found_in_jira: list[str] = []
    not_found_in_op: list[dict[str, Any]] = []

    for name in names:
        # Already in the mapping under any of its identifiers? Skip.
        if name in user_mapping:
            already_mapped.append(name)
            continue

        try:
            jira_user = jira_client.get_user_info(name)
        except Exception:
            jira_user = None
        if not isinstance(jira_user, dict):
            not_found_in_jira.append(name)
            continue

        op_user = _find_op_user(op_client, jira_user)
        if op_user is None:
            not_found_in_op.append(
                {
                    "jira_name": name,
                    "jira_email": jira_user.get("emailAddress"),
                    "jira_display": jira_user.get("displayName"),
                    "active": jira_user.get("active"),
                },
            )
            continue

        op_id = op_user.get("id")
        if not isinstance(op_id, int):
            try:
                op_id = int(op_id) if op_id is not None else None
            except TypeError, ValueError:
                op_id = None
        if op_id is None:
            not_found_in_op.append(
                {
                    "jira_name": name,
                    "jira_email": jira_user.get("emailAddress"),
                    "reason": "op_user_id_missing_or_non_integer",
                },
            )
            continue

        entry = {
            "openproject_id": op_id,
            "jira_name": jira_user.get("name"),
            "jira_email": jira_user.get("emailAddress"),
            "jira_display_name": jira_user.get("displayName"),
            "jira_key": jira_user.get("key"),
            "matched_by": "backfill_unmapped_users",
        }
        # Insert under every identifier the migration may probe so the
        # mapping is reachable from any probe path. Don't clobber
        # existing entries on those keys (preserves operator manual
        # fixes).
        for k in _candidate_keys(jira_user):
            if k not in user_mapping:
                user_mapping[k] = entry
        added.append({"name": name, "openproject_id": op_id, "matched_by_keys": _candidate_keys(jira_user)})

    if added and not dry_run:
        _save_user_mapping(mapping_path, user_mapping)
        sys.stderr.write(f"[backfill] wrote {len(user_mapping)} entries to {mapping_path}\n")
    elif added and dry_run:
        sys.stderr.write(f"[backfill] DRY RUN — would write {len(user_mapping)} entries to {mapping_path}\n")

    return {
        "summary": {
            "names_examined": len(names),
            "added": len(added),
            "already_mapped": len(already_mapped),
            "not_found_in_jira": len(not_found_in_jira),
            "not_found_in_op": len(not_found_in_op),
        },
        "added": added,
        "already_mapped": already_mapped,
        "not_found_in_jira": not_found_in_jira,
        "not_found_in_op": not_found_in_op,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill user_mapping.json for unmapped Jira users surfaced by watcher / TE / … migrations",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("names", nargs="*", default=[], help="Jira usernames (positional)")
    src.add_argument("--names-file", type=Path, help="File with one Jira username per line")
    src.add_argument(
        "--from-migration-results",
        type=Path,
        help="Read unmapped_users from a migration_results.json",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=_DEFAULT_USER_MAPPING_FILE,
        help=f"Path to user_mapping.json (default: {_DEFAULT_USER_MAPPING_FILE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write the mapping; just report what would be added",
    )
    args = parser.parse_args(argv)

    if args.names_file:
        names = _read_names_from_file(args.names_file)
    elif args.from_migration_results:
        names = _read_names_from_migration_results(args.from_migration_results)
    else:
        names = list(args.names)

    if not names:
        sys.stderr.write("[backfill] no names supplied — nothing to do\n")
        return 0

    try:
        report = backfill(names, args.mapping, dry_run=args.dry_run)
    except Exception as exc:
        sys.stderr.write(f"[backfill] failed: {type(exc).__name__}: {exc}\n")
        sys.stderr.write(traceback.format_exc())
        return 2

    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    s = report["summary"]
    sys.stderr.write(
        f"[backfill] added={s['added']},"
        f" already_mapped={s['already_mapped']},"
        f" not_found_in_jira={s['not_found_in_jira']},"
        f" not_found_in_op={s['not_found_in_op']}\n",
    )
    return 0
    # NB: returns 0 even when some weren't found — partial backfill is the
    # expected outcome on a real instance, not a hard failure. Operator
    # reads ``not_found_in_op`` and decides per-user.


if __name__ == "__main__":
    sys.exit(main())
