# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

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
import hashlib
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
    # Guard against duplicate handlers on re-invocation (e.g. repeated calls
    # from tests or reloads).  If handlers are already attached, skip adding
    # new ones to avoid log-line duplication.
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
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


# Canonical probe order for resolving a Jira comment author to an OP user.
# Single source of truth: maps the internal comment-dict key to the
# attribute name on the Jira SDK ``author`` object.  Used both to extract
# fields in ``_make_jira_fetcher`` AND to probe in
# ``_resolve_comment_author``, so adding a new probe key is a one-line
# change.
#
# Order mirrors ``IssueTransformer._JOURNAL_AUTHOR_PROBE_KEYS``:
#   accountId    → author_account_id   (Cloud only)
#   name         → author_name         (Server/DC login)
#   key          → author_key          (Server/DC internal key, e.g. JIRAUSER12345)
#   emailAddress → author_email
#   displayName  → author_display_name
_COMMENT_AUTHOR_FIELDS: tuple[tuple[str, str], ...] = (
    ("author_account_id", "accountId"),
    ("author_name", "name"),
    ("author_key", "key"),
    ("author_email", "emailAddress"),
    ("author_display_name", "displayName"),
)
_COMMENT_AUTHOR_PROBE_FIELDS: tuple[str, ...] = tuple(internal for internal, _ in _COMMENT_AUTHOR_FIELDS)


def _resolve_comment_author(
    jira_comment: dict[str, Any],
    user_mapping: dict[str, int],
) -> int | None:
    """Resolve a Jira comment's author to an OP user id.

    Probes each author field in canonical order (Cloud ``accountId`` first,
    then Server/DC ``name`` and ``key``, then ``emailAddress``,
    ``displayName``).  Returns the first matching OP user id or ``None``
    when the author cannot be resolved.

    Args:
        jira_comment: Comment dict as returned by :func:`_make_jira_fetcher`.
            Must contain the ``author_*`` fields populated by that function.
        user_mapping: Flat ``{identifier: op_user_id}`` dict built by
            :func:`_load_user_mapping`.  Indexed by every probe value so a
            single dict lookup per probe is O(1).

    Returns:
        The resolved ``openproject_id`` (int) or ``None``.
    """
    for field in _COMMENT_AUTHOR_PROBE_FIELDS:
        value = jira_comment.get(field)
        if not value:
            continue
        op_user_id = user_mapping.get(str(value))
        if op_user_id is not None:
            return op_user_id
    return None


