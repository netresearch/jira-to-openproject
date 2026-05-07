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

import json
import re
import unicodedata
from collections import Counter
from typing import Any

from src.application.components.attachments_migration import (
    AttachmentsMigration,
    compute_wp_lookup_by_jira_key,
)
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
    def _normalize_filename(name: str) -> str:
        """Return a normalized form of ``name`` for false-positive
        pairing.

        Strips ALL whitespace (regular, NBSP, zero-width, …) and
        applies Unicode NFC normalisation + case folding. Two
        filenames that differ only in whitespace or Unicode
        composition will share the same normalized form — which is
        exactly the false-positive class CarrierWave / Rails creates
        when it sanitises the stored filename on save.
        """
        # NFC-normalise then strip ALL Unicode whitespace classes
        # (matches \s + NBSP + zero-width spaces).
        nfc = unicodedata.normalize("NFC", name)
        return re.sub(r"\s+", "", nfc, flags=re.UNICODE).casefold()

    @classmethod
    def _pair_by_normalized_name(
        cls,
        missing: Counter[str],
        extra: Counter[str],
    ) -> int:
        """Mutate ``missing`` and ``extra`` in place: subtract any
        files whose normalized name matches across both sides.

        Returns the count of paired filenames (each pair == one
        false positive). On a clean run with perfect filename
        fidelity, returns 0.
        """
        paired = 0
        # Index by normalized form to find matches in O(n).
        missing_by_norm: dict[str, list[str]] = {}
        for fn in list(missing.elements()):
            missing_by_norm.setdefault(cls._normalize_filename(fn), []).append(fn)
        for fn in list(extra.elements()):
            norm = cls._normalize_filename(fn)
            candidates = missing_by_norm.get(norm)
            if not candidates:
                continue
            # Pop one candidate from missing.
            paired_name = candidates.pop()
            missing[paired_name] -= 1
            if missing[paired_name] <= 0:
                del missing[paired_name]
            extra[fn] -= 1
            if extra[fn] <= 0:
                del extra[fn]
            paired += 1
            if not candidates:
                missing_by_norm.pop(norm, None)
        return paired

    @staticmethod
    def _read_attr(obj: Any, name: str) -> Any:
        """Dual-shape access (dict / SDK object). Mirrors the helper in
        :mod:`relation_migration` and the audit tool.
        """
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def _project_keys_in_scope(self, wp_map: dict[str, int]) -> list[str]:
        """Project keys present in the WP mapping (after the ``inner.jira_key`` /
        outer-fallback normalisation). Used to scope the Jira-side
        pagination query without requiring CLI flags.
        """
        prefixes: set[str] = set()
        for k in wp_map:
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
                    raw_filename = self._read_attr(a, "filename")
                    aid = self._read_attr(a, "id")
                    # Apply the SAME ``noname``/empty → ``jira-attachment-{aid}``
                    # transformation that ``AttachmentsMigration._extract_batch``
                    # applies before upload. Without this, the recovery
                    # audit compares Jira's raw ``noname`` against OP's
                    # uploaded ``jira-attachment-{aid}`` and reports the
                    # files as missing even though the upload succeeded —
                    # caught on the live 2026-05-07 NRS audit
                    # (e.g. NRS-4347 reported ``['noname','noname','noname']``
                    # missing while the same WP held the uploaded
                    # ``jira-attachment-XXXX`` triplet under ``extra``).
                    is_blank = (
                        not isinstance(raw_filename, str)
                        or not raw_filename.strip()
                        or raw_filename.strip().lower() == "noname"
                    )
                    if is_blank:
                        if not aid:
                            # No id and no filename — genuinely
                            # un-uploadable; the upload pipeline drops
                            # these too via ``extract_no_id_no_filename``.
                            continue
                        filename = f"jira-attachment-{aid}"
                    else:
                        filename = raw_filename
                    entries.append(
                        {
                            "filename": filename,
                            "size": self._read_attr(a, "size"),
                            "id": aid,
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

        # Use the shared ``compute_wp_lookup_by_jira_key`` helper so we
        # see exactly the same normalisation the original migration
        # used (no divergence-bug class) without paying the
        # ``BaseMigration.__init__`` cost up-front. We only construct
        # an :class:`AttachmentsMigration` once we actually have keys
        # to delegate — keeps the empty/no-recovery paths fast.
        wp_map = compute_wp_lookup_by_jira_key(self.mappings)
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

        project_keys = self._project_keys_in_scope(wp_map)
        if not project_keys:
            return ComponentResult(
                success=True,
                updated=0,
                message="No project keys derivable from work_package mapping; nothing to recover.",
                details={"projects_examined": 0},
            )

        # Gather Jira side first — paginated per project. Scope to
        # the keys present in the WP mapping immediately so unmapped
        # issues never pollute the recovery accounting (PR #206
        # review: previously they inflated ``missing_total_before``,
        # got passed to delegation as wasted API calls, and were
        # silently skipped from ``still_missing_total`` so a run
        # could report ``success=True`` with ``wp_unmapped > 0``
        # masking real loss).
        jira_atts_all: dict[str, list[dict[str, Any]]] = {}
        for proj_key in project_keys:
            self.logger.info("Recovery: enumerating Jira attachments for %r", proj_key)
            jira_atts_all.update(self._iter_jira_issues_with_attachments(proj_key))

        # Split into in-scope (mapped) and unmapped buckets.
        jira_atts: dict[str, list[dict[str, Any]]] = {}
        unmapped_jira_keys: list[str] = []
        for k, v in jira_atts_all.items():
            if k in wp_map:
                jira_atts[k] = v
            else:
                unmapped_jira_keys.append(k)

        if unmapped_jira_keys:
            # These issues exist in Jira but their WPs aren't in our
            # mapping — surface as a real warning so an operator
            # knows attachments on those WPs (if any) cannot be
            # recovered by this run.
            self.logger.warning(
                "Recovery: %d Jira issues have attachments but no WP mapping entry"
                " (sample: %s) — out of scope for this component;"
                " run work_packages_skeleton on those projects first.",
                len(unmapped_jira_keys),
                unmapped_jira_keys[:10],
            )

        if not jira_atts:
            return ComponentResult(
                # ``success`` reflects the *in-scope* state. Out-of-scope
                # unmapped issues are surfaced via the warning above
                # and the ``wp_unmapped`` count below; they do not
                # pretend a clean run.
                success=True,
                updated=0,
                message=(f"No in-scope Jira issues with attachments to recover. wp_unmapped={len(unmapped_jira_keys)}"),
                details={
                    "projects_examined": len(project_keys),
                    "wp_unmapped": len(unmapped_jira_keys),
                },
            )

        # Resolve only the WPs we need to query.
        relevant_wp_ids = sorted({wp_map[k] for k in jira_atts})
        op_by_wp = self._fetch_op_attachments_by_wp(relevant_wp_ids)

        # Per-issue diff (multiset semantics).
        per_issue_missing: dict[str, list[str]] = {}
        per_issue_extra: dict[str, list[str]] = {}
        clean = 0
        missing_total = 0
        extra_total = 0

        # Track filename-fidelity false positives: files that ARE in
        # OP but under a slightly-different name (CarrierWave / Rails
        # strips certain whitespace + Unicode normalises the filename
        # on save). Without this match-by-normalized-name pass, the
        # diagnostic counts the same file as both "missing" (under the
        # Jira name) AND "extra" (under the OP name), inflating the
        # apparent loss. Live 2026-05-07 NRS run: ~31 of 163
        # "missing" attachments are actually present under a
        # space-stripped filename. A separate fix in the Rails-side
        # attach script is needed to preserve fidelity; this only
        # corrects the diagnostic accounting.
        fidelity_false_positives = 0

        for jira_key, jira_list in jira_atts.items():
            wp_id = wp_map[jira_key]  # guaranteed by the in-scope filter above
            jira_filenames = [a["filename"] for a in jira_list]
            op_filenames = op_by_wp.get(wp_id, [])
            jira_counter = Counter(jira_filenames)
            op_counter = Counter(op_filenames)
            raw_missing = jira_counter - op_counter
            raw_extra = op_counter - jira_counter
            # Pair raw_missing with raw_extra by normalized filename
            # (case-folded, NFC-normalised, all whitespace stripped).
            # If a missing file matches an extra under that key, the
            # file is actually present in OP — just under a sanitised
            # name. Subtract those pairs from both buckets.
            paired = self._pair_by_normalized_name(raw_missing, raw_extra)
            fidelity_false_positives += paired
            missing = sorted(raw_missing.elements())
            extra = sorted(raw_extra.elements())
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
                "Recovery: no missing attachments detected (%d issues clean, %d extras,"
                " %d unmapped, %d fidelity false positives paired)",
                clean,
                extra_total,
                len(unmapped_jira_keys),
                fidelity_false_positives,
            )
            # Use the same key names as the recovery branch so callers
            # can read ``details`` uniformly regardless of which path
            # the component took.
            return ComponentResult(
                success=True,
                updated=0,
                message=(
                    f"All in-scope attachments present in OP. clean={clean},"
                    f" extra={extra_total}, wp_unmapped={len(unmapped_jira_keys)},"
                    f" fidelity_false_positives={fidelity_false_positives}"
                ),
                details={
                    "projects_examined": len(project_keys),
                    "issues_examined": len(jira_atts),
                    "clean": clean,
                    "wp_unmapped": len(unmapped_jira_keys),
                    "missing_total_before": 0,
                    "still_missing_total": 0,
                    "extra_total": extra_total,
                    "fidelity_false_positives": fidelity_false_positives,
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
        merged_mapping: dict[str, dict[str, int]] = {}
        batch_size = 50
        for i in range(0, len(recovery_keys), batch_size):
            batch = recovery_keys[i : i + batch_size]
            try:
                up, fl, mapping = att_migration._process_batch_end_to_end(batch)
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
            # Accumulate {jira_key: {filename: attachment_id}} for
            # downstream consumers (work_packages_content uses this
            # mapping to resolve ``!image.png!`` references to OP API
            # URLs). Per PR #206 review.
            if isinstance(mapping, dict):
                for jk, file_map in mapping.items():
                    if not isinstance(file_map, dict):
                        continue
                    bucket = merged_mapping.setdefault(str(jk), {})
                    for fn, att_id in file_map.items():
                        try:
                            bucket[str(fn)] = int(att_id)
                        except TypeError, ValueError:
                            continue

        # Persist the recovered attachment mapping. Merge with any
        # existing ``attachment_mapping.json`` so the original run's
        # entries aren't lost. Atomic write (tmp + rename) — same
        # defence PR #197 added for the WP mapping.
        if merged_mapping:
            self._merge_attachment_mapping(merged_mapping)

        # After the recovery pass, recompute the missing tail so we
        # report what's *still* lost (genuine 404s / collisions /
        # transfer errors) — not the original count.
        op_by_wp_after = self._fetch_op_attachments_by_wp(relevant_wp_ids)
        still_missing_total = 0
        for jira_key in recovery_keys:
            wp_id = wp_map[jira_key]  # guaranteed by in-scope filter
            jira_counter = Counter(a["filename"] for a in jira_atts[jira_key])
            op_counter = Counter(op_by_wp_after.get(wp_id, []))
            still_missing_total += sum((jira_counter - op_counter).values())

        msg = (
            f"Recovery: recovered={recovered}, still_missing={still_missing_total},"
            f" failed_batches={failed}, clean={clean}, extra={extra_total},"
            f" wp_unmapped={len(unmapped_jira_keys)},"
            f" fidelity_false_positives={fidelity_false_positives}"
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
                "wp_unmapped": len(unmapped_jira_keys),
                "missing_total_before": missing_total,
                "still_missing_total": still_missing_total,
                "extra_total": extra_total,
                "fidelity_false_positives": fidelity_false_positives,
                "recovered": recovered,
                "still_missing_sample": dict(list(per_issue_missing.items())[:20]),
                "extra_sample": dict(list(per_issue_extra.items())[:20]),
            },
        )

    def _merge_attachment_mapping(self, new_mapping: dict[str, dict[str, int]]) -> None:
        """Merge ``new_mapping`` into ``attachment_mapping.json`` atomically.

        Each entry has shape ``{jira_key: {filename: attachment_id}}``.
        Existing entries are preserved; per-key file maps are merged
        with the new run's entries taking precedence on duplicate
        filenames (e.g. when an existing OP attachment was re-attached
        with a fresh id). Atomic write (tmp + rename) keeps the file
        consistent if the process crashes mid-dump — same defence
        PR #197 added for ``work_package_mapping``.
        """
        path = self.data_dir / "attachment_mapping.json"
        existing: dict[str, dict[str, int]] = {}
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, dict):
                            existing[str(k)] = {
                                str(fn): int(aid)
                                for fn, aid in v.items()
                                if isinstance(aid, (int, str)) and str(aid).lstrip("-").isdigit()
                            }
            except (OSError, json.JSONDecodeError) as exc:
                self.logger.warning(
                    "Existing attachment_mapping.json unreadable (%s) — recovery"
                    " writes a fresh file from this run's results only.",
                    exc,
                )
                existing = {}
        for jk, file_map in new_mapping.items():
            existing.setdefault(jk, {}).update(file_map)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, sort_keys=True)
        tmp.replace(path)
        self.logger.info(
            "Recovery: persisted attachment_mapping for %d Jira keys (total %d entries)",
            len(new_mapping),
            sum(len(v) for v in existing.values()),
        )
