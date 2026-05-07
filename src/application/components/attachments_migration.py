"""Migrate Jira attachments to OpenProject and bind to work packages.

Flow:
- Extract: collect attachments for Jira issues already mapped to work packages.
- Map: download to local attachment path, compute sha256, deduplicate by digest.
- Load: copy files to container and run a minimal Rails script to attach files
  to the corresponding work packages idempotently (skip if same filename exists).

Phase 7e notes
--------------
The polymorphic ``wp_map`` (``dict | int``) ladder used to resolve a Jira
issue key to an OpenProject work-package id is normalised through
:meth:`WorkPackageMappingEntry.from_legacy` here. Production ``wp_map``
is keyed by ``str(jira_id)`` (numeric) outer, with the human-readable
``jira_key`` stored inside the value; the mapping walk prefers the
inner ``jira_key`` and falls back to the outer key (matching test
fixtures that key directly by Jira issue key).

The Jira SDK boundary in :meth:`_extract_batch` is intentionally left
duck-typed: the legacy reader probes ``attachment.content`` (an SDK
quirk — the real ``jira.Attachment`` exposes the download URL via
``url`` while cached/test payloads tend to expose ``content``). The
canonical :class:`JiraIssueFields.from_issue_any` reader maps
``att.url`` to ``JiraAttachment.content``, which would change observed
behaviour against the existing test doubles. Phase 7's scope is the
``wp_map`` ladder; this SDK probe is deferred.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry


def compute_wp_lookup_by_jira_key(mappings: Any) -> dict[str, int]:
    """Build a ``jira_key → openproject_id`` lookup from a mappings facade.

    Walks the ``work_package`` mapping once and normalises each row
    through :meth:`WorkPackageMappingEntry.from_legacy`. Production
    ``wp_map`` is keyed by ``str(jira_id)`` outer with the
    human-readable ``jira_key`` stored inside; legacy/test fixtures
    sometimes key directly by ``jira_key``. Resolution rules:

    * dict shape with inner ``jira_key`` → use the inner key.
    * dict shape without inner ``jira_key`` → fall back to outer
      (covers test fixtures keyed by ``jira_key``).
    * bare ``int`` shape → SKIP. Legacy bare-int rows do not carry
      a recoverable ``jira_key``; treating the numeric outer key as
      a Jira issue key would produce invalid downstream queries
      like ``key in (10001, …)``.

    Lifted out of :class:`AttachmentsMigration` so
    :class:`AttachmentRecoveryMigration` can call it without
    constructing a full migration instance (which carries the heavy
    ``BaseMigration.__init__`` chain).
    """
    wp_map = mappings.get_mapping("work_package") or {}
    lookup: dict[str, int] = {}
    for outer_key, raw_entry in wp_map.items():
        if not isinstance(raw_entry, dict):
            continue
        inner_jira_key = raw_entry.get("jira_key")
        jira_key = str(inner_jira_key or outer_key)
        try:
            entry = WorkPackageMappingEntry.from_legacy(jira_key, raw_entry)
        except ValueError:
            continue
        lookup[jira_key] = int(entry.openproject_id)
    return lookup


@register_entity_types("attachments")
class AttachmentsMigration(BaseMigration):  # noqa: D101
    # Cache for the wp_map → jira_key lookup. Built lazily on first use
    # by :meth:`_wp_lookup_by_jira_key`; reset to ``None`` whenever the
    # migration runs against a fresh mappings instance.
    _wp_lookup_cache: dict[str, int] | None

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        # Attachment directory
        try:
            ap = config.migration_config.get("attachment_path")  # type: ignore[assignment]
        except Exception:
            ap = None
        self.attachment_dir: Path = Path(ap or (Path(self.data_dir) / "attachments"))
        self.attachment_dir.mkdir(parents=True, exist_ok=True)
        self._wp_lookup_cache = None
        # Per-stage loss counters. Each silent skip in
        # ``_extract_batch`` / ``_map`` / ``_load`` increments a
        # bucket; ``run()`` surfaces them under
        # ``ComponentResult.details["loss_counters"]`` so an
        # operator (or the audit) can pinpoint *which* stage drops
        # files instead of seeing only the aggregate "-N missing"
        # number from the audit. Caught by the live 2026-05-07 NRS
        # audit where 9 sequential JPGs on NRS-3630 were silently
        # missing with no log clue. Reset before every ``run()``.
        self._loss_counters: Counter[str] = Counter()

    def _wp_lookup_by_jira_key(self) -> dict[str, int]:
        """Return a cached ``jira_key → openproject_id`` lookup.

        Thin wrapper over the module-level
        :func:`compute_wp_lookup_by_jira_key` helper so the
        normalisation logic is reachable by sibling components
        (notably :class:`AttachmentRecoveryMigration`) without
        having to construct a full :class:`AttachmentsMigration`
        (which carries the heavy ``BaseMigration.__init__`` chain).
        Per PR #206 review.
        """
        if self._wp_lookup_cache is None:
            self._wp_lookup_cache = compute_wp_lookup_by_jira_key(self.mappings)
        return dict(self._wp_lookup_cache)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on attachment files
        rather than fetching directly from Jira. It operates on already-fetched
        work package data and handles binary file transfers.

        Args:
            entity_type: The type of entities requested

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "AttachmentsMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on data from other migrations."
        )
        raise ValueError(msg)

    def _extract_batch(self, jira_keys: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Extract attachments for a small batch of issues (memory-efficient).

        Args:
            jira_keys: List of Jira issue keys (e.g., ["AAP-1", "AAP-2"])

        Returns:
            Dict mapping jira_key to list of attachment info dicts

        """
        if not jira_keys:
            return {}

        issues: dict[str, Any] = {}
        try:
            # Use direct JQL search to avoid ThreadPoolExecutor deadlock issues
            jql = f"key in ({','.join(jira_keys)})"
            jira_issues = self.jira_client.jira.search_issues(
                jql,
                maxResults=len(jira_keys),
                fields="attachment",
            )
            for issue in jira_issues:
                issues[issue.key] = issue
        except Exception:
            logger.exception("Failed to fetch Jira issues for attachments extraction")
            return {}

        by_key: dict[str, list[dict[str, Any]]] = {}
        for key, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                atts = getattr(fields, "attachment", None)
                if not isinstance(atts, list) or not atts:
                    continue
                items: list[dict[str, Any]] = []
                for a in atts:
                    try:
                        aid = getattr(a, "id", None)
                        filename = getattr(a, "filename", None)
                        size = getattr(a, "size", None)
                        url = getattr(a, "content", None)
                        # Without ``url`` we can't download — that's a
                        # genuine skip. But ``filename`` may be empty
                        # or the literal string ``"noname"`` for
                        # rich-text-paste images / clipboard uploads;
                        # those are still real attachments and we
                        # must preserve them. Derive a stable name
                        # from the Jira attachment id so the file
                        # ends up under a unique key downstream and
                        # the Rails LOWER(filename) idempotency check
                        # doesn't collapse multiple ``noname``s into
                        # one.
                        if not url:
                            self._loss_counters["extract_no_url"] += 1
                            continue
                        if not filename or not str(filename).strip() or str(filename).lower() == "noname":
                            if aid is None:
                                self._loss_counters["extract_no_id_no_filename"] += 1
                                continue
                            filename = f"jira-attachment-{aid}"
                        items.append({"id": aid, "filename": filename, "size": size, "url": url})
                    except Exception:
                        self._loss_counters["extract_per_attachment_exception"] += 1
                        continue
                if items:
                    by_key[key] = items
            except Exception:
                self._loss_counters["extract_per_issue_exception"] += 1
                continue

        return by_key

    def _extract(self) -> ComponentResult:
        """Collect attachments from Jira issues mapped to work packages.

        NOTE: This method is kept for backwards compatibility but is not used
        in the memory-efficient run() implementation.
        """
        return ComponentResult(success=True, extracted=0, data={"attachments": {}})

    def _download_attachment(self, url: str, dest_path: Path) -> Path:
        """Download attachment from Jira to dest_path; return local path.

        Uses Jira client's authenticated session to download.
        Tests may monkeypatch this to avoid network IO.
        """
        try:
            # Use Jira client's authenticated session
            session = getattr(self.jira_client.jira, "_session", None)
            if session is None:
                import requests

                # Fallback to unauthenticated request (unlikely to work)
                logger.warning("Jira session not available, attempting unauthenticated download")
                with requests.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with dest_path.open("wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
            else:
                # Use authenticated session (don't pass timeout - Jira session already sets it)
                response = session.get(url, stream=True)
                response.raise_for_status()
                with dest_path.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
        except Exception as e:
            logger.warning("Attachment download failed for %s: %s", url, e)
        return dest_path

    @staticmethod
    def _sha256_of(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        att_by_key: dict[str, list[dict[str, Any]]] = data.get("attachments", {}) if isinstance(data, dict) else {}
        if not att_by_key:
            return ComponentResult(success=True, data={"ops": []})

        key_to_wp_id = self._wp_lookup_by_jira_key()

        ops: list[dict[str, Any]] = []
        seen_digests: set[str] = set()

        for key, items in att_by_key.items():
            wp_id = key_to_wp_id.get(key)
            if not wp_id:
                # Issue's WP isn't in our mapping (out-of-scope project
                # or skipped issue). Counted per-attachment so the
                # totals match the recovery diagnostic's view.
                self._loss_counters["map_wp_unmapped"] += len(items)
                continue

            for item in items:
                try:
                    filename = str(item.get("filename"))
                    url = str(item.get("url"))
                    if not filename or not url:
                        self._loss_counters["map_missing_filename_or_url"] += 1
                        continue
                    safe_name = filename.replace("/", "_")
                    local_path = self.attachment_dir / safe_name
                    # Download and hash
                    self._download_attachment(url, local_path)
                    if not local_path.exists():
                        # ``_download_attachment`` already logs a
                        # warning; record here so the operator sees
                        # the count alongside the other buckets.
                        self._loss_counters["map_download_failed"] += 1
                        continue
                    digest = self._sha256_of(local_path)
                    if digest in seen_digests:
                        # Dedup local-only; still may attach same file to multiple WPs
                        pass
                    seen_digests.add(digest)
                    ops.append(
                        {
                            "jira_key": key,
                            "work_package_id": int(wp_id),
                            "local_path": local_path.as_posix(),
                            "filename": filename,
                            "digest": digest,
                        },
                    )
                except Exception:
                    self._loss_counters["map_per_item_exception"] += 1
                    continue

        return ComponentResult(success=True, data={"ops": ops})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        ops: list[dict[str, Any]] = (mapped.data or {}).get("ops", []) if mapped.data else []
        if not ops:
            return ComponentResult(success=True, updated=0, data={"attachment_mapping": {}})

        # Copy files to container and build data payload for Rails script
        logger.info("_load: transferring %d files to container", len(ops))
        container_ops: list[dict[str, Any]] = []
        for idx, op in enumerate(ops):
            try:
                local_path = Path(str(op["local_path"]))
                if not local_path.exists():
                    # Local file vanished between ``_map`` and ``_load``
                    # (rare — only possible if something else cleaned
                    # up the attachment dir mid-run). Counted so the
                    # operator sees the discrepancy.
                    self._loss_counters["load_local_file_missing"] += 1
                    continue
                wp_id = int(op["work_package_id"])  # type: ignore[arg-type]
                jira_key = str(op["jira_key"])
                filename = str(op["filename"])
                digest = str(op["digest"])
                # Place in /tmp with digest prefix to avoid collisions
                container_path = f"/tmp/j2o_att_{digest[:12]}_{os.path.basename(filename)}"
                try:
                    logger.info("_load: transferring file %d/%d: %s", idx + 1, len(ops), filename)
                    self.op_client.transfer_file_to_container(local_path, container_path)
                    logger.info("_load: transfer completed for %s", filename)
                except Exception:
                    logger.exception("File transfer failed for %s", local_path)
                    self._loss_counters["load_transfer_failed"] += 1
                    continue
                container_ops.append(
                    {
                        "work_package_id": wp_id,
                        "jira_key": jira_key,
                        "filename": filename,
                        "container_path": container_path,
                    },
                )
            except Exception:
                self._loss_counters["load_per_op_exception"] += 1
                continue

        if not container_ops:
            return ComponentResult(success=True, updated=0, data={"attachment_mapping": {}})

        # Rails script returns attachment IDs for mapping content migration
        # Must output JSON between markers for execute_script_with_data to parse.
        #
        # Filename fidelity: ``att.filename = fname`` runs through
        # ActiveRecord's setter, which on OP / CarrierWave models can
        # apply silent normalisation (strip internal whitespace,
        # Unicode-normalise) — caught by the live 2026-05-07 NRS run
        # where Jira's ``Screenshot 2026-04-21 122931.png`` was stored
        # in OP as ``Screenshot2026-04-21 122931.png``. ``update_columns``
        # bypasses callbacks/validations and writes the exact byte
        # string we provide. Run AFTER ``save!`` so the row exists
        # to update; the storage filename (CarrierWave's
        # ``file_file_name``) is left untouched (it's the
        # on-disk path) — only the user-visible ``filename`` column
        # is corrected.
        ruby_runner = (
            "require 'json'\n"
            "start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'\n"
            "end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'\n"
            "result = begin\n"
            "  ops = input_data\n"
            "  results = []\n"
            "  errors = []\n"
            "  ops.each do |op|\n"
            "    begin\n"
            "      wp = WorkPackage.find_by(id: op['work_package_id'])\n"
            "      next unless wp\n"
            "      jira_key = op['jira_key']\n"
            "      fname = op['filename']\n"
            "      # Check if an attachment with same name already exists.\n"
            "      # Compare against the byte-exact ``filename`` column the\n"
            "      # post-save ``update_columns`` writes — case-insensitive\n"
            "      # to match the project's existing idempotency contract.\n"
            "      existing = wp.attachments.where('LOWER(filename) = ?', fname.to_s.downcase).first\n"
            "      if existing\n"
            "        # Return existing attachment ID for mapping.\n"
            "        results << { jira_key: jira_key, filename: fname, attachment_id: existing.id, existed: true }\n"
            "        next\n"
            "      end\n"
            "      path = op['container_path']\n"
            "      file = File.open(path, 'rb')\n"
            "      author = User.where(admin: true).first\n"
            "      att = Attachment.new(container: wp, author: author)\n"
            "      # Assign file using Paperclip-style API.\n"
            "      if att.respond_to?(:file=)\n"
            "        att.file = file\n"
            "      end\n"
            "      att.filename = fname if att.respond_to?(:filename=)\n"
            "      att.save!\n"
            "      # Filename-fidelity guard: bypass any AR callbacks and\n"
            "      # write the exact byte string. Without this, OP's\n"
            "      # filename sanitiser strips selected internal\n"
            "      # whitespace (NRS audit: ~31 of 163 'missing' files\n"
            "      # were present under a normalised name).\n"
            "      att.update_columns(filename: fname) if att.filename != fname\n"
            "      results << { jira_key: jira_key, filename: fname, attachment_id: att.id, existed: false }\n"
            "    rescue => e\n"
            "      errors << { jira_key: op['jira_key'], filename: op['filename'], error: e.message }\n"
            "    end\n"
            "  end\n"
            "  { results: results, errors: errors }\n"
            "rescue => e\n"
            "  { results: [], errors: [{ error: e.message }] }\n"
            "end\n"
            "puts start_marker\n"
            "puts result.to_json\n"
            "puts end_marker\n"
        )

        updated = 0
        failed = 0
        attachment_mapping: dict[str, dict[str, int]] = {}
        logger.info("_load: all %d files transferred, executing Rails script with %d ops", len(ops), len(container_ops))
        try:
            envelope = self.op_client.execute_script_with_data(ruby_runner, container_ops)
            logger.info("_load: Rails script completed")
            # ``execute_script_with_data`` returns an envelope:
            # ``{status, message, data, output}``. The actual results
            # live under ``data`` (the parsed JSON the Ruby script
            # printed between the markers). The pre-fix code read
            # ``res.get("results")`` directly on the envelope and
            # always saw an empty list — silently returning
            # ``updated=0, failed=0`` regardless of what Rails did.
            # Same envelope-bug class PR #201 caught for
            # ``wp_metadata_backfill``.
            if isinstance(envelope, dict):
                if envelope.get("status") != "success":
                    logger.warning(
                        "_load: Rails returned status=%r message=%r — counting batch as failed",
                        envelope.get("status"),
                        envelope.get("message"),
                    )
                    failed = len(container_ops)
                    self._loss_counters["load_rails_status_not_success"] += len(container_ops)
                else:
                    data = envelope.get("data") or {}
                    if not isinstance(data, dict):
                        data = {}
                    results = data.get("results", [])
                    errors = data.get("errors", [])
                    # Build attachment mapping: {jira_key: {filename: attachment_id}}
                    for r in results:
                        jira_key = r.get("jira_key")
                        filename = r.get("filename")
                        att_id = r.get("attachment_id")
                        if jira_key and filename and att_id:
                            if jira_key not in attachment_mapping:
                                attachment_mapping[jira_key] = {}
                            attachment_mapping[jira_key][filename] = int(att_id)
                            if not r.get("existed"):
                                updated += 1
                    failed = len(errors)
                    if errors:
                        self._loss_counters["load_rails_per_op_error"] += len(errors)
        except Exception:
            logger.exception("Rails attach operation failed")
            failed = len(container_ops)
            self._loss_counters["load_rails_call_exception"] += len(container_ops)

        return ComponentResult(
            success=failed == 0,
            updated=updated,
            failed=failed,
            data={"attachment_mapping": attachment_mapping},
        )

    def _save_attachment_mapping(self, mapping: dict[str, dict[str, int]]) -> None:
        """Save attachment mapping to file for content migration phase.

        Args:
            mapping: {jira_key: {filename: openproject_attachment_id}}

        """
        mapping_file = Path(self.data_dir) / "attachment_mapping.json"
        try:
            # Load existing mapping if present (for incremental migrations)
            existing: dict[str, dict[str, int]] = {}
            if mapping_file.exists():
                with mapping_file.open("r") as f:
                    existing = json.load(f)

            # Merge new mapping into existing
            for jira_key, attachments in mapping.items():
                if jira_key not in existing:
                    existing[jira_key] = {}
                existing[jira_key].update(attachments)

            # Save merged mapping
            with mapping_file.open("w") as f:
                json.dump(existing, f, indent=2)
            self.logger.info("Saved attachment mapping for %d issues to %s", len(existing), mapping_file)
        except Exception:
            self.logger.exception("Failed to save attachment mapping")

    def _process_batch_end_to_end(
        self,
        jira_keys: list[str],
    ) -> tuple[int, int, dict[str, dict[str, int]]]:
        """Process a batch of issues: extract, download, upload, attach.

        Args:
            jira_keys: List of Jira issue keys to process

        Returns:
            Tuple of (updated_count, failed_count, attachment_mapping)

        """
        # Extract attachment metadata for this batch
        logger.info("_process_batch_end_to_end: starting extract for %d keys", len(jira_keys))
        att_by_key = self._extract_batch(jira_keys)
        logger.info("_process_batch_end_to_end: extracted %d issues with attachments", len(att_by_key))
        if not att_by_key:
            return 0, 0, {}

        # Single normalisation source — see ``_wp_lookup_by_jira_key``.
        key_to_wp = self._wp_lookup_by_jira_key()

        # Download attachments and prepare ops
        ops: list[dict[str, Any]] = []
        seen_digests: set[str] = set()

        for key, items in att_by_key.items():
            # Find work package ID from reverse lookup
            wp_id = key_to_wp.get(key)
            if not wp_id:
                self._loss_counters["map_wp_unmapped"] += len(items)
                continue

            for item in items:
                try:
                    filename = str(item.get("filename"))
                    url = str(item.get("url"))
                    if not filename or not url:
                        self._loss_counters["map_missing_filename_or_url"] += 1
                        continue
                    safe_name = filename.replace("/", "_")
                    local_path = self.attachment_dir / safe_name
                    # Download
                    self._download_attachment(url, local_path)
                    if not local_path.exists():
                        self._loss_counters["map_download_failed"] += 1
                        continue
                    digest = self._sha256_of(local_path)
                    seen_digests.add(digest)
                    ops.append(
                        {
                            "jira_key": key,
                            "work_package_id": int(wp_id),
                            "local_path": local_path.as_posix(),
                            "filename": filename,
                            "digest": digest,
                        },
                    )
                except Exception:
                    self._loss_counters["map_per_item_exception"] += 1
                    continue

        if not ops:
            logger.info("_process_batch_end_to_end: no ops to load, returning early")
            return 0, 0, {}

        # Upload to container and load via Rails
        logger.info("_process_batch_end_to_end: starting _load with %d ops", len(ops))
        mapped_result = ComponentResult(success=True, data={"ops": ops})
        loaded = self._load(mapped_result)
        logger.info("_process_batch_end_to_end: _load completed")

        # Cleanup local files after upload
        for op in ops:
            try:
                local_path = Path(str(op["local_path"]))
                if local_path.exists():
                    local_path.unlink()
            except Exception:
                pass

        return (
            loaded.updated or 0,
            loaded.failed or 0,
            (loaded.data or {}).get("attachment_mapping", {}),
        )

    def run(self) -> ComponentResult:
        """Execute attachment migration pipeline - memory efficient per-project."""
        self.logger.info("Starting attachment migration (memory-efficient mode)")

        # Reset per-stage loss counters at the start of every run so
        # the surfaced totals reflect THIS invocation, not a prior one
        # accumulated on the same instance (matters when ``run()`` is
        # called more than once on the same migration object — e.g.
        # ``attachment_recovery_migration`` constructs its own
        # ``AttachmentsMigration`` and delegates).
        self._loss_counters.clear()

        # Build the canonical Jira-key → wp-id lookup once, then group
        # the resolved Jira keys by project for batch-friendly processing.
        # See :meth:`_wp_lookup_by_jira_key` for normalisation rules.
        key_to_wp_id = self._wp_lookup_by_jira_key()
        if not key_to_wp_id:
            # FAIL LOUD. Silent ``success=True`` here masks a 100%
            # attachment loss (operator postmortem in PR #194). The
            # mapping may be either *absent* (skeleton never ran /
            # ``_save_mapping`` swallowed an error) or *present but
            # unusable* (only legacy bare-int rows that
            # ``_wp_lookup_by_jira_key`` skips). Distinguish both
            # cases in the message so the operator knows whether to
            # re-run skeleton or to back-fill ``jira_key`` on the
            # legacy rows.
            raw_wp_map = self.mappings.get_mapping("work_package") or {}
            if raw_wp_map:
                msg = (
                    f"work_package mapping present ({len(raw_wp_map)} entries) but"
                    " contains no usable rows (no entry has a recoverable Jira key"
                    " — likely all legacy bare-int entries). Re-run"
                    " work_packages_skeleton to refresh the mapping shape."
                )
            else:
                msg = (
                    "No work_package mapping available — attachments cannot be"
                    " correlated to OP work packages. Run the work_packages_skeleton"
                    " component first (or verify it persisted its mapping)."
                )
            self.logger.error(msg)
            return ComponentResult(
                success=False,
                updated=0,
                message=msg,
                errors=["missing_work_package_mapping"],
            )

        by_project: dict[str, list[str]] = {}
        for jira_key in key_to_wp_id:
            project_key = self._issue_project_key(jira_key)
            if project_key not in by_project:
                by_project[project_key] = []
            by_project[project_key].append(jira_key)

        total_issues = sum(len(keys) for keys in by_project.values())
        self.logger.info(
            "Processing attachments for %d projects (%d issues total)",
            len(by_project),
            total_issues,
        )

        total_updated = 0
        total_failed = 0
        all_mappings: dict[str, dict[str, int]] = {}
        batch_size = 50  # Process 50 issues at a time to limit memory usage

        for project_key, jira_keys in by_project.items():
            self.logger.info(
                "Processing project %s (%d issues)",
                project_key,
                len(jira_keys),
            )

            # Process in small batches
            for i in range(0, len(jira_keys), batch_size):
                batch_keys = jira_keys[i : i + batch_size]
                try:
                    updated, failed, mapping = self._process_batch_end_to_end(
                        batch_keys,
                    )
                    total_updated += updated
                    total_failed += failed
                    # Merge mapping
                    for jk, att_map in mapping.items():
                        if jk not in all_mappings:
                            all_mappings[jk] = {}
                        all_mappings[jk].update(att_map)
                except Exception:
                    self.logger.exception(
                        "Batch processing failed for project %s batch %d",
                        project_key,
                        i // batch_size,
                    )
                    total_failed += len(batch_keys)

            # Log progress after each project
            self.logger.info(
                "Project %s complete: %d updated, %d failed so far",
                project_key,
                total_updated,
                total_failed,
            )

        # Save final attachment mapping
        self._save_attachment_mapping(all_mappings)

        success = total_failed == 0 or (total_updated > 0 and total_failed < total_updated)
        # Surface the per-stage drop breakdown so an operator can
        # tell where files are getting lost (e.g. download failures
        # vs Rails errors vs filename normalisation). The buckets
        # also feed the next ``attachment_recovery`` run's
        # diagnostics.
        loss_counters = dict(self._loss_counters)
        if loss_counters:
            self.logger.info(
                "Attachments loss breakdown (silent skips): %s",
                loss_counters,
            )
        return ComponentResult(
            success=success,
            updated=total_updated,
            failed=total_failed,
            data={"attachment_mapping": all_mappings},
            message=f"Processed {total_updated} attachments, {total_failed} failures",
            details={"loss_counters": loss_counters},
        )

    def run_legacy(self) -> ComponentResult:
        """Legacy run method - kept for reference but not used."""
        self.logger.info("Starting attachment migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error("Attachment extraction failed: %s", extracted.message or extracted.error)
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error("Attachment mapping failed: %s", mapped.message or mapped.error)
            return mapped

        loaded = self._load(mapped)

        # Save attachment mapping for content migration phase
        if loaded.data and "attachment_mapping" in loaded.data:
            self._save_attachment_mapping(loaded.data["attachment_mapping"])

        if loaded.success:
            self.logger.info("Attachment migration completed (updated=%s, failed=%s)", loaded.updated, loaded.failed)
        else:
            self.logger.error("Attachment migration encountered failures (failed=%s)", loaded.failed)
        return loaded