def _pair_journals_with_comments(
    op_journals: list[dict[str, Any]],
    jira_comments: list[dict[str, Any]],
    user_mapping: dict[str, int],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], str | None]:
    """Pair OP journals with Jira comments by chronological order.

    All-or-nothing semantics: if any OP journal already carries a provenance
    marker the whole WP is considered partially backfilled and skipped.
    Partial backfill cannot be safely completed by positional pairing because
    the already-marked journal's position shifts all subsequent pairings.

    Args:
        op_journals: OP journal dicts with keys ``id``, ``wp_id``,
            ``user_id``, ``notes``, ``created_at``.
        jira_comments: Jira comment dicts with keys ``id``,
            ``author_account_id``, ``author_name``, ``author_key``,
            ``author_email``, ``author_display_name``, ``body``.  Must be in
            chronological order (Jira REST API default).
        user_mapping: Flat ``{identifier: op_user_id}`` dict built by
            :func:`_load_user_mapping`.  Each Jira user is indexed by every
            probe field (``jira_key``, ``jira_name``, ``jira_display_name``,
            ``jira_email``) so a single dict lookup per probe is O(1).

    Returns:
        ``(pairs, skip_reason)`` where ``pairs`` is a list of
        ``(op_journal, jira_comment)`` tuples and ``skip_reason`` is ``None``
        on success or a descriptive string when the WP must be skipped.
        When ``skip_reason`` is set ``pairs`` is always ``[]``.
    """
    marked = [j for j in op_journals if _MARKER_RE.search(j["notes"])]
    unmarked = [j for j in op_journals if not _MARKER_RE.search(j["notes"])]

    # If all journals are already marked: nothing to do, not an error.
    if not unmarked:
        return [], None

    # Partial backfill: some marked, some not.  Cannot safely pair by position
    # because the marked journal occupies a slot in the ordinal sequence.
    # Skip with a clear diagnostic rather than a misleading "count mismatch".
    if marked:
        return [], (
            f"partially backfilled: {len(marked)} journal(s) already marked, "
            f"{len(unmarked)} unmarked — skipping (all-or-nothing policy); "
            f"re-run after manually removing or verifying existing markers"
        )

    # Count check: we need a 1:1 match.
    if len(unmarked) != len(jira_comments):
        return [], (f"count mismatch: {len(unmarked)} unmarked OP journals vs {len(jira_comments)} Jira comments")

    # Author validation: zip by position (both are in chronological order).
    # Probe author fields in canonical order (Cloud accountId first, then
    # Server/DC name/key, then emailAddress, displayName) so this works for
    # both Jira Cloud and Jira Server deployments.
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for idx, (op_j, jira_c) in enumerate(zip(unmarked, jira_comments, strict=True)):
        expected_op_user_id = _resolve_comment_author(jira_c, user_mapping)
        actual_op_user_id = op_j.get("user_id")
        if expected_op_user_id is None or expected_op_user_id != actual_op_user_id:
            # Build a compact representation of the probed author fields for
            # the diagnostic (mirrors the old "account {account_id!r}" line).
            probed = {f: jira_c.get(f) for f in _COMMENT_AUTHOR_PROBE_FIELDS if jira_c.get(f)}
            return [], (
                f"author mismatch at position {idx}: "
                f"OP journal #{op_j['id']} has user_id={actual_op_user_id} "
                f"but Jira comment {jira_c['id']} (author fields {probed!r}) "
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
  .where(restricted: false)
  .order(:created_at, :id)
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
    updates: list[tuple[int, str, str]],
) -> str:
    """Ruby script: patch journal notes in bulk using update_columns.

    Uses ``update_columns`` (not ``save!``) so ActiveRecord callbacks are
    bypassed — no new journal version is created, and validity_period /
    data_type are left intact.

    Args:
        updates: Journal IDs, proposed markers and hashes of the original notes.
    """
    data = [{"id": jid, "marker": marker, "digest": digest} for jid, marker, digest in updates]
    data_json = json.dumps(data, ensure_ascii=False)
    return f"""
require 'json'
require 'digest'
data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

updated = 0
data.each do |item|
  j = Journal.find_by(id: item['id'], journable_type: 'WorkPackage', restricted: false)
  next unless j
  j.with_lock do
    next if j.restricted? || Digest::SHA256.hexdigest(j.notes.to_s) != item['digest']
    j.update_columns(notes: j.notes + "\n" + item['marker'])
    updated += 1
  end
end
{{updated: updated}}
"""


# ---------------------------------------------------------------------------
# Jira comment fetching
# ---------------------------------------------------------------------------


def _make_jira_fetcher(jira_client: Any) -> Any:
    """Return a ``fetch_jira_comments`` callable bound to the given JiraClient.

    The returned callable has the signature
    ``(jira_issue_key: str) -> list[dict]`` expected by :func:`run`.
    Constructing the client once and binding it here avoids per-call
    reconnection overhead.

    The returned comment dicts carry all author-identifying fields so that
    :func:`_pair_journals_with_comments` can probe them in canonical order
    (``accountId`` first for Cloud, then ``name`` / ``key`` for Server/DC).
    """

    def _fetch(jira_issue_key: str) -> list[dict[str, Any]]:
        selection = jira_client.select_comments_for_migration(jira_issue_key)
        if selection.held:
            msg = (
                "Cannot pair public journals by position when Jira has comments with unverified or restricted audiences"
            )
            raise ValueError(msg)
        raw_comments = selection.comments
        result = []
        for c in raw_comments:
            author = getattr(c, "author", None)
            entry: dict[str, Any] = {
                "id": getattr(c, "id", ""),
            }
            for internal, attr in _COMMENT_AUTHOR_FIELDS:
                entry[internal] = (getattr(author, attr, "") or "") if author is not None else ""
            result.append(entry)
        return result

    return _fetch


def _default_fetch_jira_comments(jira_issue_key: str) -> list[dict[str, Any]]:
    """Fetch Jira comments for an issue key, constructing a JiraClient each call.

    .. deprecated::
        Prefer :func:`_make_jira_fetcher` with a shared client instance.
        This function exists for backward compatibility; ``main()`` uses
        :func:`_make_jira_fetcher` to avoid per-call reconnection overhead.
    """
    # Lazy import to avoid side effects during tests.
    from src.infrastructure.jira.jira_client import JiraClient  # noqa: PLC0415

    client = JiraClient()
    return _make_jira_fetcher(client)(jira_issue_key)


