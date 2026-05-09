"""Remove duplicate comment journals produced by non-idempotent migration runs.

Before the fix in PR #XXX (fix/wp-content-comment-idempotency) every
re-run of ``--components work_packages_content`` blindly INSERTed a fresh
set of comment journals for every work package, producing 2-3× duplicates.

WP 5040 (Jira NRS-4391) accumulates the canonical example:

    total comment-journals: 12  (Jira has 4)
    unique notes:            8
    by user:
        Anonymous (user_id=2): 8   ← May-7 broken runs (author not resolved)
        Björn Marten:          3   ← correct run
        Mikhail Sarnov:        1   ← correct run

This script deduplicates those journals **safely**:

1. For each work package in the project's mapping, query OP for all
   non-empty journals ordered by ``created_at``.
2. Group journals by their ``notes`` content (normalised to strip the
   provenance marker so a marker-carrying journal matches a plain one).
3. Within each duplicate group keep the journal with a **real** author
   (user_id != 2, i.e. not the Anonymous fallback) — or, if all
   duplicates have the same author, keep the newest.
4. Delete the others.
5. Log every deletion to stdout AND to
   ``var/logs/cleanup_anonymous_comment_duplicates_<ts>.log``.

Additionally, when ``--also-delete-orphan-anonymous`` is passed: for every
work package that has **both** real-author journals AND Anonymous journals,
Anonymous journals that are **older** than the earliest real-author journal
are deleted regardless of content similarity.  Anonymous journals that
post-date (or share the timestamp of) the earliest real-author journal are
kept — they may be legitimate comments from deactivated or unmapped Jira
users.  This tightened heuristic handles the case where the broken-run
renderer produced different text (e.g. ``concourse~~ci`` vs ``concourse-ci``)
so text-based dedup would not catch the duplicate, while avoiding false
deletions of legitimate newer Anonymous comments.

Defaults to ``--dry-run``.  Deletion requires explicit ``--apply``.
``--also-delete-orphan-anonymous`` is opt-in (not the default).

Usage::

    .venv/bin/python -m scripts.cleanup_anonymous_comment_duplicates NRS
    .venv/bin/python -m scripts.cleanup_anonymous_comment_duplicates NRS --dry-run
    .venv/bin/python -m scripts.cleanup_anonymous_comment_duplicates NRS --apply
    .venv/bin/python -m scripts.cleanup_anonymous_comment_duplicates NRS --apply --also-delete-orphan-anonymous

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

#: OpenProject user_id for the ``Anonymous`` / system fallback account.
ANONYMOUS_USER_ID = 2

#: Regex to strip the j2o provenance marker from notes for dedup matching.
_MARKER_RE = re.compile(r"\n?<!--\s*j2o:jira-comment-id:[^\s>]+\s*-->")

#: Minimum valid project key: 2+ uppercase letters/digits
_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\z")


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------


def _setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cleanup_anon_comments")
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
# Core logic
# ---------------------------------------------------------------------------


def _strip_marker(notes: str) -> str:
    """Remove provenance marker from notes for equality comparison."""
    return _MARKER_RE.sub("", notes).strip()


def _select_keeper(journals: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose the journal to KEEP from a group of duplicates.

    Selection priority:
    1. A journal with a real author (user_id != ANONYMOUS_USER_ID).
       If multiple real-author journals exist, prefer the one whose raw
       notes carry the j2o provenance marker (from the correctly-run
       migration) — if still tied, keep the newest.
    2. If all journals are Anonymous, keep the newest (to match what the
       fresh correct run would have written last).
    """
    real_author = [j for j in journals if j["user_id"] != ANONYMOUS_USER_ID]
    pool = real_author if real_author else journals

    # Among the pool, prefer marker-bearing journals (they came from the
    # correctly-fixed run) then fall back to newest.
    with_marker = [j for j in pool if "j2o:jira-comment-id:" in j["notes"]]
    candidates = with_marker if with_marker else pool

    # Newest by created_at (ISO string — lexicographic sort works fine)
    return max(candidates, key=lambda j: j["created_at"])


