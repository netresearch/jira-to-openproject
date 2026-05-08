"""Delete legacy ``noname`` OP attachments left over from pre-#215 runs.

Before [PR #215](https://github.com/netresearch/jira-to-openproject/pull/215),
the migration uploaded Jira attachments whose Jira-side filename was
``noname`` (or blank) under that raw filename. PR #215 changed both
the upload pipeline and the recovery audit to convert
``noname``/blank → ``jira-attachment-{aid}``. Existing OP rows from
runs predating #215 still carry the literal ``noname`` filename and
have a ``jira-attachment-{aid}`` sibling for the same WP — making
them redundant duplicates.

This tool deletes those legacy rows safely:

1. Find OP attachments where ``filename = 'noname'`` (case-insensitive).
2. For each, verify the same WP holds at least one
   ``jira-attachment-<digits>`` attachment (the canonical replacement).
3. **Dry-run by default**: prints the deletion plan; pass
   ``--apply`` to actually delete.

Usage::

    .venv/bin/python -m tools.cleanup_noname_op_attachments NRS
    .venv/bin/python -m tools.cleanup_noname_op_attachments NRS --apply

The single-project filter (``Project.identifier``) keeps the blast
radius bounded — running it on ``NRS`` won't touch any other
project's attachments.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from src.infrastructure.openproject.openproject_client import OpenProjectClient

_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\z")


def _build_script(*, apply: bool) -> str:
    """Return the Ruby script to run.

    Output contract: emits a JSON envelope between
    ``$j2o_start_marker`` / ``$j2o_end_marker`` so
    ``execute_script_with_data`` / ``execute_json_query`` can
    consume it via the standard envelope path.

    The project identifier is **not** interpolated into the script
    here — it's passed via the ``execute_script_with_data`` data
    payload so the Ruby reads it from ``input_data`` rather than from
    a string-spliced literal. Per PR #220 review.

    The Ruby also pre-fetches sibling counts in ONE query (instead of
    one per ``noname`` row) — eliminates the N+1 the prior version
    had on projects with many leftover ``noname`` rows.
    """
    apply_literal = "true" if apply else "false"
    return f"""
require 'json'
data = (lambda do
  identifier = input_data['identifier']
  proj = Project.find_by(identifier: identifier)
  next {{ error: "project not found", identifier: identifier }} unless proj

  wp_id_scope = WorkPackage.where(project_id: proj.id).select(:id)
  noname = Attachment.where(container_type: 'WorkPackage', container_id: wp_id_scope)
                     .where("LOWER(filename) = 'noname'")

  # Per PR #220 review: fetch sibling counts in ONE query keyed by
  # container_id, instead of one query per ``noname`` row. The set
  # built here lets each plan row check membership in O(1).
  affected_wp_ids = noname.pluck(:container_id).uniq
  siblings_by_wp = Attachment
    .where(container_type: 'WorkPackage', container_id: affected_wp_ids)
    .where("filename ~ '^jira-attachment-\\\\d+$'")
    .pluck(:container_id, :id, :filename)
    .group_by(&:first)

  # NOTE: ``apply`` is captured at script-build time, not at run
  # time — flipping the dry-run / apply mode requires building a
  # fresh script. Per PR #220 review's safety hardening request:
  # ``eligible_for_deletion`` is reported regardless of mode so the
  # dry-run summary shows operators what *would* happen on apply.
  plan = []
  noname.each do |att|
    siblings = siblings_by_wp[att.container_id] || []
    eligible = siblings.size > 0
    plan << {{
      attachment_id: att.id,
      wp_id: att.container_id,
      sibling_count: siblings.size,
      siblings_sample: siblings.first(3).map {{ |_, sid, sfn| [sid, sfn] }},
      eligible_for_deletion: eligible,
      will_delete: ({apply_literal} && eligible),
    }}
  end

  deleted_ids = []
  if {apply_literal}
    plan.each do |row|
      next unless row[:will_delete]
      att = Attachment.find_by(id: row[:attachment_id])
      next unless att
      att.destroy!
      deleted_ids << row[:attachment_id]
    end
  end

  {{
    candidates: plan.size,
    eligible_for_deletion: plan.count {{ |r| r[:eligible_for_deletion] }},
    will_delete: plan.count {{ |r| r[:will_delete] }},
    skipped_no_sibling: plan.count {{ |r| !r[:eligible_for_deletion] }},
    deleted: deleted_ids.size,
    plan_sample: plan.first(20),
    deleted_ids: deleted_ids,
    apply_mode: {apply_literal},
  }}
end).call

start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'
end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'
puts start_marker
puts data.to_json
puts end_marker
"""


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_key", help="Jira project key (e.g. NRS) — used to find the OP project by identifier")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the noname rows. Default is a dry-run (plan only).",
    )
    args = parser.parse_args(argv)

    if not _PROJECT_KEY_RE.match(args.project_key):
        sys.stderr.write(f"Invalid project key: {args.project_key!r}\n")
        return 2

    op = OpenProjectClient()
    script = _build_script(apply=args.apply)
    envelope = op.execute_script_with_data(script, [{"identifier": args.project_key.lower()}])
    if not isinstance(envelope, dict) or envelope.get("status") != "success":
        sys.stderr.write(f"Rails call failed: {envelope!r}\n")
        return 1
    data: dict[str, Any] = envelope.get("data") or {}

    # Per PR #220 review: surface a Ruby-side error (e.g. "project not
    # found") to the operator AND non-zero exit, instead of letting
    # it slip past as a "0 candidates" success summary.
    if data.get("error"):
        sys.stderr.write(f"Audit error: {data['error']} (identifier={data.get('identifier')!r})\n")
        sys.stdout.write(json.dumps(data, indent=2, default=str))
        sys.stdout.write("\n")
        return 1

    if args.apply:
        sys.stderr.write(
            f"DELETED {data.get('deleted', 0)} of {data.get('candidates', 0)} candidates"
            f" ({data.get('skipped_no_sibling', 0)} skipped — no jira-attachment-* sibling).\n",
        )
    else:
        # Dry-run reports ``eligible_for_deletion`` (always populated)
        # rather than ``will_delete`` (gated on ``apply``) — without
        # this, the dry-run would always claim "0 eligible" even when
        # candidates have valid siblings. Per PR #220 review.
        sys.stderr.write(
            f"DRY-RUN: {data.get('candidates', 0)} candidate(s) found,"
            f" {data.get('eligible_for_deletion', 0)} eligible for deletion"
            f" ({data.get('skipped_no_sibling', 0)} skipped — no jira-attachment-* sibling)."
            " Re-run with --apply to delete.\n",
        )

    sys.stdout.write(json.dumps(data, indent=2, default=str))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