# ---------------------------------------------------------------------------
# Core run() function
# ---------------------------------------------------------------------------


def _plan_work_package_markers(entry, user_mapping, fetch_jira_comments, op_client, *, dry_run, logger, stats):
    jira_key: str = entry["jira_key"]
    op_wp_id: int = int(entry["openproject_id"])

    # --- Fetch OP journals (unmarked) ---
    try:
        fetch_result = op_client.execute_query_to_json_file(_build_fetch_journals_script(op_wp_id))
    except Exception as exc:
        logger.warning("WP#%d (%s): Rails fetch failed (%s)", op_wp_id, jira_key, type(exc).__name__)
        stats["errors"] += 1
        return []

    if not isinstance(fetch_result, dict):
        logger.error("WP#%d (%s): unexpected fetch result type %r", op_wp_id, jira_key, type(fetch_result))
        stats["errors"] += 1
        return []

    op_journals: list[dict[str, Any]] = fetch_result.get("journals", [])

    # --- Fetch Jira comments ---
    try:
        jira_comments: list[dict[str, Any]] = fetch_jira_comments(jira_key)
    except Exception as exc:
        logger.warning("WP#%d (%s): Jira fetch failed (%s)", op_wp_id, jira_key, type(exc).__name__)
        stats["errors"] += 1
        return []

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
        stats["wps_skipped"] += 1
        return []

    # --- Compute updates ---
    updates: list[tuple[int, str, str]] = []
    for op_j, jira_c in pairs:
        marker = _COMMENT_PROVENANCE_MARKER.format(jira_comment_id=jira_c["id"])
        digest = hashlib.sha256(op_j["notes"].encode("utf-8")).hexdigest()
        updates.append((op_j["id"], marker, digest))
        logger.info(
            "  [%s] WP#%d  Journal#%d  jira_comment_id=%s  marker=j2o:jira-comment-id:%s",
            "UPDATE" if not dry_run else "WOULD UPDATE",
            op_wp_id,
            op_j["id"],
            jira_c["id"],
            jira_c["id"],
        )

    stats["wps_processed"] += 1

    return updates