def _plan_deletions(
    journals: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (to_keep, to_delete) for a WP's journals.

    Groups by normalised notes content; for each group with >1 entry
    selects a keeper and marks the rest for deletion.
    """
    # Partition by normalised notes text
    groups: dict[str, list[dict[str, Any]]] = {}
    for j in journals:
        key = _strip_marker(j["notes"])
        groups.setdefault(key, []).append(j)

    to_keep: list[dict[str, Any]] = []
    to_delete: list[dict[str, Any]] = []

    for _key, group in groups.items():
        keeper = _select_keeper(group)
        to_keep.append(keeper)
        for j in group:
            if j["id"] != keeper["id"]:
                to_delete.append(j)

    return to_keep, to_delete


def _plan_orphan_anonymous_deletions(
    journals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return Anonymous journals from a WP that are OLDER than the earliest real-author journal.

    This tightened heuristic handles the case where broken-run Anonymous
    journals have *different* text from the correctly-migrated real-author
    journals (e.g. the old converter rendered ``concourse~~ci`` instead of
    ``concourse-ci``).  Text-based dedup misses these because the normalised
    notes don't match.

    The heuristic is sound because:
    - If a WP already has real-author journals the correct migration has run.
    - Anonymous journals that PRE-DATE the earliest real-author journal are
      artifacts of the pre-fix broken runs and can safely be deleted.
    - Anonymous journals that POST-DATE (or share the same timestamp as) the
      earliest real-author journal may be legitimate comments from deactivated
      or unmapped Jira users — they are KEPT.

    When a WP has NO real-author journals the heuristic does not apply —
    those Anonymous journals may be legitimately the only copies.

    Returns:
        List of Anonymous journals to delete (may be empty).
    """
    real_timestamps = [j["created_at"] for j in journals if j["user_id"] != ANONYMOUS_USER_ID]
    if not real_timestamps:
        # No real-author journals → heuristic does not apply.
        return []

    earliest_real = min(real_timestamps)
    # Only delete Anonymous journals that are strictly older than the earliest
    # real-author journal.  Journals at the same timestamp or newer are kept.
    return [j for j in journals if j["user_id"] == ANONYMOUS_USER_ID and j["created_at"] < earliest_real]


def _build_fetch_script(project_key: str) -> str:
    """Ruby script to fetch all non-empty journals for WPs in the project."""
    safe_key = project_key.upper()
    # Fetches all journals with non-empty notes for WPs in the project,
    # ordered by wp_id then created_at so duplicates are adjacent.
    return f"""
require 'json'
proj = Project.find_by(identifier: '{safe_key.lower()}')
if proj
  wp_ids = WorkPackage.where(project_id: proj.id).pluck(:id)
  journals = Journal
    .where(journable_type: 'WorkPackage', journable_id: wp_ids)
    .where.not(notes: [nil, ''])
    .order(:journable_id, :created_at)
    .pluck(:id, :journable_id, :user_id, :notes, :created_at)
    .map {{|id, wp_id, user_id, notes, created_at|
      {{
        id: id,
        wp_id: wp_id,
        user_id: user_id,
        notes: notes,
        created_at: created_at
      }}
    }}
  {{project_id: proj.id, wp_ids_count: wp_ids.length, journals: journals}}
else
  {{error: 'project not found', identifier: '{safe_key.lower()}'}}
end
"""


def _build_delete_script(journal_ids: list[int]) -> str:
    """Ruby script to delete a batch of journal IDs."""
    ids_json = json.dumps(journal_ids)
    return f"""
require 'json'
ids = {ids_json}
deleted = Journal.where(id: ids).delete_all
{{deleted: deleted}}
"""


def run(
    project_key: str,
    *,
    apply: bool,
    logger: logging.Logger,
    op_client: Any,
    also_delete_orphan_anonymous: bool = False,
) -> dict[str, int]:
    """Analyse and optionally clean up duplicate comment journals.

    Args:
        project_key: Jira/OP project key (e.g. ``NRS``).
        apply: When ``True`` deletions are executed; when ``False`` (default)
            the run is a dry-run — all analysis is performed but nothing is
            deleted.
        logger: Configured logger instance.
        op_client: OpenProjectClient (or any object with
            ``execute_query_to_json_file``).
        also_delete_orphan_anonymous: When ``True``, for every WP that has
            **both** real-author journals AND Anonymous journals, Anonymous
            journals that are **older** than the earliest real-author journal
            are deleted regardless of whether their text matches a real-author
            journal.  Anonymous journals newer-than or equal-to the earliest
            real-author journal are kept (they may be legitimate comments from
            deactivated/unmapped Jira users).  Defaults to ``False`` (opt-in).

    Returns:
        Dict with keys: ``wps_scanned``, ``duplicate_groups``, ``to_delete``,
        ``deleted`` (0 in dry-run mode), ``kept``.

    """
    mode = "APPLY" if apply else "DRY-RUN"
    orphan_flag = " +orphan-anon" if also_delete_orphan_anonymous else ""
    logger.info("Project: %s  Mode: %s%s", project_key, mode, orphan_flag)

    # Step 1: Fetch all non-empty journals
    logger.info("Fetching journals from OpenProject via Rails …")
    fetch_script = _build_fetch_script(project_key)
    try:
        result = op_client.execute_query_to_json_file(fetch_script)
    except Exception as exc:
        logger.error("Rails query failed: %s", exc)
        raise

    if isinstance(result, dict) and result.get("error"):
        logger.error("Rails returned error: %s", result["error"])
        raise RuntimeError(result["error"])

    if not isinstance(result, dict):
        logger.error("Unexpected result type: %r", type(result))
        raise TypeError(f"Expected dict, got {type(result)}")

    journals_raw: list[dict[str, Any]] = result.get("journals", [])
    wp_ids_count: int = result.get("wp_ids_count", 0)
    logger.info(
        "Found %d non-empty journals across %d work packages",
        len(journals_raw),
        wp_ids_count,
    )

    # Step 2: Group journals by WP, then plan deletions
    by_wp: dict[int, list[dict[str, Any]]] = {}
    for j in journals_raw:
        wp_id = j["wp_id"]
        by_wp.setdefault(wp_id, []).append(j)

    all_to_delete: list[dict[str, Any]] = []
    # Track IDs already scheduled for deletion to avoid double-counting when
    # both text-based dedup AND the orphan heuristic flag the same journal.
    to_delete_ids: set[int] = set()
    duplicate_group_count = 0

    for wp_id, wp_journals in by_wp.items():
        wp_to_delete: list[dict[str, Any]] = []

        # --- text-based dedup (always active) ---
        _kept, text_to_delete = _plan_deletions(wp_journals)
        for j in text_to_delete:
            if j["id"] not in to_delete_ids:
                wp_to_delete.append(j)
                to_delete_ids.add(j["id"])

        # Count duplicate groups (unique note texts with >1 copy for this WP)
        if text_to_delete:
            groups: dict[str, list[dict[str, Any]]] = {}
            for j in wp_journals:
                key = _strip_marker(j["notes"])
                groups.setdefault(key, []).append(j)
            duplicate_group_count += sum(1 for g in groups.values() if len(g) > 1)

        # --- orphan-anonymous heuristic (opt-in) ---
        if also_delete_orphan_anonymous:
            orphans = _plan_orphan_anonymous_deletions(wp_journals)
            for j in orphans:
                if j["id"] not in to_delete_ids:
                    wp_to_delete.append(j)
                    to_delete_ids.add(j["id"])

        for j in wp_to_delete:
            if j["user_id"] == ANONYMOUS_USER_ID and also_delete_orphan_anonymous:
                # Check whether this was already flagged by text-based dedup or
                # only by the orphan heuristic.
                text_delete_ids = {x["id"] for x in text_to_delete}
                if j["id"] not in text_delete_ids:
                    reason = "Anonymous-orphan (WP also has real-author journals)"
                else:
                    reason = "Anonymous-author duplicate"
            elif j["user_id"] == ANONYMOUS_USER_ID:
                reason = "Anonymous-author duplicate"
            else:
                reason = "duplicate"

            logger.info(
                "  [%s] WP#%d  Journal#%d  user_id=%d  created_at=%s  reason=%s",
                "DELETE" if apply else "WOULD DELETE",
                wp_id,
                j["id"],
                j["user_id"],
                j["created_at"],
                reason,
            )
            all_to_delete.append(j)

    total_journals = len(journals_raw)
    total_to_delete = len(all_to_delete)
    logger.info(
        "Summary: %d journals scanned, %d WPs with deletions planned, %d journals to delete",
        total_journals,
        len({j["wp_id"] for j in all_to_delete}),
        total_to_delete,
    )

    if total_to_delete == 0:
        logger.info("Nothing to do — no duplicate journals found.")
        return {
            "wps_scanned": wp_ids_count,
            "duplicate_groups": 0,
            "to_delete": 0,
            "deleted": 0,
            "kept": total_journals,
        }

    # Step 3: Delete if --apply
    deleted = 0
    if apply:
        delete_ids = [j["id"] for j in all_to_delete]
        # Delete in batches of 100 to avoid overly large SQL IN clauses
        batch_size = 100
        for i in range(0, len(delete_ids), batch_size):
            batch = delete_ids[i : i + batch_size]
            del_script = _build_delete_script(batch)
            try:
                del_result = op_client.execute_query_to_json_file(del_script)
                n = del_result.get("deleted", 0) if isinstance(del_result, dict) else 0
                deleted += n
                logger.info("Deleted batch %d–%d: %d rows", i + 1, i + len(batch), n)
            except Exception as exc:
                logger.error("Delete batch %d–%d failed: %s", i + 1, i + len(batch), exc)
        logger.info("DONE: deleted %d duplicate journals", deleted)
    else:
        logger.info("DRY-RUN complete — pass --apply to delete %d journals", total_to_delete)

    return {
        "wps_scanned": wp_ids_count,
        "duplicate_groups": duplicate_group_count,
        "to_delete": total_to_delete,
        "deleted": deleted,
        "kept": total_journals - deleted,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_key", help="Jira project key (e.g. NRS)")
    apply_grp = parser.add_mutually_exclusive_group()
    apply_grp.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually delete duplicate journals. Default is dry-run.",
    )
    apply_grp.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Analyse and report only; do not delete (this is the default).",
    )
    parser.add_argument(
        "--also-delete-orphan-anonymous",
        action="store_true",
        default=False,
        dest="also_delete_orphan_anonymous",
        help=(
            "For every WP that has both real-author journals AND Anonymous journals, "
            "delete Anonymous journals that are OLDER than the earliest real-author "
            "journal, regardless of content similarity.  "
            "Anonymous journals newer-than or equal-to the earliest real-author "
            "journal are kept (may be legitimate comments from deactivated/unmapped "
            "Jira users).  "
            "This handles the case where broken-run renderers produced different text "
            "so text-based dedup misses the duplicates.  "
            "Off by default — operator must explicitly opt in."
        ),
    )
    args = parser.parse_args(argv)

    project_key = args.project_key.upper()
    if not _PROJECT_KEY_RE.match(project_key):
        sys.stderr.write(f"Invalid project key: {args.project_key!r}\n")
        return 2

    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path("var/logs") / f"cleanup_anonymous_comment_duplicates_{ts}.log"

    logger = _setup_logging(log_path)
    logger.info("Log: %s", log_path)

    # Import here to avoid import-time side effects from config loading when
    # running tests that patch this module.
    from src.infrastructure.openproject.openproject_client import OpenProjectClient

    op_client = OpenProjectClient()

    try:
        stats = run(
            project_key,
            apply=args.apply,
            logger=logger,
            op_client=op_client,
            also_delete_orphan_anonymous=args.also_delete_orphan_anonymous,
        )
    except Exception as exc:
        logger.error("Fatal: %s", exc)
        return 1

    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
