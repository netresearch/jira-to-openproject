"""Preserve attachment author and timestamp in OpenProject.

Reads Jira attachments for mapped issues, resolves OP user IDs for authors,
and updates existing OP attachments (matched by filename on the WP) to set
author and created_at.
"""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult


@register_entity_types("attachment_provenance")
class AttachmentProvenanceMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

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
        except Exception:
            return None
        return None

    def _extract_batch(self, jira_keys: list[str]) -> list[dict[str, Any]]:
        """Extract attachment provenance for a batch of issues (memory-efficient).

        Args:
            jira_keys: List of Jira issue keys (e.g., ["AAP-1", "AAP-2"])

        Returns:
            List of attachment provenance items

        """
        if not jira_keys:
            return []
        try:
            issues: dict[str, Any] = self._merge_batch_issues(jira_keys)
        except Exception:
            logger.exception("Failed to batch-get issues for provenance")
            return []
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
                items.append(
                    {
                        "jira_key": k,
                        "filename": filename,
                        "created": created,
                        "author": author,
                    },
                )
        return items

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        items = data.get("items", []) if isinstance(data, dict) else []
        wp_map = self.mappings.get_mapping("work_package") or {}

        # Build a lookup from jira_key to entry since wp_map uses jira_id as keys
        key_to_entry: dict[str, dict[str, Any]] = {}
        for entry in wp_map.values():
            if isinstance(entry, dict) and "jira_key" in entry:
                key_to_entry[entry["jira_key"]] = entry

        out: list[dict[str, Any]] = []
        for it in items:
            jira_key = it.get("jira_key")
            entry = key_to_entry.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            author_id = self._resolve_user_id(it.get("author"))
            created_at = it.get("created") if isinstance(it.get("created"), str) else None
            out.append(
                {
                    "work_package_id": wp_id,
                    "filename": it.get("filename"),
                    "author_id": author_id,
                    "created_at": created_at,
                },
            )
        return ComponentResult(success=True, data={"updates": out})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        updates = data.get("updates", []) if isinstance(data, dict) else []
        if not updates:
            return ComponentResult(success=True, updated=0)
        script = (
            "require 'time'\n"
            "start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'\n"
            "end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'\n"
            "recs = input_data\n"
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
            "puts start_marker\n"
            "puts({updated: updated, failed: failed}.to_json)\n"
            "puts end_marker\n"
        )
        res = self.op_client.execute_script_with_data(script, updates)
        updated = int(res.get("updated", 0)) if isinstance(res, dict) else 0
        failed = int(res.get("failed", 0)) if isinstance(res, dict) else 0
        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Execute attachment provenance migration - memory efficient per-project."""
        self.logger.info("Starting attachment provenance migration (memory-efficient mode)")

        wp_map = self.mappings.get_mapping("work_package") or {}
        if not wp_map:
            self.logger.warning("No work package mapping found - skipping provenance migration")
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
            "Processing provenance for %d projects (%d issues total)",
            len(by_project),
            total_issues,
        )

        total_updated = 0
        total_failed = 0
        batch_size = 50  # Process 50 issues at a time

        for project_key, jira_keys in by_project.items():
            self.logger.info("Processing project %s (%d issues)", project_key, len(jira_keys))

            # Process in small batches
            for i in range(0, len(jira_keys), batch_size):
                batch_keys = jira_keys[i : i + batch_size]
                try:
                    # Extract provenance for this batch
                    items = self._extract_batch(batch_keys)
                    if not items:
                        continue

                    # Map to OP format
                    extracted = ComponentResult(success=True, data={"items": items})
                    mapped = self._map(extracted)
                    if not mapped.success:
                        total_failed += len(batch_keys)
                        continue

                    # Load to OP
                    loaded = self._load(mapped)
                    total_updated += loaded.updated or 0
                    total_failed += loaded.failed or 0
                except Exception:
                    self.logger.exception(
                        "Batch processing failed for project %s batch %d",
                        project_key,
                        i // batch_size,
                    )
                    total_failed += len(batch_keys)

        success = total_failed == 0 or (total_updated > 0 and total_failed < total_updated)
        self.logger.info(
            "Attachment provenance migration completed: %d updated, %d failed",
            total_updated,
            total_failed,
        )
        return ComponentResult(
            success=success,
            updated=total_updated,
            failed=total_failed,
            message=f"Processed {total_updated} provenances, {total_failed} failures",
        )
