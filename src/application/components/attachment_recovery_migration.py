"""Recover Jira attachments that didn't make it into OP after the
``attachments`` component ran.

Live 2026-05-07 NRS audit: ``Jira reports 4572, OP has 4441 (-131)``.
The ``attachments`` migration is idempotent on the Rails side — a
file with the same filename already attached to a WP is silently
skipped — so a re-run can't fill in the missing 131 unless we tell
it *which* issues are short. Causes of the gap fall into three
buckets:

1. **Issues skipped mid-batch** — a transient Jira / SSH / Rails
   error during ``_load`` aborted that batch; the partial-success
   classifier marked the run green and the skipped files were never
   re-attempted.
2. **Files that 404'd on Jira** — the Jira metadata still lists the
   attachment but the file itself was deleted. No fix; we record it
   under ``still_missing`` for operator triage.
3. **Filename collisions** — the Rails idempotency check uses
   ``LOWER(filename)``; two distinct Jira files with the same
   filename collapse to one OP attachment.

This component runs after ``attachments`` and ``attachment_provenance``
in :data:`DEFAULT_COMPONENT_SEQUENCE`. It:

* enumerates Jira's per-issue attachment list (filename + size + id);
* enumerates OP's per-WP attachment list via a single batched Rails
  query;
* builds the per-issue diff with multiset semantics so duplicate
  filenames match correctly;
* delegates the actual re-attach to
  :meth:`AttachmentsMigration._process_batch_end_to_end` for the
  Jira keys that still have missing files — same path the original
  attachments migration uses, so any future fix to the transfer /
  Rails registration code applies here without duplication.

Idempotent: re-runs only re-attempt files genuinely missing in OP.
On a clean instance this component is a fast no-op.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from src.application.components.attachments_migration import AttachmentsMigration
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult

# Validated against jira_project_key before being interpolated into JQL —
# same regex guard ``audit_migrated_project`` uses to prevent quote
# injection from a malformed project key.
_JIRA_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\z")

# Hard cap on pagination — same defence the audit tool uses against a
# buggy upstream returning the same page repeatedly.
_PAGINATION_MAX_PAGES = 1000


@register_entity_types("attachment_recovery")
class AttachmentRecoveryMigration(BaseMigration):
    """Phase: per-issue diagnose + recover missing OP attachments."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        msg = (
            "AttachmentRecoveryMigration is a transformation-only migration"
            " and does not support idempotent workflow. It diffs Jira ↔ OP"
            " state at runtime."
        )
        raise ValueError(msg)

    @staticmethod
    def _read_attr(obj: Any, name: str) -> Any:
        """Dual-shape access (dict / SDK object). Mirrors the helper in
        :mod:`relation_migration` and the audit tool.
        """
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def _wp_lookup_by_jira_key(self) -> dict[str, int]:
        """Reuse :class:`AttachmentsMigration`'s normalised lookup so this
        component sees exactly the WPs the migration intended to process.

        Avoids a divergence-bug class where the recovery uses a
        slightly-different WP map than the migration and ends up
        re-attaching against a different WP than the original load.
        """
        # Don't call ``__init__`` — we don't need the helper's
        # attachment dir / cache; just the method.
        helper = AttachmentsMigration.__new__(AttachmentsMigration)
        helper.mappings = self.mappings
        helper._wp_lookup_cache = None
        return helper._wp_lookup_by_jira_key()

    def _project_keys_in_scope(self) -> list[str]:
        """Project keys present in the WP mapping (after the ``inner.jira_key`` /
        outer-fallback normalisation). Used to scope the Jira-side
        pagination query without requiring CLI flags.
        """
        prefixes: set[str] = set()
        for k in self._wp_lookup_by_jira_key():
            head = k.split("-", 1)[0]
            if _JIRA_PROJECT_KEY_RE.match(head):
                prefixes.add(head)
        return sorted(prefixes)

    def _iter_jira_issues_with_attachments(
        self,
        jira_project_key: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return ``{jira_key: [{filename, size, id, url}, …]}`` for the
        project. Issues with zero attachments are omitted.

        Uses the same paginated SDK call the migration's ``_extract_batch``
        relies on; no separate query path so a future Jira-side change
        affects both consistently.
        """
        if not _JIRA_PROJECT_KEY_RE.match(jira_project_key):
            self.logger.warning(
                "Recovery skipping invalid project key %r",
                jira_project_key,
            )
            return {}

        underlying = getattr(self.jira_client, "jira", None)
        if underlying is None:
            self.logger.warning(
                "JiraClient.jira is None — Jira not initialised; recovery for project %r skipped",
                jira_project_key,
            )
            return {}

        out: dict[str, list[dict[str, Any]]] = {}
        page_size = 100
        start_at = 0
        jql = f'project = "{jira_project_key}"'
        for _ in range(_PAGINATION_MAX_PAGES):
            try:
                page = underlying.search_issues(
                    jql,
                    startAt=start_at,
                    maxResults=page_size,
                    fields="attachment",
                    expand="",
                )
            except Exception:
                self.logger.exception(
                    "Recovery aborted enumerating Jira attachments for %r",
                    jira_project_key,
                )
                return out
            if not page:
                break
            for issue in page:
                key = self._read_attr(issue, "key")
                fields_obj = self._read_attr(issue, "fields")
                if not key or fields_obj is None:
                    continue
                atts = self._read_attr(fields_obj, "attachment") or []
                entries: list[dict[str, Any]] = []
                for a in atts:
                    filename = self._read_attr(a, "filename")
                    if not isinstance(filename, str) or not filename.strip():
                        continue
                    entries.append(
                        {
                            "filename": filename,
                            "size": self._read_attr(a, "size"),
                            "id": self._read_attr(a, "id"),
                            "url": self._read_attr(a, "content"),
                        },
                    )
                if entries:
                    out[str(key)] = entries
            start_at += len(page)
        else:
            self.logger.warning(
                "Recovery hit %d-page safety cap on project %r",
                _PAGINATION_MAX_PAGES,
                jira_project_key,
            )
        return out

    @staticmethod
    def _build_op_attachment_query(wp_ids: list[int]) -> str:
        """Ruby that returns ``{wp_id: [filename, ...]}`` for the WPs.

        Output contract: same start/end markers ``execute_script_with_data``
        consumes. ``puts``-emitted JSON ends up under ``envelope['data']``.
        """
        return f"""
require 'json'
start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'
end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'
ids = {wp_ids!r}
out = {{}}
Attachment.where(container_type: 'WorkPackage', container_id: ids).
  pluck(:container_id, :filename).each do |cid, fn|
    out[cid] ||= []
    out[cid] << fn
end
puts start_marker
puts out.to_json
puts end_marker
"""

    def _fetch_op_attachments_by_wp(self, wp_ids: list[int]) -> dict[int, list[str]]:
        """Return ``{op_wp_id: [filename, …]}`` for the WPs in ``wp_ids``.

        Batches large id lists to keep the Ruby script under the
        bind-parameter limits the audit tool also defends against. The
        Rails envelope is parsed: ``status`` → success/failure, payload
        from ``envelope['data']`` (NOT the top-level keys — same
        envelope-bug class PR #201 caught for ``wp_metadata_backfill``).
        """
        batch_size = 500
        merged: dict[int, list[str]] = {}
        for i in range(0, len(wp_ids), batch_size):
            batch = wp_ids[i : i + batch_size]
            script = self._build_op_attachment_query(batch)
            try:
                envelope = self.op_client.execute_script_with_data(script, [])
            except Exception:
                self.logger.exception(
                    "Recovery: Rails fetch of OP attachments failed for batch starting at %d",
                    i,
                )
                continue
            if not isinstance(envelope, dict):
                continue
            if envelope.get("status") != "success":
                self.logger.warning(
                    "Recovery: Rails returned status=%r message=%r — batch skipped",
                    envelope.get("status"),
                    envelope.get("message"),
                )
                continue
            data = envelope.get("data") or {}
            if not isinstance(data, dict):
                continue
            for k, v in data.items():
                try:
                    wid = int(k)
                except TypeError, ValueError:
                    continue
                if isinstance(v, list):
                    merged[wid] = [str(fn) for fn in v if isinstance(fn, str)]
        return merged

    def run(self) -> ComponentResult:  # type: ignore[override]
        self.logger.info("Starting attachment recovery (per-issue diff + targeted re-attach)")

        wp_map = self._wp_lookup_by_jira_key()
        if not wp_map:
            # Same fail-loud pattern as siblings (#194/#197/#198/#199).
            msg = (
                "No usable work_package mapping — recovery cannot run."
                " Run work_packages_skeleton first (or back-fill the"
                " jira_key on legacy rows)."
            )
            self.logger.error(msg)
            return ComponentResult(
                success=False,
                message=msg,
                errors=["missing_work_package_mapping"],
            )

        project_keys = self._project_keys_in_scope()
        if not project_keys:
            return ComponentResult(
                success=True,
                updated=0,
                message="No project keys derivable from work_package mapping; nothing to recover.",
                details={"projects_examined": 0},
            )

        # Gather Jira side first — paginated per project.
        jira_atts: dict[str, list[dict[str, Any]]] = {}
        for proj_key in project_keys:
            self.logger.info("Recovery: enumerating Jira attachments for %r", proj_key)
            jira_atts.update(self._iter_jira_issues_with_attachments(proj_key))

        if not jira_atts:
            return ComponentResult(
                success=True,
                updated=0,
                message="No Jira issues with attachments in scope; nothing to recover.",
                details={"projects_examined": len(project_keys)},
            )

        # Resolve only the WPs we need to query.
        relevant_wp_ids = sorted({wp_map[k] for k in jira_atts if k in wp_map})
        op_by_wp = self._fetch_op_attachments_by_wp(relevant_wp_ids)

        # Per-issue diff (multiset semantics).
        per_issue_missing: dict[str, list[str]] = {}
        per_issue_extra: dict[str, list[str]] = {}
        clean = 0
        wp_unmapped = 0
        missing_total = 0
        extra_total = 0

        for jira_key, jira_list in jira_atts.items():
            wp_id = wp_map.get(jira_key)
            jira_filenames = [a["filename"] for a in jira_list]
            if wp_id is None:
                wp_unmapped += 1
                per_issue_missing[jira_key] = jira_filenames
                missing_total += len(jira_filenames)
                continue
            op_filenames = op_by_wp.get(wp_id, [])
            jira_counter = Counter(jira_filenames)
            op_counter = Counter(op_filenames)
            missing = sorted((jira_counter - op_counter).elements())
            extra = sorted((op_counter - jira_counter).elements())
            if missing:
                per_issue_missing[jira_key] = missing
                missing_total += len(missing)
            if extra:
                per_issue_extra[jira_key] = extra
                extra_total += len(extra)
            if not missing and not extra:
                clean += 1

        recovery_keys = sorted(per_issue_missing)
        if not recovery_keys:
            self.logger.info(
                "Recovery: no missing attachments detected (%d issues clean, %d extras)",
                clean,
                extra_total,
            )
            # Use the same key names as the recovery branch so callers
            # can read ``details`` uniformly regardless of which path
            # the component took.
            return ComponentResult(
                success=True,
                updated=0,
                message=f"All examined attachments present in OP. clean={clean}, extra={extra_total}",
                details={
                    "projects_examined": len(project_keys),
                    "issues_examined": len(jira_atts),
                    "clean": clean,
                    "wp_unmapped": wp_unmapped,
                    "missing_total_before": 0,
                    "still_missing_total": 0,
                    "extra_total": extra_total,
                    "recovered": 0,
                },
            )

        self.logger.info(
            "Recovery: %d issues with missing attachments (%d files); delegating to attachments_migration",
            len(recovery_keys),
            missing_total,
        )

        # Delegate to the same path the original attachments migration
        # uses. ``_process_batch_end_to_end`` is idempotent on the
        # Rails side — files already attached are skipped, so passing
        # the whole jira_keys list (not just the missing filenames)
        # is safe AND simpler than re-implementing per-filename
        # filtering. Rails-side dedup keeps the cost proportional to
        # the actually-missing count.
        att_migration = AttachmentsMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
        )
        recovered = 0
        failed = 0
        batch_size = 50
        for i in range(0, len(recovery_keys), batch_size):
            batch = recovery_keys[i : i + batch_size]
            try:
                up, fl, _mapping = att_migration._process_batch_end_to_end(batch)
            except Exception:
                self.logger.exception(
                    "Recovery batch %d failed (%d keys)",
                    i // batch_size,
                    len(batch),
                )
                failed += len(batch)
                continue
            recovered += up
            failed += fl

        # After the recovery pass, recompute the missing tail so we
        # report what's *still* lost (genuine 404s / collisions /
        # transfer errors) — not the original count.
        op_by_wp_after = self._fetch_op_attachments_by_wp(relevant_wp_ids)
        still_missing_total = 0
        for jira_key in recovery_keys:
            wp_id = wp_map.get(jira_key)
            if wp_id is None:
                continue
            jira_counter = Counter(a["filename"] for a in jira_atts[jira_key])
            op_counter = Counter(op_by_wp_after.get(wp_id, []))
            still_missing_total += sum((jira_counter - op_counter).values())

        msg = (
            f"Recovery: recovered={recovered}, still_missing={still_missing_total},"
            f" failed_batches={failed}, clean={clean}, extra={extra_total},"
            f" wp_unmapped={wp_unmapped}"
        )
        self.logger.info(msg)
        return ComponentResult(
            success=still_missing_total == 0,
            updated=recovered,
            failed=failed,
            message=msg,
            details={
                "projects_examined": len(project_keys),
                "issues_examined": len(jira_atts),
                "clean": clean,
                "wp_unmapped": wp_unmapped,
                "missing_total_before": missing_total,
                "still_missing_total": still_missing_total,
                "extra_total": extra_total,
                "recovered": recovered,
                "still_missing_sample": dict(list(per_issue_missing.items())[:20]),
                "extra_sample": dict(list(per_issue_extra.items())[:20]),
            },
        )
