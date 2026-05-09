"""Backfill provenance markers onto OP journal notes that lack them.

After a correctly-authored migration run the OP journals have the right
author and content but no ``<!-- j2o:jira-comment-id:{id} -->`` marker.
Without that marker every subsequent re-run of
``--components work_packages_content`` treats the comments as "not yet
migrated" and creates a fresh duplicate set.

This script patches the raw ``notes`` column of each un-marked journal with
the correct provenance marker so future re-runs are idempotent.

Algorithm per work package
--------------------------
1. Fetch OP journals (notes != '', no existing marker) ordered by
   ``created_at`` — the order mirrors the order in which the migration
   created them.
2. Fetch Jira comments for the corresponding Jira issue (Jira returns them
   in chronological order).
3. If counts differ → SKIP + WARNING (unsafe to pair; operator must
   investigate).
4. Zip the two lists by position.  For each pair validate that the Jira
   comment author (resolved via ``user_mapping``) matches the OP journal's
   ``user_id``.  If any pair mismatches → SKIP + WARNING.
5. For safe WPs: append the provenance marker to each journal's notes using
   ``Journal.update_columns(notes: ...)`` — this bypasses ActiveRecord
   callbacks so no new journal version is created.

Safety guarantees
-----------------
- Default mode is ``--dry-run``; ``--apply`` is required to mutate.
- Refuses to mutate on count mismatch OR author mismatch.
- Idempotent: already-marked journals are excluded from the fetch so a
  second run is a safe no-op.

Usage::

    .venv/bin/python -m scripts.backfill_comment_provenance_markers NRS
    .venv/bin/python -m scripts.backfill_comment_provenance_markers NRS --dry-run
    .venv/bin/python -m scripts.backfill_comment_provenance_markers NRS --apply

"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Marker template (same as openproject_work_package_content_service.py).
_COMMENT_PROVENANCE_MARKER = "<!-- j2o:jira-comment-id:{jira_comment_id} -->"

#: Regex to detect an already-present provenance marker in notes.
_MARKER_RE = re.compile(r"<!--\s*j2o:jira-comment-id:[^\s>]+\s*-->")

#: Minimum valid project key: 2+ uppercase letters/digits.
_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\z")

#: Default data directory (relative to project root).
_DEFAULT_DATA_DIR = Path("var/data")


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------


def _setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("backfill_comment_provenance")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Pure pairing logic
# ---------------------------------------------------------------------------


def _pair_journals_with_comments(
    op_journals: list[dict[str, Any]],
    jira_comments: list[dict[str, Any]],
    user_mapping: dict[str, int],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], str | None]:
    """Pair OP journals with Jira comments by chronological order.

    Pre-filters already-marked journals from ``op_journals`` before pairing
    so the function is idempotent on WPs that are partially backfilled.

    Args:
        op_journals: OP journal dicts with keys ``id``, ``wp_id``,
            ``user_id``, ``notes``, ``created_at``.
        jira_comments: Jira comment dicts with keys ``id``,
            ``author_account_id``, ``body``.  Must be in chronological order
            (Jira REST API default).
        user_mapping: ``{jira_account_id: op_user_id}`` dict.

    Returns:
        ``(pairs, skip_reason)`` where ``pairs`` is a list of
        ``(op_journal, jira_comment)`` tuples and ``skip_reason`` is ``None``
        on success or a descriptive string when the WP must be skipped.
        When ``skip_reason`` is set ``pairs`` is always ``[]``.
    """
    # Exclude already-marked journals — they need no update.
    unmarked = [j for j in op_journals if not _MARKER_RE.search(j["notes"])]

    # If all journals are already marked: nothing to do, not an error.
    if not unmarked:
        return [], None

    # Count check: we need a 1:1 match.
    if len(unmarked) != len(jira_comments):
        return [], (f"count mismatch: {len(unmarked)} unmarked OP journals vs {len(jira_comments)} Jira comments")

    # Author validation: zip by position (both are in chronological order).
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for idx, (op_j, jira_c) in enumerate(zip(unmarked, jira_comments, strict=True)):
        jira_account_id: str = jira_c.get("author_account_id", "")
        expected_op_user_id = user_mapping.get(jira_account_id)
        actual_op_user_id = op_j.get("user_id")
        if expected_op_user_id is None or expected_op_user_id != actual_op_user_id:
            return [], (
                f"author mismatch at position {idx}: "
                f"OP journal #{op_j['id']} has user_id={actual_op_user_id} "
                f"but Jira comment {jira_c['id']} (account {jira_account_id!r}) "
                f"maps to op_user_id={expected_op_user_id}"
            )
        pairs.append((op_j, jira_c))

    return pairs, None


# ---------------------------------------------------------------------------
# Rails script builders
# ---------------------------------------------------------------------------


def _build_fetch_journals_script(wp_id: int) -> str:
    """Ruby script: fetch un-marked journals for a single WP, ordered by created_at."""
    return f"""
