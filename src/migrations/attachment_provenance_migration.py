"""Preserve attachment author and timestamp in OpenProject.

Reads Jira attachments for mapped issues, resolves OP user IDs for authors,
and updates existing OP attachments (matched by filename on the WP) to set
author and created_at.
"""

from __future__ import annotations

from typing import Any

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src import config
    from src.config import logger as logger  # type: ignore
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore


@register_entity_types("attachment_provenance")
class AttachmentProvenanceMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.mappings = config.mappings

    @staticmethod
    def _get(val: Any, key: str, default: Any = None) -> Any:
        if isinstance(val, dict):
            return val.get(key, default)
        return getattr(val, key, default)

    def _resolve_user_id(self, author: Any) -> int | None:
        try:
            umap = self.mappings.get_mapping("user") or {}
            # Try common Jira identifiers
            for k in ("accountId", "name", "key", "emailAddress", "email", "displayName"):
                v = self._get(author, k)
                if isinstance(v, str) and v in umap:
                    rec = umap[v]
                    if isinstance(rec, dict) and rec.get("openproject_id"):
                        return int(rec["openproject_id"])  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
        return None

    def _extract(self) -> ComponentResult:
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        if not keys:
            return ComponentResult(success=True, data={"items": []})
        issues = self.jira_client.batch_get_issues(keys)
        items: list[dict[str, Any]] = []
        for k, issue in issues.items():
            fields = getattr(issue, "fields", None)
            atts = []
            if fields is not None:
                atts = self._get(fields, "attachment", []) or []
            for a in atts or []:
                filename = self._get(a, "filename") or self._get(a, "name")
                created = self._get(a, "created")
                author = self._get(a, "author")
                if not isinstance(filename, str) or not filename.strip():
                    continue
                items.append({
                    "jira_key": k,
                    "filename": filename,
                    "created": created,
                    "author": author,
                })
        return ComponentResult(success=True, data={"items": items})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        items = data.get("items", []) if isinstance(data, dict) else []
        wp_map = self.mappings.get_mapping("work_package") or {}
        out: list[dict[str, Any]] = []
        for it in items:
            jira_key = it.get("jira_key")
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            author_id = self._resolve_user_id(it.get("author"))
            created_at = it.get("created") if isinstance(it.get("created"), str) else None
            out.append({
                "work_package_id": wp_id,
                "filename": it.get("filename"),
                "author_id": author_id,
                "created_at": created_at,
            })
        return ComponentResult(success=True, data={"updates": out})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        updates = data.get("updates", []) if isinstance(data, dict) else []
        if not updates:
            return ComponentResult(success=True, updated=0)
        script = (
            "require 'time'\n"
            "recs = ARGV.first\n"
            "updated = 0; failed = 0\n"
            "recs.each do |r|\n"
            "  begin\n"
            "    wp = WorkPackage.find(r['work_package_id'])\n"
            "    a = wp.attachments.where(filename: r['filename']).order('id DESC').first\n"
            "    next unless a\n"
            "    if r['author_id']; a.author = User.find(r['author_id']) rescue nil; end\n"
            "    if r['created_at']; begin; t = Time.parse(r['created_at']); a.created_at = t; rescue; end; end\n"
            "    a.save!\n"
            "    updated += 1\n"
            "  rescue => e\n"
            "    failed += 1\n"
            "  end\n"
            "end\n"
            "STDOUT.puts({updated: updated, failed: failed}.to_json)\n"
        )
        res = self.op_client.execute_script_with_data(script, updates)
        updated = int(res.get("updated", 0)) if isinstance(res, dict) else 0
        failed = int(res.get("failed", 0)) if isinstance(res, dict) else 0
        return ComponentResult(success=failed == 0, updated=updated, failed=failed)
    def run(self) -> ComponentResult:
        """Execute attachment provenance migration pipeline."""
        self.logger.info("Starting attachment provenance migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error("Attachment provenance extraction failed: %s", extracted.message or extracted.error)
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error("Attachment provenance mapping failed: %s", mapped.message or mapped.error)
            return mapped

        loaded = self._load(mapped)
        if loaded.success:
            self.logger.info("Attachment provenance migration completed (updated=%s, failed=%s)", loaded.updated, loaded.failed)
        else:
            self.logger.error("Attachment provenance migration encountered failures (failed=%s)", loaded.failed)
        return loaded
