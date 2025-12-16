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

from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

try:
    from src import config
    from src.config import logger  # type: ignore
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore


@register_entity_types("attachments")
class AttachmentsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.mappings = config.mappings
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

    @staticmethod
    def _issue_project_key(issue_key: str) -> str:
        try:
            return str(issue_key).split("-", 1)[0]
        except Exception:
            return str(issue_key)

    def _extract(self) -> ComponentResult:
        """Collect attachments from Jira issues mapped to work packages."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        jira_keys = [str(k) for k in wp_map.keys()]
        if not jira_keys:
            return ComponentResult(success=True, extracted=0, data={"attachments": {}})

        issues: dict[str, Any] = {}
        try:
            batch_get = getattr(self.jira_client, "batch_get_issues", None)
            if callable(batch_get):
                issues = batch_get(jira_keys)
        except Exception:
            logger.exception("Failed to batch-get Jira issues for attachments extraction")
            issues = {}

        by_key: dict[str, list[dict[str, Any]]] = {}
        count = 0
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
                        count += 1
                    except Exception:  # noqa: BLE001
                        continue
                if items:
                    by_key[key] = items
            except Exception:  # noqa: BLE001
                continue

        return ComponentResult(success=True, extracted=count, data={"attachments": by_key})

    def _download_attachment(self, url: str, dest_path: Path) -> Path:
        """Download attachment from Jira to dest_path; return local path.

        Tests may monkeypatch this to avoid network IO.
        """
        import requests  # local import to ease testing

        try:
            with requests.get(url, stream=True, timeout=60) as r:  # type: ignore[call-arg]
                r.raise_for_status()
                with dest_path.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
        except Exception as e:  # noqa: BLE001
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
                except Exception:  # noqa: BLE001
                    continue

        return ComponentResult(success=True, data={"ops": ops})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        ops: list[dict[str, Any]] = (mapped.data or {}).get("ops", []) if mapped.data else []
        if not ops:
            return ComponentResult(success=True, updated=0, data={"attachment_mapping": {}})

        # Copy files to container and build data payload for Rails script
        container_ops: list[dict[str, Any]] = []
        for op in ops:
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
                    self.op_client.transfer_file_to_container(local_path, container_path)
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
            except Exception:  # noqa: BLE001
                continue

        if not container_ops:
            return ComponentResult(success=True, updated=0, data={"attachment_mapping": {}})

        # Rails script returns attachment IDs for mapping content migration
        ruby_runner = (
            "require 'json'\n"
            "begin\n"
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
        )

        updated = 0
        failed = 0
        attachment_mapping: dict[str, dict[str, int]] = {}
        try:
            res = self.op_client.execute_script_with_data(ruby_runner, container_ops)
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

    def run(self) -> ComponentResult:
        """Execute attachment migration pipeline."""
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