require 'json'
wp_id = {int(wp_id)}
journals = Journal
  .where(journable_type: 'WorkPackage', journable_id: wp_id)
  .where.not(notes: [nil, ''])
  .where("notes NOT LIKE '%j2o:jira-comment-id:%'")
  .order(:created_at)
  .pluck(:id, :journable_id, :user_id, :notes, :created_at)
  .map {{|id, wid, user_id, notes, created_at|
    {{
      id: id,
      wp_id: wid,
      user_id: user_id,
      notes: notes,
      created_at: created_at
    }}
  }}
{{journals: journals}}
"""


def _build_update_markers_script(
    updates: list[tuple[int, str]],
) -> str:
    """Ruby script: patch journal notes in bulk using update_columns.

    Uses ``update_columns`` (not ``save!``) so ActiveRecord callbacks are
    bypassed — no new journal version is created, and validity_period /
    data_type are left intact.

    Args:
        updates: List of ``(journal_id, new_notes_with_marker)`` tuples.
    """
    data = [{"id": jid, "notes": notes} for jid, notes in updates]
    data_json = json.dumps(data, ensure_ascii=False)
    return f"""
require 'json'
data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

updated = 0
data.each do |item|
  j = Journal.find_by(id: item['id'])
  if j
    j.update_columns(notes: item['notes'])
    updated += 1
  end
