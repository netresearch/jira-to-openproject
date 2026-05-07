"""Diagnose per-issue attachment loss between Jira and OpenProject.

The :mod:`tools.audit_migrated_project` script reports an aggregate
``Jira reports N, OP has M`` for attachments. When they differ (live
2026-05-07 NRS audit: -131), that summary doesn't say *which* issues
or *which* filenames are missing — operators are left to grep the
migration log or trial-and-error.

This tool enumerates the gap directly:

1. Iterate every Jira issue in the project (paginated).
2. For each issue, list Jira's attachments (filename + size).
3. Look up the matching OP work package via the persisted
   ``work_package_mapping``.
4. List OP's attachments on that work package.
5. Diff by filename and emit per-issue + aggregate counts:

   * ``missing_in_op`` — present in Jira, not in OP (real loss).
   * ``extra_in_op`` — present in OP, not in Jira (phantom /
     duplicate / pre-existing).

Usage::

    .venv/bin/python -m tools.diagnose_attachment_loss NRS

Output: structured JSON to stdout. The intended downstream is a
follow-up backfill (re-attach the missing files) or a remediation
runbook entry (Jira-side 404s — confirmed deleted source).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from collections import Counter
from pathlib import Path
from typing import Any

from src.infrastructure.openproject.openproject_client import OpenProjectClient

# Validated against argv before being interpolated into JQL — same
# guard as the audit tool to prevent quote injection from a malformed
# project key.
_JIRA_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\z")

# Hard cap on pagination — same defence the audit tool uses against
# a buggy upstream returning the same page repeatedly.
_PAGINATION_MAX_PAGES = 1000

# Default location of the persisted WP mapping. Override via CLI if
# the operator runs against an alternate data dir.
_DEFAULT_WP_MAPPING_FILE = Path("var/data/work_package_mapping.json")


def _read_attr(obj: Any, name: str) -> Any:
    """Dual-shape access (dict / SDK object)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _iter_jira_issues_with_attachments(jira_project_key: str) -> dict[str, list[dict[str, Any]]]:
    """Return ``{jira_key: [{filename, size, id}, ...]}`` for the project.

    Issues with no attachments are omitted from the result so the
    diagnostic output stays focused on actionable rows.
    """
    if not _JIRA_PROJECT_KEY_RE.match(jira_project_key):
        msg = f"invalid project key {jira_project_key!r} (expected uppercase Jira key)"
        raise ValueError(msg)

    from src.infrastructure.jira.jira_client import JiraClient

    out: dict[str, list[dict[str, Any]]] = {}
    jira = JiraClient()
    underlying = jira.jira
    if underlying is None:
        msg = "Jira client is not initialized — check J2O_JIRA_* env vars"
        raise RuntimeError(msg)
    page_size = 100
    start_at = 0
    jql = f'project = "{jira_project_key}"'
    for _ in range(_PAGINATION_MAX_PAGES):
        page = underlying.search_issues(
            jql,
            startAt=start_at,
            maxResults=page_size,
            fields="attachment",
            expand="",
        )
        if not page:
            break
        for issue in page:
            key = _read_attr(issue, "key")
            fields_obj = _read_attr(issue, "fields")
            if not key or fields_obj is None:
                continue
            atts = _read_attr(fields_obj, "attachment") or []
            entries: list[dict[str, Any]] = []
            for a in atts:
                filename = _read_attr(a, "filename")
                if not isinstance(filename, str) or not filename.strip():
                    continue
                entries.append(
                    {
                        "filename": filename,
                        "size": _read_attr(a, "size"),
                        "id": _read_attr(a, "id"),
                    },
                )
            if entries:
                out[str(key)] = entries
        start_at += len(page)
    else:
        sys.stderr.write(
            f"[diagnose] Jira pagination hit the {_PAGINATION_MAX_PAGES}"
            f"-page cap for project {jira_project_key!r} — likely a buggy"
            " upstream returning the same page repeatedly\n",
        )
    return out


def _load_wp_mapping(path: Path) -> dict[str, int]:
    """Return ``{jira_key: op_wp_id}`` from the persisted mapping file.

    Mirrors :meth:`AttachmentsMigration._wp_lookup_by_jira_key` so the
    diagnostic uses the same lookup the migration uses — any divergence
    here would silently miscount.
    """
    if not path.exists():
        msg = f"work_package mapping file not found at {path} (run work_packages_skeleton first)"
        raise FileNotFoundError(msg)
    with path.open() as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        msg = f"work_package mapping at {path} is not a dict (got {type(raw).__name__})"
        raise TypeError(msg)
    out: dict[str, int] = {}
    for outer_key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        inner_jira_key = entry.get("jira_key")
        jira_key = str(inner_jira_key or outer_key)
        op_id = entry.get("openproject_id")
        if op_id is None:
            continue
        if not isinstance(op_id, int):
            try:
                op_id = int(op_id)
            except TypeError, ValueError:
                continue
        out[jira_key] = op_id
    return out


def _build_op_query(wp_ids: list[int]) -> str:
    """Ruby that returns ``{wp_id: [{filename, id, size}]}`` for the WPs."""
    return f"""
(lambda do
  ids = {wp_ids!r}
  out = {{}}
  Attachment.where(container_type: 'WorkPackage', container_id: ids).
    pluck(:container_id, :filename, :id, :file_size).each do |cid, fn, id, sz|
      out[cid] ||= []
      out[cid] << {{ 'filename' => fn, 'id' => id, 'size' => sz }}
    end
  out
end).call
"""


