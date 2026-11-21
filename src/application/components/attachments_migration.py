# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

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

from jira import Issue
from src import config
from src.application.components import level_issues
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.security_level_projects_migration import LevelProjectError
from src.config import logger
from src.infrastructure.jira.issue_pagination import iter_issue_pages
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.container_staging import private_container_directory
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry
from src.utils.scoped_receipts import ScopedReceipts


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
    _receipts: ScopedReceipts | None = None

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
        # Level work packages (ADR-009) this run may attach to: Jira key to the work package its
        # sub-project holds now. Empty unless the level switch is on.
        self._level_work_packages: dict[str, int] = {}
        self._level_ids: dict[str, str] = {}
        self._level: level_issues.LevelWorkPackages | None = None
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
        return dict(self._wp_lookup_cache) | getattr(self, "_level_work_packages", {})

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

        jql = f"key in ({','.join(jira_keys)}) ORDER BY key ASC"
        # A level issue is checked again: its level must still be the one it was selected for.
        level_ids = getattr(self, "_level_ids", {})
        levels = {key: level_ids[key] for key in jira_keys if key in level_ids}
        fields = "attachment,security" if levels else "attachment"

        def fetch_page(offset: int, size: int) -> list[Issue]:
            return self.jira_client.jira.search_issues(jql, startAt=offset, maxResults=size, fields=fields)

        project_key = self._issue_project_key(jira_keys[0])
        requested = set(jira_keys)
        issues = {
            issue.key: issue for issue in iter_issue_pages(project_key, fetch_page, len(requested), len(requested))
        }
        missing = requested.difference(issues)
        if missing:
            self._loss_counters["extract_unread_issues"] += len(missing)
            logger.error("Jira omitted issues during attachment extraction: %s", ", ".join(sorted(missing)))

        by_key: dict[str, list[dict[str, Any]]] = {}
        for key, issue in self._with_unchanged_levels(issues, levels).items():
            try:
                fields = getattr(issue, "fields", None)
                atts = getattr(fields, "attachment", None)
                if self._receipts is not None and isinstance(atts, list):
                    self._receipts.observe(key, [getattr(attachment, "id", None) for attachment in atts])
                if not isinstance(atts, list) or not atts:
                    continue
                items = self._attachment_records(atts)
                if items:
                    by_key[key] = items
            except Exception:
                self._loss_counters["extract_per_issue_exception"] += 1
                continue

        return by_key

    def _with_unchanged_levels(self, issues: dict[str, Any], levels: dict[str, str]) -> dict[str, Any]:
        """Drop each level issue whose level is no longer the one it was selected for; count it."""
        kept = {}
        for key, issue in issues.items():
            if key in levels and level_issues.issue_level_id(issue) != levels[key]:
                self._loss_counters["extract_level_changed"] += 1
                logger.error("Level work package attachments refused: %s changed its level after the selection", key)
            else:
                kept[key] = issue
        return kept

    def _attachment_records(self, attachments: list) -> list[dict[str, Any]]:
        """Keep attachments with a download URL and a recoverable filename."""
        items = []
        for attachment in attachments:
            try:
                aid = getattr(attachment, "id", None)
                filename = getattr(attachment, "filename", None)
                size = getattr(attachment, "size", None)
                url = getattr(attachment, "content", None)
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
        return items

    def _extract(self) -> ComponentResult:
        """Collect attachments from Jira issues mapped to work packages.

        NOTE: This method is kept for backwards compatibility but is not used
        in the memory-efficient run() implementation.
        """
        return ComponentResult(success=True, extracted=0, data={"attachments": {}})

    @staticmethod
    def _double_encode_slashes(url: str) -> str | None:
        """Return the same URL with each ``%2F`` / ``%2C`` re-encoded
        as ``%252F`` / ``%252C``.

        The replace runs over the *whole* URL string. For Jira
        attachment URLs the offending ``%2F`` / ``%2C`` only ever
        appear inside the filename component of the path (Jira
        percent-encodes them when building the URL from the
        attachment metadata), so a plain string replace is safe in
        practice. **Caveat**: if a future caller passes a URL whose
        *query string* contains ``%2F`` or ``%2C``, double-encoding
        them would change the decoded query parameter values
        (servers decode query and path with the same rules but the
        post-decode tokens carry different semantic meaning depending
        on the parameter). Today's only caller —
        ``_download_attachment`` — only retries with this transform
        on a Jira attachment download URL, which is path-only by
        construction. Per PR #217/#223 review.

        Tomcat (Jira's web server) blocks URLs whose path contains a
        literal encoded slash (``%2F``) by default — it returns
        ``HTTP 400 Bad Request`` before the request ever reaches the
        Jira app. Double-encoding bypasses Tomcat's check: the ``%25``
        stays a percent literal at Tomcat's layer, and Jira's
        application then decodes it once to ``%2F`` and parses it as a
        slash inside the filename — which is what Jira originally
        produced when generating the URL.

        Live 2026-05-08 NRS audit: the last ``still_missing=1`` was
        traced here. Returns ``None`` when no transform applies, so
        callers can short-circuit.
        """
        replaced = url.replace("%2F", "%252F").replace("%2C", "%252C")
        return replaced if replaced != url else None

    def _download_attachment(self, url: str, dest_path: Path) -> Path | None:
        """Download attachment from Jira to dest_path; return local path.

        Uses Jira client's authenticated session to download.
        Tests may monkeypatch this to avoid network IO.

        On HTTP 400 with a URL containing ``%2F`` / ``%2C`` in the path,
        retries once with the slashes/commas re-encoded as
        ``%252F`` / ``%252C`` to bypass Tomcat's literal-encoded-slash
        block. See :meth:`_double_encode_slashes`.
        """
        try:
            self._fetch_attachment(url, dest_path)
        except Exception as e:
            # Retry with double-encoded slashes/commas if Tomcat
            # rejected the URL with HTTP 400. At most one extra HTTP
            # call per genuinely-failing download.
            retried = False
            if self._is_http_400(e):
                fallback = self._double_encode_slashes(url)
                if fallback:
                    try:
                        self._fetch_attachment(fallback, dest_path)
                        retried = True
                    except Exception as e2:
                        logger.warning(
                            "Attachment download failed for %s (also double-encoded retry): %s",
                            url,
                            e2,
                        )
            if not retried:
                logger.warning("Attachment download failed for %s: %s", url, e)
                return None
        return dest_path

    def _fetch_attachment(self, target_url: str, dest_path: Path) -> None:
        # Always wrap the response in a ``with`` block so the
        # underlying connection is returned to the pool even on
        # iteration errors mid-stream — without this, a partial
        # read can leak the keep-alive socket. Per PR #217 review.
        session = getattr(self.jira_client.jira, "_session", None)
        if session is None:
            import requests

            logger.warning("Jira session not available, attempting unauthenticated download")
            with requests.get(target_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                self._stream_to(r, dest_path)
            return
        with session.get(target_url, stream=True) as response:
            response.raise_for_status()
            self._stream_to(response, dest_path)

    @staticmethod
    def _stream_to(response: Any, target_path: Path) -> None:
        # Stream-write into a tempfile + rename so a mid-stream
        # error never leaves a truncated file behind for callers
        # that only check ``local_path.exists()``. Per PR #217 review.
        tmp_path = target_path.with_name(target_path.name + ".part")
        try:
            with tmp_path.open("wb") as f:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            tmp_path.replace(target_path)
        except Exception:
            # Clean up the partial file on failure so the caller
            # sees ``not local_path.exists()`` and counts it under
            # ``map_download_failed`` instead of misinterpreting
            # an N-byte fragment as a successful download.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _is_http_400(exc: Exception) -> bool:
        # Prefer the structured status code over substring
        # matching against ``str(exc)``: ``requests.HTTPError``
        # carries the ``Response`` it was raised from, and
        # ``response.status_code`` is the canonical signal. Fall
        # through to substring match only for non-``HTTPError``
        # cases (e.g. tests with custom exception classes that
        # mirror the surface but don't carry ``response``).
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status == 400
        err_text = str(exc)
        return "400" in err_text or "Bad Request" in err_text

    @staticmethod
    def _sha256_of(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _prepare_op_from_item(
        self,
        *,
        item: dict[str, Any],
        jira_key: str,
        work_package_id: int,
    ) -> dict[str, Any] | None:
        """Validate, download, hash a single Jira attachment item.

        Returns the prepared op dict (with ``jira_key``,
        ``work_package_id``, ``local_path``, ``filename``, ``digest``)
        on success, or ``None`` when the item should be skipped. Each
        skip-class increments a named ``_loss_counters`` bucket so the
        per-stage breakdown stays accurate.

        Extracted out of ``_map`` and ``_process_batch_end_to_end``
        which had a near-identical 20-line block — Sonar flagged the
        duplicated body on PR #224. Keeping the helper here on the
        instance (instead of a free function) so it can update
        ``self._loss_counters`` directly without threading.
        """
        raw_filename = item.get("filename")
        raw_url = item.get("url")
        # Validate BEFORE coercing to string — ``str(None)``
        # produces the truthy literal ``"None"``, which would bypass
        # the ``if not …`` guard and silently let an invalid item
        # through. Per PR #211 review.
        if not raw_filename or not raw_url:
            self._loss_counters["map_missing_filename_or_url"] += 1
            return None
        filename = str(raw_filename)
        url = str(raw_url)
        # Disambiguate per-attachment on the local filesystem so two
        # attachments sharing a Jira filename (common across issues
        # — ``image.png`` everywhere) don't overwrite each other on
        # disk before ``_load`` can transfer them. The Jira attachment
        # id is immutable + unique, so it's a stable disambiguator.
        # The op's ``filename`` field stays the original Jira filename
        # so the Rails attach script writes the user-visible name
        # correctly. Per PR #222 review.
        aid = item.get("id") or ""
        safe_name = filename.replace("/", "_")
        local_path = self.attachment_dir / f"{aid}_{safe_name}" if aid else self.attachment_dir / safe_name
        # ``_download_attachment`` already swallows transport
        # exceptions and logs a warning — the next-line ``exists()``
        # check is the canonical "did the download produce a file"
        # signal. Adding an outer try/except here would mask
        # genuine programming errors (e.g., the helper itself
        # raising on a bug in this module) by lumping them under
        # ``map_download_failed``; the per-item ``except Exception``
        # in the *caller* still catches anything truly unexpected
        # and routes it to ``map_per_item_exception``. Per PR #225
        # review.
        downloaded = self._download_attachment(url, local_path)
        if downloaded is None or not local_path.exists():
            self._loss_counters["map_download_failed"] += 1
            return None
        digest = self._sha256_of(local_path)
        return {
            "jira_key": jira_key,
            "work_package_id": int(work_package_id),
            "jira_attachment_id": aid,
            "local_path": local_path.as_posix(),
            "filename": filename,
            "digest": digest,
        }

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        att_by_key: dict[str, list[dict[str, Any]]] = data.get("attachments", {}) if isinstance(data, dict) else {}
        if not att_by_key:
            return ComponentResult(success=True, data={"ops": []})

        key_to_wp_id = self._wp_lookup_by_jira_key()

        ops = self._attachment_operations(att_by_key, key_to_wp_id)
        return ComponentResult(success=True, data={"ops": ops})

    def _attachment_operations(self, att_by_key: dict, key_to_wp_id: dict) -> list[dict[str, Any]]:
        """Prepare mapped attachments and count unmatched or invalid source rows."""
        ops: list[dict[str, Any]] = []

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
                    op = self._prepare_op_from_item(item=item, jira_key=key, work_package_id=int(wp_id))
                    if op is None:
                        continue
                    ops.append(op)
                except Exception:
                    self._loss_counters["map_per_item_exception"] += 1
                    continue

        return ops

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        ops: list[dict[str, Any]] = (mapped.data or {}).get("ops", []) if mapped.data else []
        if not ops:
            return ComponentResult(success=True, updated=0, data={"attachment_mapping": {}})

        with private_container_directory(self.op_client.docker_client) as directory:
            container_ops = self._transfer_attachments(ops, directory)
            return self._attach_files(container_ops)

    def _transfer_attachments(self, ops: list[dict[str, Any]], directory: Path) -> list[dict[str, Any]]:
        """Stage attachment bytes under unique names, keeping display names in the payload."""
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
                container_path = directory / f"{idx}-{Path(filename).name}"
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
                        "container_path": container_path.as_posix(),
                        "digest": op.get("digest"),
                        "jira_attachment_id": op.get("jira_attachment_id"),
                    },
                )
            except Exception:
                self._loss_counters["load_per_op_exception"] += 1
                continue

        return container_ops

    def _attach_files(self, container_ops: list[dict[str, Any]]) -> ComponentResult:
        """Apply the staged files to OpenProject before the staging directory is removed."""
        if not container_ops:
            return ComponentResult(success=True, updated=0, data={"attachment_mapping": {}})

        # Within a single batch, multiple ops may target the same
        # ``(work_package_id, filename)`` — e.g. a Jira issue with
        # multiple ``logobottom.gif`` attachments. The Rails idempotency
        # check (``LOWER(filename) = ?``) keeps the first and skips
        # the rest, silently losing duplicates. Suffix-disambiguate
        # in-batch so each op presents a unique filename to Rails.
        # The matching audit-side disambiguation lives in
        # ``AttachmentRecoveryMigration._iter_jira_issues_with_attachments``.
        # Live 2026-05-07 NRS: 95 still_missing files traced largely to
        # this collision class (NRS-3363 alone: 13 distinct duplicate
        # filename pairs == 26 files).
        container_ops = self._disambiguate_duplicate_filenames(container_ops)

        ruby_runner = r"""
require "digest"
require "json"
start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'
end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'
result = begin
  results = []
  errors = []
  input_data.each do |op|
    begin
      wp = WorkPackage.find(op.fetch("work_package_id"))
      name = op.fetch("filename")
      # The storage identifier also becomes the HTTP download name on OpenProject.
      # Accept one basename. Path components and unsafe header characters cannot be preserved.
      unless name.is_a?(String) && name.present? && name.bytesize <= 255 &&
             !name.match?(%r{[[:cntrl:]/\\;"]}) && ![".", ".."].include?(name)
        raise "Unsupported attachment filename"
      end
      path = op.fetch("container_path")
      size = File.size(path)
      digest = Digest::SHA256.file(path).hexdigest
      raise "Staged attachment digest differs from source" if op["digest"] && op["digest"] != digest

      # Native uploads may have only the CarrierWave identifier. Older imports also
      # wrote the original name to the separate filename column.
      matches = wp.attachments.where("LOWER(filename) = LOWER(?) OR LOWER(file) = LOWER(?)", name, name).limit(2).to_a
      raise "Ambiguous existing attachment name" if matches.length > 1
      att = matches.first
      existed = !att.nil?
      unless existed
        att = Attachment.new(container: wp, author: User.where(admin: true).first!)
        File.open(path, "rb") do |source|
          att.file = source
          # The cache keeps its independently sanitized staging name. Only the final
          # uploader object receives the validated basename used to store and retrieve this file.
          att.file.define_singleton_method(:filename) { name }
          att.filename = name
          att.save!
        end
      end
      att.reload
      raise "Stored attachment filename differs" unless att.filename == name
      stored_digest = Digest::SHA256.file(att.diskfile.path).hexdigest
      unless att.filesize == size && stored_digest == digest
        raise "Stored attachment bytes differ"
      end
      results << { jira_key: op.fetch("jira_key"), filename: name, attachment_id: att.id, existed: existed,
                   verified_sha256: stored_digest }
    rescue => e
      errors << { jira_key: op["jira_key"], filename: op["filename"], error: e.message }
    end
  end
  { results: results, errors: errors }
rescue => e
  { results: [], errors: [{ error: e.message }] }
end
puts start_marker
puts result.to_json
puts end_marker
"""

        updated = 0
        failed = 0
        attachment_mapping: dict[str, dict[str, int]] = {}
        logger.info("_load: executing Rails script with %d staged attachments", len(container_ops))
        try:
            envelope = self.op_client.execute_script_with_data(ruby_runner, container_ops)
            logger.info("_load: Rails script completed")
            rows = self._attachment_rows(envelope, len(container_ops))
            if rows is None:
                return ComponentResult(
                    success=False, updated=0, failed=len(container_ops), data={"attachment_mapping": {}}
                )
            results, errors = rows
            if not self._attachment_acknowledgements_match(container_ops, results, errors):
                self._loss_counters["load_rails_malformed_data"] += len(container_ops)
                logger.warning("_load: Rails did not acknowledge the requested attachment identities")
                return ComponentResult(success=False, failed=len(container_ops), data={"attachment_mapping": {}})
            self._confirm_attachment_digests(container_ops, results)
            for result in results:
                attachment_mapping.setdefault(result["jira_key"], {})[result["filename"]] = result["attachment_id"]
                if not result.get("existed"):
                    updated += 1
            failed = len(errors)
            if errors:
                self._loss_counters["load_rails_per_op_error"] += failed
                sample = errors[:5]
                logger.warning("_load: Rails returned %d per-op errors (sample of %d): %s", failed, len(sample), sample)
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

    def _confirm_attachment_digests(self, ops: list[dict], results: list[dict]) -> None:
        if self._receipts is None:
            return
        by_name = {(op["jira_key"], op["filename"]): op for op in ops}
        if len(by_name) != len(ops):
            return
        for row in results:
            op = by_name[row["jira_key"], row["filename"]]
            if op.get("jira_attachment_id") and op.get("digest") and op["digest"] == row.get("verified_sha256"):
                self._receipts.confirm(row["jira_key"], op["jira_attachment_id"], row["attachment_id"])

    @staticmethod
    def _attachment_acknowledgements_match(ops: list, results: list, errors: list) -> bool:
        """Require one response per requested attachment and one positive target ID per success."""
        rows = results + errors
        if len(rows) != len(ops) or any(not isinstance(row, dict) for row in rows):
            return False
        identities = Counter((op["jira_key"], op["filename"]) for op in ops)
        if Counter((row.get("jira_key"), row.get("filename")) for row in rows) != identities:
            return False
        ids = [row.get("attachment_id") for row in results]
        return all(type(value) is int and value > 0 for value in ids) and len(set(ids)) == len(ids)

    def _attachment_rows(self, envelope: Any, count: int) -> tuple[list, list] | None:
        """Validate the Rails runner envelope before treating the batch as complete."""
        if not isinstance(envelope, dict) or envelope.get("status") != "success":
            logger.warning("_load: Rails did not return a successful attachment response")
            self._loss_counters["load_rails_status_not_success"] += count
            return None
        data = envelope.get("data")
        if isinstance(data, dict) and isinstance(data.get("results"), list) and isinstance(data.get("errors"), list):
            return data["results"], data["errors"]
        logger.warning("_load: Rails attachment results or errors have an unexpected format")
        self._loss_counters["load_rails_malformed_data"] += count
        return None

    @staticmethod
    def disambiguate_duplicate_filenames_in_group(
        names: list[str],
    ) -> list[str]:
        """Return a list of unique filenames given an input list that
        may contain duplicates (case-insensitive).

        First occurrence keeps its original name. Each subsequent
        duplicate gets a ``" (N)"`` suffix inserted before the extension.

        Examples:
            ``["a.png", "a.png", "A.PNG"]`` →
            ``["a.png", "a (2).png", "A (3).PNG"]``
            ``["doc"]`` → ``["doc"]``  (no extension)
            ``["doc", "doc"]`` → ``["doc", "doc (2)"]``

        Naming scheme matches what browsers use when downloading
        duplicate files — natural and human-readable.

        """
        used_lower: dict[str, int] = {}
        out: list[str] = []
        for name in names:
            key = name.lower()
            n = used_lower.get(key, 0)
            if n == 0:
                out.append(name)
                used_lower[key] = 1
                continue
            # Find a free suffix. The base name + extension live on
            # either side of the LAST dot (no dot → bare name, append
            # at end). Dotfiles (``.bashrc`` → stem="", ext="bashrc")
            # would otherwise become ``" (2).bashrc"`` (leading space,
            # base lost) — treat them as bare-name + ext-less so the
            # suffix lands at the end. Per PR #216 review.
            stem, dot, ext = name.rpartition(".")
            if dot and stem:
                base = stem
                tail = dot + ext
            else:
                base = name
                tail = ""
            counter = n + 1
            while True:
                candidate = f"{base} ({counter}){tail}"
                if candidate.lower() not in used_lower:
                    out.append(candidate)
                    used_lower[candidate.lower()] = 1
                    used_lower[key] = counter
                    break
                counter += 1
        return out

    def _disambiguate_duplicate_filenames(
        self,
        ops: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Disambiguate duplicate filenames per work-package group.

        Each op's ``filename`` is rewritten so that within a single
        ``work_package_id``, no two ops collide on
        ``LOWER(filename)`` — Rails's idempotency check would
        otherwise drop the duplicates. The disambiguator preserves
        original input order so the first-seen filename keeps its
        original name.
        """
        by_wp: dict[int, list[int]] = {}
        for idx, op in enumerate(ops):
            wp = int(op["work_package_id"])
            by_wp.setdefault(wp, []).append(idx)

        renamed = list(ops)
        for indices in by_wp.values():
            if len(indices) <= 1:
                continue
            originals = [str(renamed[i]["filename"]) for i in indices]
            unique = self.disambiguate_duplicate_filenames_in_group(originals)
            for i, new_name in zip(indices, unique, strict=True):
                if new_name != renamed[i]["filename"]:
                    # Mutate a fresh dict so the caller's payload
                    # isn't surprised by an in-place edit.
                    new_op = dict(renamed[i])
                    new_op["filename"] = new_name
                    renamed[i] = new_op
        return renamed

    @staticmethod
    def _build_attachment_max_size_script(value_kb: int | None) -> str:
        """Ruby that reads or writes ``Setting.attachment_max_size``.

        ``value_kb is None`` → read; otherwise write that value first.
        Output contract matches ``execute_script_with_data``: emit
        ``{"value": int}`` between the runner's start/end markers so
        ``envelope['data']`` carries it cleanly. The plain
        ``execute_query`` path returns the raw tmux buffer which can
        be polluted with leftover output from prior commands — caught
        by the live 2026-05-07 NRS run where the read returned a
        kilobyte-long JSON blob from a prior recovery query.
        """
        prelude = "" if value_kb is None else f"Setting.attachment_max_size = {int(value_kb)}\n"
        return f"""
require 'json'
start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'
end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'
{prelude}value = Setting.attachment_max_size.to_i
puts start_marker
puts({{value: value}}.to_json)
puts end_marker
"""

    def _read_envelope_value(self, envelope: object) -> int | None:
        if not isinstance(envelope, dict):
            return None
        if envelope.get("status") != "success":
            logger.warning(
                "attachment_max_size read/write returned status=%r message=%r",
                envelope.get("status"),
                envelope.get("message"),
            )
            return None
        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            return None
        try:
            return int(data.get("value"))
        except TypeError, ValueError:
            return None

    def _get_op_attachment_max_kb(self) -> int | None:
        """Return OP's current ``Setting.attachment_max_size`` in KB.

        Uses the marker-fenced ``execute_script_with_data`` envelope
        (same pattern :class:`AttachmentRecoveryMigration` uses) so the
        return value is parsed JSON, not raw tmux buffer output.
        Returns ``None`` if the value can't be read — the caller treats
        that as "don't bump".
        """
        script = self._build_attachment_max_size_script(None)
        try:
            envelope = self.op_client.execute_script_with_data(script, [])
        except Exception:
            logger.exception("Failed to read Setting.attachment_max_size")
            return None
        return self._read_envelope_value(envelope)

    def _set_op_attachment_max_kb(self, value_kb: int) -> bool:
        """Write ``Setting.attachment_max_size = value_kb`` (in KB).

        Returns ``True`` only when the write round-trip emits the
        new value back through the envelope — confirms the write
        actually took effect rather than just trusting a fire-and-
        forget `puts` over tmux.
        """
        script = self._build_attachment_max_size_script(int(value_kb))
        try:
            envelope = self.op_client.execute_script_with_data(script, [])
        except Exception:
            logger.exception("Failed to write Setting.attachment_max_size=%d", value_kb)
            return False
        observed = self._read_envelope_value(envelope)
        if observed != int(value_kb):
            logger.warning(
                "attachment_max_size write did not stick: wanted=%d observed=%r",
                value_kb,
                observed,
            )
            return False
        return True

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
        before = self._loss_counters["extract_level_changed"]
        att_by_key = self._extract_batch(jira_keys)
        # A level issue whose level changed since the selection is a failure of this batch.
        changed = self._loss_counters["extract_level_changed"] - before
        logger.info("_process_batch_end_to_end: extracted %d issues with attachments", len(att_by_key))
        if not att_by_key:
            return 0, changed, {}

        # Single normalisation source — see ``_wp_lookup_by_jira_key``.
        key_to_wp = self._wp_lookup_by_jira_key()

        ops = self._attachment_operations(att_by_key, key_to_wp)

        if not ops:
            logger.info("_process_batch_end_to_end: no ops to load, returning early")
            return 0, changed, {}

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
            (loaded.failed or 0) + changed,
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
        self._level_work_packages = {}
        self._level_ids = {}

        # Build the canonical Jira-key → wp-id lookup once, then group
        # the resolved Jira keys by project for batch-friendly processing.
        # See :meth:`_wp_lookup_by_jira_key` for normalisation rules.
        # Before the size cap is raised: a missing level report stops the component here.
        stopped = self._prepare_level_work_packages(self._wp_lookup_by_jira_key())
        if stopped is not None:
            return stopped
        # Receipts compare ordinary issues only; level keys only form batches below, and each
        # one's target is the work package its sub-project holds.
        self._receipts = ScopedReceipts(dict(self._wp_lookup_cache or {}))
        lookup = self._wp_lookup_by_jira_key()
        if not lookup:
            return self._missing_work_package_mapping()

        by_project: dict[str, list[str]] = {}
        for jira_key in lookup:
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

        # Pre-flight: ensure OP's ``Setting.attachment_max_size`` is
        # high enough that Rails won't reject oversized files with
        # ``Validation failed: File is too large``. The 2026-05-07
        # NRS audit traced 34 silent attachment losses to this 5 MB
        # cap. ``J2O_ATTACHMENT_MAX_KB`` (default 1 GiB) controls the
        # bumped value; the original is restored in ``finally`` so a
        # crash mid-run doesn't leave the cap permanently elevated.
        target_max_kb = int(os.environ.get("J2O_ATTACHMENT_MAX_KB", str(1024 * 1024)))
        original_max_kb = self._get_op_attachment_max_kb()
        cap_was_raised = False
        if original_max_kb is not None and target_max_kb > original_max_kb:
            if self._set_op_attachment_max_kb(target_max_kb):
                cap_was_raised = True
                self.logger.warning(
                    "Raised OP Setting.attachment_max_size from %d KB to %d KB"
                    " for the duration of this run; will restore in finally.",
                    original_max_kb,
                    target_max_kb,
                )
            else:
                self.logger.error(
                    "Could not raise OP attachment_max_size; oversized files"
                    " (>%d KB) will be rejected by Rails validation.",
                    original_max_kb,
                )

        try:
            result = self._run_per_project_loop(
                by_project=by_project,
                batch_size=batch_size,
                total_updated=total_updated,
                total_failed=total_failed,
                all_mappings=all_mappings,
            )
            return self._with_level_report(result)
        finally:
            if cap_was_raised and original_max_kb is not None:
                if self._set_op_attachment_max_kb(original_max_kb):
                    self.logger.info(
                        "Restored OP Setting.attachment_max_size to %d KB",
                        original_max_kb,
                    )
                else:
                    self.logger.error(
                        "FAILED to restore OP attachment_max_size to %d KB —"
                        " operator must reset manually (current: %d KB)",
                        original_max_kb,
                        target_max_kb,
                    )

    def _prepare_level_work_packages(self, key_to_wp_id: dict[str, int]) -> ComponentResult | None:
        """With the level switch on, resolve this run's level work packages (ADR-009).

        The ordinary lookup loses every issue of a level search or of the archive. Returns the
        failed result when the level reports are missing or malformed, else None.
        """
        self._level = None
        if not level_issues.enabled():
            return None
        try:
            level = level_issues.level_work_packages(self.jira_client, self.op_client, self.mappings, self.logger)
        except LevelProjectError as exc:
            self.logger.error("Level work package attachments stopped: %s", exc)
            return ComponentResult(success=False, message=str(exc), errors=["security_level"])
        self._wp_lookup_cache = level.without_level_issues(key_to_wp_id)
        self._level_work_packages = dict(level.work_packages)
        self._level_ids = dict(level.level_ids)
        self._level = level
        return None

    def _with_level_report(self, result: ComponentResult) -> ComponentResult:
        """Count the level refusals of this run into the result."""
        level = getattr(self, "_level", None)
        return result if level is None else level.report(result, self.logger)

    def _missing_work_package_mapping(self) -> ComponentResult:
        """Distinguish an absent mapping from one containing only unusable rows."""
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

    def _run_per_project_loop(
        self,
        *,
        by_project: dict[str, list[str]],
        batch_size: int,
        total_updated: int,
        total_failed: int,
        all_mappings: dict[str, dict[str, int]],
    ) -> ComponentResult:
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

        total_failed += self._loss_counters["extract_unread_issues"]
        success = total_failed == 0
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
            details={
                "loss_counters": loss_counters,
                "comparison_by_project": self._receipts.counts() if self._receipts else {},
            },
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