end
{{updated: updated}}
"""


# ---------------------------------------------------------------------------
# Jira comment fetching
# ---------------------------------------------------------------------------


def _default_fetch_jira_comments(jira_issue_key: str) -> list[dict[str, Any]]:
    """Fetch Jira comments for an issue key using the configured JiraClient.

    Returns a list of dicts with keys ``id``, ``author_account_id``, ``body``.
    """
    # Lazy import to avoid side effects during tests.
    from src.infrastructure.jira.jira_client import JiraClient  # noqa: PLC0415

    client = JiraClient()
    raw_comments = client.jira.comments(jira_issue_key)
    result = []
    for c in raw_comments:
        author = getattr(c, "author", None)
        account_id = ""
        if author is not None:
            account_id = getattr(author, "accountId", "") or ""
        result.append(
            {
                "id": getattr(c, "id", ""),
                "author_account_id": account_id,
                "body": getattr(c, "body", ""),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Core run() function
# ---------------------------------------------------------------------------


def run(
    wp_mapping: list[dict[str, Any]],
    user_mapping: dict[str, int],
    fetch_jira_comments: Any,  # callable(jira_issue_key) -> list[dict]
    op_client: Any,
    *,
    dry_run: bool,
    logger: logging.Logger,
) -> dict[str, int]:
    """Backfill provenance markers onto un-marked OP journals.

    Args:
        wp_mapping: List of dicts, each with keys ``jira_key``,
            ``openproject_id``, ``project_key``.
        user_mapping: ``{jira_account_id: op_user_id}`` dict.
        fetch_jira_comments: Callable ``(jira_issue_key: str) ->
            list[dict]`` — each dict has ``id``, ``author_account_id``,
            ``body``.  Injected so tests can mock without hitting Jira.
        op_client: OpenProjectClient (or any object with
            ``execute_query_to_json_file``).
        dry_run: When ``True`` no Rails writes are issued.
        logger: Configured logger instance.

    Returns:
        Dict with keys: ``wps_processed``, ``wps_skipped``,
        ``would_update`` (dry-run), ``updated`` (apply),
        ``skipped``, ``errors``.
    """
    mode = "DRY-RUN" if dry_run else "APPLY"
    logger.info("Backfill provenance markers  Mode: %s  WPs: %d", mode, len(wp_mapping))

    stats: dict[str, int] = {
        "wps_processed": 0,
        "skipped": 0,
        "would_update": 0,
        "updated": 0,
        "errors": 0,
    }

    for entry in wp_mapping:
        jira_key: str = entry["jira_key"]
        op_wp_id: int = int(entry["openproject_id"])

        # --- Fetch OP journals (unmarked) ---
        try:
            fetch_result = op_client.execute_query_to_json_file(_build_fetch_journals_script(op_wp_id))
        except Exception as exc:
            logger.error("WP#%d (%s): Rails fetch failed: %s", op_wp_id, jira_key, exc)
            stats["errors"] += 1
            continue

        if not isinstance(fetch_result, dict):
            logger.error("WP#%d (%s): unexpected fetch result type %r", op_wp_id, jira_key, type(fetch_result))
            stats["errors"] += 1
            continue

        op_journals: list[dict[str, Any]] = fetch_result.get("journals", [])

        # --- Fetch Jira comments ---
        try:
            jira_comments: list[dict[str, Any]] = fetch_jira_comments(jira_key)
        except Exception as exc:
            logger.error("WP#%d (%s): Jira fetch failed: %s", op_wp_id, jira_key, exc)
            stats["errors"] += 1
            continue

        # --- Pair journals with comments ---
        pairs, skip_reason = _pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )

        if skip_reason is not None:
            logger.warning(
                "WP#%d (%s): SKIP — %s",
                op_wp_id,
                jira_key,
                skip_reason,
            )
            stats["skipped"] += 1
            continue

        if not pairs:
            # All journals already marked — clean, nothing to do.
            logger.debug("WP#%d (%s): all journals already marked — no-op", op_wp_id, jira_key)
            stats["wps_processed"] += 1
            continue

        # --- Compute updates ---
        updates: list[tuple[int, str]] = []
        for op_j, jira_c in pairs:
            new_notes = op_j["notes"] + "\n" + _COMMENT_PROVENANCE_MARKER.format(jira_comment_id=jira_c["id"])
            updates.append((op_j["id"], new_notes))
            logger.info(
                "  [%s] WP#%d  Journal#%d  jira_comment_id=%s  marker=j2o:jira-comment-id:%s",
                "UPDATE" if not dry_run else "WOULD UPDATE",
                op_wp_id,
                op_j["id"],
                jira_c["id"],
                jira_c["id"],
            )

        stats["wps_processed"] += 1

        if dry_run:
            stats["would_update"] += len(updates)
            continue

        # --- Apply updates ---
        update_script = _build_update_markers_script(updates)
        try:
            update_result = op_client.execute_query_to_json_file(update_script)
            n = update_result.get("updated", 0) if isinstance(update_result, dict) else 0
            stats["updated"] += n
            logger.info("WP#%d (%s): updated %d journal(s)", op_wp_id, jira_key, n)
        except Exception as exc:
            logger.error("WP#%d (%s): update failed: %s", op_wp_id, jira_key, exc)
            stats["errors"] += 1

    logger.info(
        "Done.  processed=%d  skipped=%d  would_update=%d  updated=%d  errors=%d",
        stats["wps_processed"],
        stats["skipped"],
        stats["would_update"],
        stats["updated"],
        stats["errors"],
    )
    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _load_wp_mapping(data_dir: Path, project_key: str) -> list[dict[str, Any]]:
    """Load work_package_mapping.json and filter to the given project key."""
    mapping_path = data_dir / "work_package_mapping.json"
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping file not found: {mapping_path}")

    with mapping_path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = json.load(fh)

    result: list[dict[str, Any]] = []
    for outer_key, value in raw.items():
        if isinstance(value, dict):
            jira_key = value.get("jira_key", outer_key)
            op_id = value.get("openproject_id")
            pk = value.get("project_key", "")
        else:
            # Legacy int-value shape — skip (no jira_key recoverable)
            continue

        if not op_id:
            continue

        # Filter to requested project: match the issue key prefix.
        if not str(jira_key).upper().startswith(project_key.upper() + "-"):
            continue

        result.append(
            {
                "jira_key": str(jira_key),
                "openproject_id": int(op_id),
                "project_key": str(pk),
            }
        )

    return result


def _load_user_mapping(data_dir: Path) -> dict[str, int]:
    """Load user_mapping.json and return ``{jira_account_id: op_user_id}``."""
    mapping_path = data_dir / "user_mapping.json"
    if not mapping_path.exists():
        return {}

    with mapping_path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = json.load(fh)

    result: dict[str, int] = {}
    for jira_id, entry in raw.items():
        if isinstance(entry, dict):
            op_id = entry.get("openproject_id")
            if op_id:
                result[str(jira_id)] = int(op_id)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("project_key", help="Jira/OP project key (e.g. NRS)")
    apply_grp = parser.add_mutually_exclusive_group()
    apply_grp.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write provenance markers.  Default is dry-run.",
    )
    apply_grp.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Analyse and report only; do not write (this is the default).",
    )
    parser.add_argument(
        "--data-dir",
        default=str(_DEFAULT_DATA_DIR),
        help=f"Directory containing mapping JSON files (default: {_DEFAULT_DATA_DIR})",
    )
    args = parser.parse_args(argv)

    project_key = args.project_key.upper()
    if not _PROJECT_KEY_RE.match(project_key):
        sys.stderr.write(f"Invalid project key: {args.project_key!r}\n")
        return 2

    data_dir = Path(args.data_dir)
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path("var/logs") / f"backfill_comment_provenance_markers_{ts}.log"

    logger = _setup_logging(log_path)
    logger.info("Log: %s", log_path)

    try:
        wp_mapping = _load_wp_mapping(data_dir, project_key)
    except FileNotFoundError as exc:
        logger.error("Cannot load work_package_mapping: %s", exc)
        return 1
    except Exception as exc:
        logger.error("Failed to load work_package_mapping: %s", exc)
        return 1

    logger.info("Loaded %d WP entries for project %s", len(wp_mapping), project_key)

    user_mapping = _load_user_mapping(data_dir)
    logger.info("Loaded %d user mapping entries", len(user_mapping))

    from src.infrastructure.openproject.openproject_client import OpenProjectClient  # noqa: PLC0415

    op_client = OpenProjectClient()

    try:
        stats = run(
            wp_mapping=wp_mapping,
            user_mapping=user_mapping,
            fetch_jira_comments=_default_fetch_jira_comments,
            op_client=op_client,
            dry_run=not args.apply,
            logger=logger,
        )
    except Exception as exc:
        logger.error("Fatal: %s", exc)
        return 1

    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
