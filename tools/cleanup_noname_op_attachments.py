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
import re
import sys
from typing import Any

from src.infrastructure.openproject.openproject_client import OpenProjectClient

_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\z")


def _build_script(project_identifier: str, *, apply: bool) -> str:
    """Return the Ruby script to run.

    Output contract: emits a JSON envelope between
    ``$j2o_start_marker`` / ``$j2o_end_marker`` so
    ``execute_script_with_data`` / ``execute_json_query`` can
    consume it via the standard envelope path.
    """
    apply_literal = "true" if apply else "false"
    # ``project_identifier`` is regex-validated upstream; quoting
    # via single-quoted Ruby literal is safe for the allowed
    # ``[A-Z][A-Z0-9_]+`` shape.
    return f"""
require 'json'
data = (lambda do
  proj = Project.find_by(identifier: '{project_identifier.lower()}')
  next {{ error: "project not found", identifier: '{project_identifier.lower()}' }} unless proj

  wp_id_scope = WorkPackage.where(project_id: proj.id).select(:id)
  noname = Attachment.where(container_type: 'WorkPackage', container_id: wp_id_scope)
                     .where("LOWER(filename) = 'noname'")

  plan = []
  noname.each do |att|
    siblings = Attachment.where(container_type: 'WorkPackage', container_id: att.container_id)
                        .where("filename ~ '^jira-attachment-\\\\d+$'")
                        .pluck(:id, :filename)
    plan << {{
      attachment_id: att.id,
      wp_id: att.container_id,
      sibling_count: siblings.size,
      siblings_sample: siblings.first(3),
      will_delete: ({apply_literal} && siblings.size > 0),
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
    will_delete: plan.count {{ |r| r[:will_delete] }},
    skipped_no_sibling: plan.count {{ |r| !r[:will_delete] && {apply_literal} }},
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
    script = _build_script(args.project_key, apply=args.apply)
    envelope = op.execute_script_with_data(script, [])
    if not isinstance(envelope, dict) or envelope.get("status") != "success":
        sys.stderr.write(f"Rails call failed: {envelope!r}\n")
        return 1
    data: dict[str, Any] = envelope.get("data") or {}

    if args.apply:
        sys.stderr.write(
            f"DELETED {data.get('deleted', 0)} of {data.get('candidates', 0)} candidates"
            f" ({data.get('skipped_no_sibling', 0)} skipped — no jira-attachment-* sibling).\n",
        )
    else:
        sys.stderr.write(
            f"DRY-RUN: {data.get('candidates', 0)} candidate(s) found,"
            f" {data.get('will_delete', 0)} eligible for deletion."
            " Re-run with --apply to delete.\n",
        )

    import json

    sys.stdout.write(json.dumps(data, indent=2, default=str))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