def _apply_marker_batches(all_updates, op_client, logger, stats):
    _batch_size = 200
    for i in range(0, len(all_updates), _batch_size):
        batch = all_updates[i : i + _batch_size]
        update_script = _build_update_markers_script(batch)
        try:
            update_result = op_client.execute_query_to_json_file(update_script)
            n = update_result.get("updated", 0) if isinstance(update_result, dict) else 0
            stats["updated"] += n
            if n != len(batch):
                logger.error("Journal batch changed after planning: expected %d updates, got %d", len(batch), n)
                stats["errors"] += 1
            logger.info(
                "Batch update %d–%d: updated %d journal(s)",
                i + 1,
                i + len(batch),
                n,
            )
        except Exception as exc:
            logger.warning("Batch update %d–%d failed (%s)", i + 1, i + len(batch), type(exc).__name__)
            stats["errors"] += 1


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
        ``would_update`` (dry-run), ``updated`` (apply), ``errors``.
    """
    mode = "DRY-RUN" if dry_run else "APPLY"
    logger.info("Backfill provenance markers  Mode: %s  WPs: %d", mode, len(wp_mapping))

    stats: dict[str, int] = {
        "wps_processed": 0,
        "wps_skipped": 0,
        "would_update": 0,
        "updated": 0,
        "errors": 0,
    }

    # Accumulate journal IDs, markers and content hashes across WPs for a single
    # batched Rails write at the end (issue #4 — reduce Rails round-trips).
    all_updates: list[tuple[int, str, str]] = []

    for entry in wp_mapping:
        updates = _plan_work_package_markers(
            entry,
            user_mapping,
            fetch_jira_comments,
            op_client,
            dry_run=dry_run,
            logger=logger,
            stats=stats,
        )
        if dry_run:
            stats["would_update"] += len(updates)
        else:
            all_updates.extend(updates)
    _apply_marker_batches(all_updates, op_client, logger, stats)

    logger.info(
        "Done.  processed=%d  skipped=%d  would_update=%d  updated=%d  errors=%d",
        stats["wps_processed"],
        stats["wps_skipped"],
        stats["would_update"],
        stats["updated"],
        stats["errors"],
    )
    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _mapping_path(data_dir: Path, filename: str) -> Path:
    """Read migration mappings from regular files in the selected local data directory."""
    root = data_dir.resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or (path.exists() and not path.is_file()):
        msg = f"Invalid local mapping file: {filename}"
        raise ValueError(msg)
    return path


def _mapping_object(mapping_path: Path) -> dict[str, Any]:
    with mapping_path.open(encoding="utf-8") as stream:
        mapping = json.load(stream)
    if not isinstance(mapping, dict):
        msg = f"Expected a JSON object in {mapping_path.name}"
        raise ValueError(msg)
    return mapping


def _load_wp_mapping(data_dir: Path, project_key: str) -> list[dict[str, Any]]:
    """Load work_package_mapping.json and filter to the given project key."""
    mapping_path = _mapping_path(data_dir, "work_package_mapping.json")
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping file not found: {mapping_path}")

    raw = _mapping_object(mapping_path)

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
    """Load user_mapping.json and return a multi-keyed ``{identifier: op_user_id}`` dict.

    ``user_mapping.json`` is keyed by display name (outer key), but Jira
    comment authors are identified by Jira Server *key* (e.g. ``JIRAUSER12345``
    or ``ID12021``), login name, email address, or display name.

    This function builds a lookup indexed by every probe field present on each
    entry, following the canonical probe order from
    ``IssueTransformer._JOURNAL_AUTHOR_PROBE_KEYS``:
    ``jira_key`` → ``jira_name`` → ``jira_display_name`` → ``jira_email``.

    The outer key (display name or jira_key) is also indexed so that mappings
    whose outer key happens to be the jira_key (e.g. ``"JIRAUSER13400"``) are
    still resolvable.

    Later insertions do not overwrite earlier ones so the higher-priority probe
    fields win in the rare case two entries share a secondary field value.
    """
    mapping_path = _mapping_path(data_dir, "user_mapping.json")
    if not mapping_path.exists():
        return {}

    raw = _mapping_object(mapping_path)

    # Probe fields in priority order (mirrors IssueTransformer._JOURNAL_AUTHOR_PROBE_KEYS
    # mapped to the user_mapping.json entry field names).
    _PROBE_ENTRY_FIELDS: tuple[str, ...] = (
        "jira_key",  # Server/DC key (e.g. JIRAUSER12345, ID12021)
        "jira_name",  # Server login name (e.g. anne.geissler)
        "jira_display_name",  # Display name
        "jira_email",  # Email address
    )

    result: dict[str, int] = {}

    for outer_key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        op_id = entry.get("openproject_id")
        if not op_id:
            continue
        op_user_id = int(op_id)

        # Index by each probe field in priority order; skip already-set keys
        # so higher-priority fields win over lower-priority ones.
        for field in _PROBE_ENTRY_FIELDS:
            value = entry.get(field)
            if value and str(value) not in result:
                result[str(value)] = op_user_id

        # Also index by the outer key (may be display name or jira_key).
        if outer_key and outer_key not in result:
            result[outer_key] = op_user_id

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
        user_mapping = _load_user_mapping(data_dir)
    except OSError, ValueError, TypeError:
        logger.exception("Cannot load migration mappings")
        return 1

    logger.info("Loaded %d WP entries for project %s", len(wp_mapping), project_key)

    logger.info("Loaded %d user mapping entries", len(user_mapping))

    from src.infrastructure.jira.jira_client import JiraClient  # noqa: PLC0415
    from src.infrastructure.openproject.openproject_client import OpenProjectClient  # noqa: PLC0415

    # Construct clients once; binding the Jira client to a fetcher closure
    # avoids per-WP reconnection overhead.
    jira_client = JiraClient()
    op_client = OpenProjectClient()

    try:
        stats = run(
            wp_mapping=wp_mapping,
            user_mapping=user_mapping,
            fetch_jira_comments=_make_jira_fetcher(jira_client),
            op_client=op_client,
            dry_run=not args.apply,
            logger=logger,
        )
    except Exception as exc:
        logger.warning("Comment repair stopped (%s)", type(exc).__name__)
        return 1

    print(json.dumps(stats, indent=2))
    return int(bool(stats["errors"]))


if __name__ == "__main__":
    sys.exit(main())
