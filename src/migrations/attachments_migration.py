"""Migrate Jira attachments to OpenProject and bind to work packages.

Flow:
- Extract: collect attachments for Jira issues already mapped to work packages.
- Map: download to local attachment path, compute sha256, deduplicate by digest.
- Load: copy files to container and run a minimal Rails script to attach files
  to the corresponding work packages idempotently (skip if same filename exists).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

from src import config
from src.config import logger


@register_entity_types("attachments")
class AttachmentsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        # Attachment directory
        try:
            ap = config.migration_config.get("attachment_path")  # type: ignore[assignment]
        except Exception:
            ap = None
        self.attachment_dir: Path = Path(ap or (Path(self.data_dir) / "attachments"))
        self.attachment_dir.mkdir(parents=True, exist_ok=True)

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
                        if not filename or not url:
                            continue
                        items.append({"id": aid, "filename": filename, "size": size, "url": url})
                    except Exception:
                        continue
                if items:
                    by_key[key] = items
            except Exception:
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

        wp_map = self.mappings.get_mapping("work_package") or {}
        ops: list[dict[str, Any]] = []
        seen_digests: set[str] = set()

        for key, items in att_by_key.items():
            entry = wp_map.get(key)
            wp_id = None
            if isinstance(entry, dict):
                wp_id = entry.get("openproject_id")
            elif isinstance(entry, int):
                wp_id = entry
            if not wp_id:
                continue

            for item in items:
                try:
                    filename = str(item.get("filename"))
                    url = str(item.get("url"))
                    if not filename or not url:
                        continue
                    safe_name = filename.replace("/", "_")
                    local_path = self.attachment_dir / safe_name
                    # Download and hash
                    self._download_attachment(url, local_path)
                    if not local_path.exists():
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
                continue

        if not container_ops:
            return ComponentResult(success=True, updated=0, data={"attachment_mapping": {}})

        # Rails script returns attachment IDs for mapping content migration
        # Must output JSON between markers for execute_script_with_data to parse
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
            "      # Check if an attachment with same name already exists\n"
            "      existing = wp.attachments.where('LOWER(filename) = ?', fname.to_s.downcase).first\n"
            "      if existing\n"
            "        # Return existing attachment ID for mapping\n"
            "        results << { jira_key: jira_key, filename: fname, attachment_id: existing.id, existed: true }\n"
            "        next\n"
            "      end\n"
            "      path = op['container_path']\n"
            "      file = File.open(path, 'rb')\n"
            "      author = User.where(admin: true).first\n"
            "      att = Attachment.new(container: wp, author: author)\n"
            "      # Assign file using Paperclip-style API\n"
            "      if att.respond_to?(:file=)\n"
            "        att.file = file\n"
            "      end\n"
            "      att.filename = fname if att.respond_to?(:filename=)\n"
            "      att.save!\n"
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
            res = self.op_client.execute_script_with_data(ruby_runner, container_ops)
            logger.info("_load: Rails script completed")
            if isinstance(res, dict):
                results = res.get("results", [])
                errors = res.get("errors", [])
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
        except Exception:
            logger.exception("Rails attach operation failed")
            failed = len(container_ops)

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
        self, jira_keys: list[str], wp_map: dict[str, Any],
    ) -> tuple[int, int, dict[str, dict[str, int]]]:
        """Process a batch of issues: extract, download, upload, attach.

        Args:
            jira_keys: List of Jira issue keys to process
            wp_map: Work package mapping dict

        Returns:
            Tuple of (updated_count, failed_count, attachment_mapping)

        """
        # Extract attachment metadata for this batch
        logger.info("_process_batch_end_to_end: starting extract for %d keys", len(jira_keys))
        att_by_key = self._extract_batch(jira_keys)
        logger.info("_process_batch_end_to_end: extracted %d issues with attachments", len(att_by_key))
        if not att_by_key:
            return 0, 0, {}

        # Build reverse lookup: jira_key -> openproject_id for O(1) access
        key_to_wp = {
            entry.get("jira_key"): entry.get("openproject_id")
            for entry in wp_map.values()
            if isinstance(entry, dict) and entry.get("jira_key")
        }

        # Download attachments and prepare ops
        ops: list[dict[str, Any]] = []
        seen_digests: set[str] = set()

        for key, items in att_by_key.items():
            # Find work package ID from reverse lookup
            wp_id = key_to_wp.get(key)
            if not wp_id:
                continue

            for item in items:
                try:
                    filename = str(item.get("filename"))
                    url = str(item.get("url"))
                    if not filename or not url:
                        continue
                    safe_name = filename.replace("/", "_")
                    local_path = self.attachment_dir / safe_name
                    # Download
                    self._download_attachment(url, local_path)
                    if not local_path.exists():
                        continue
                    digest = self._sha256_of(local_path)
                    seen_digests.add(digest)
                    ops.append({
                        "jira_key": key,
                        "work_package_id": int(wp_id),
                        "local_path": local_path.as_posix(),
                        "filename": filename,
                        "digest": digest,
                    })
                except Exception:
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

        # Get work package mapping
        wp_map = self.mappings.get_mapping("work_package") or {}
        if not wp_map:
            self.logger.warning("No work package mapping found - skipping attachment migration")
            return ComponentResult(success=True, updated=0, message="No work packages to process")

        # Group jira keys by project for efficient processing
        by_project: dict[str, list[str]] = {}
        for entry in wp_map.values():
            if isinstance(entry, dict) and "jira_key" in entry:
                jira_key = str(entry["jira_key"])
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
                        batch_keys, wp_map,
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
        return ComponentResult(
            success=success,
            updated=total_updated,
            failed=total_failed,
            data={"attachment_mapping": all_mappings},
            message=f"Processed {total_updated} attachments, {total_failed} failures",
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