def _fetch_op_attachments(op_client: OpenProjectClient, wp_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Return ``{op_wp_id: [{filename, id, size}]}`` for the given WPs.

    Batches large id lists to keep the Ruby script under the
    bind-parameter limits the audit tool also defends against.
    """
    batch_size = 500
    merged: dict[int, list[dict[str, Any]]] = {}
    for i in range(0, len(wp_ids), batch_size):
        batch = wp_ids[i : i + batch_size]
        script = _build_op_query(batch)
        result = op_client.execute_json_query(script, timeout=120)
        if not isinstance(result, dict):
            continue
        for k, v in result.items():
            try:
                wid = int(k)
            except TypeError, ValueError:
                continue
            if isinstance(v, list):
                merged[wid] = v
    return merged


def diagnose(jira_project_key: str, mapping_path: Path) -> dict[str, Any]:
    """Run the per-issue Jira ↔ OP attachment diff."""
    sys.stderr.write(f"[diagnose] reading WP mapping from {mapping_path}\n")
    wp_map = _load_wp_mapping(mapping_path)
    project_keys_in_mapping = {k for k in wp_map if k.startswith(f"{jira_project_key}-")}
    sys.stderr.write(
        f"[diagnose] {len(wp_map)} total WP entries; {len(project_keys_in_mapping)}"
        f" belong to project {jira_project_key!r}\n",
    )

    sys.stderr.write(f"[diagnose] enumerating Jira issues with attachments for {jira_project_key!r}…\n")
    jira_atts = _iter_jira_issues_with_attachments(jira_project_key)
    sys.stderr.write(
        f"[diagnose] found {len(jira_atts)} Jira issues with attachments;"
        f" {sum(len(v) for v in jira_atts.values())} attachments total\n",
    )

    # Resolve the WPs we'll look up on the OP side.
    relevant_wp_ids = sorted({wp_map[k] for k in jira_atts if k in wp_map})
    sys.stderr.write(f"[diagnose] fetching OP attachments for {len(relevant_wp_ids)} matching WPs…\n")
    op_client = OpenProjectClient()
    op_atts_by_wp = _fetch_op_attachments(op_client, relevant_wp_ids)

    per_issue: dict[str, dict[str, Any]] = {}
    summary: Counter[str] = Counter()
    missing_filenames_total: list[tuple[str, str]] = []
    extra_filenames_total: list[tuple[str, str]] = []

    for jira_key, jira_list in jira_atts.items():
        wp_id = wp_map.get(jira_key)
        jira_filenames = [a["filename"] for a in jira_list]
        if wp_id is None:
            per_issue[jira_key] = {
                "wp_id": None,
                "jira_count": len(jira_filenames),
                "op_count": 0,
                "missing_in_op": jira_filenames,
                "extra_in_op": [],
                "status": "wp_unmapped",
            }
            summary["wp_unmapped"] += 1
            for fn in jira_filenames:
                missing_filenames_total.append((jira_key, fn))
            continue

        op_list = op_atts_by_wp.get(wp_id, [])
        op_filenames = [a["filename"] for a in op_list]
        # Multiset diff so duplicate filenames (a real Jira pattern)
        # match correctly instead of collapsing to a set.
        jira_counter = Counter(jira_filenames)
        op_counter = Counter(op_filenames)
        missing_in_op_counter = jira_counter - op_counter
        extra_in_op_counter = op_counter - jira_counter
        missing_in_op = sorted(missing_in_op_counter.elements())
        extra_in_op = sorted(extra_in_op_counter.elements())

        if missing_in_op or extra_in_op:
            per_issue[jira_key] = {
                "wp_id": wp_id,
                "jira_count": len(jira_filenames),
                "op_count": len(op_filenames),
                "missing_in_op": missing_in_op,
                "extra_in_op": extra_in_op,
                "status": "diff",
            }
            if missing_in_op:
                summary["issues_with_missing"] += 1
            if extra_in_op:
                summary["issues_with_extra"] += 1
            for fn in missing_in_op:
                missing_filenames_total.append((jira_key, fn))
            for fn in extra_in_op:
                extra_filenames_total.append((jira_key, fn))
        else:
            summary["clean"] += 1

    return {
        "project_key": jira_project_key,
        "mapping_path": str(mapping_path),
        "summary": {
            "issues_examined": len(jira_atts),
            **dict(summary),
            "missing_attachments_total": len(missing_filenames_total),
            "extra_attachments_total": len(extra_filenames_total),
        },
        "per_issue_diffs": per_issue,
        "missing_filenames_sample": missing_filenames_total[:50],
        "extra_filenames_sample": extra_filenames_total[:50],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: write the per-issue attachment diff JSON to stdout."""
    parser = argparse.ArgumentParser(description="Diagnose per-issue attachment loss for a migrated Jira project")
    parser.add_argument("jira_key", help="Jira project key (e.g. NRS)")
    parser.add_argument(
        "--mapping",
        type=Path,
        default=_DEFAULT_WP_MAPPING_FILE,
        help=f"Path to the work_package_mapping.json (default: {_DEFAULT_WP_MAPPING_FILE})",
    )
    args = parser.parse_args(argv)

    try:
        report = diagnose(args.jira_key, args.mapping)
    except Exception as exc:
        sys.stderr.write(f"[diagnose] failed: {type(exc).__name__}: {exc}\n")
        sys.stderr.write(traceback.format_exc())
        return 2

    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    summary = report["summary"]
    sys.stderr.write(
        f"[diagnose] PASS={summary.get('clean', 0)},"
        f" MISSING_ISSUES={summary.get('issues_with_missing', 0)},"
        f" EXTRA_ISSUES={summary.get('issues_with_extra', 0)},"
        f" MISSING_FILES={summary['missing_attachments_total']},"
        f" EXTRA_FILES={summary['extra_attachments_total']}\n",
    )
    # Non-zero exit if any real loss is detected so CI / wrappers see it.
    return 1 if summary.get("missing_attachments_total", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
