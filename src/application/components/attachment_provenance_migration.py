"""Preserve attachment author and timestamp in OpenProject.

Reads Jira attachments for mapped issues, resolves OP user IDs for authors,
and updates existing OP attachments (matched by filename on the WP) to set
author and created_at.

Note on dict access patterns kept here
--------------------------------------
The ``user`` mapping resolved by :meth:`_resolve_user_id` is a flat
dict whose values are dicts with ``openproject_id``; there is no
typed user-mapping model yet, so the narrow lookup ladder remains.
The Jira side (``fields.attachment``) is parsed at the boundary
through :class:`JiraIssueFields.from_issue_any`, and the polymorphic
work-package mapping reads use
:class:`WorkPackageMappingEntry.from_legacy`.
"""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, JiraUser, WorkPackageMappingEntry
from src.models.jira import JiraIssueFields


@register_entity_types("attachment_provenance")
class AttachmentProvenanceMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    @staticmethod
    def _author_identifiers(author: Any) -> list[str]:
        """Return the candidate identifier strings from an attachment author.

        Accepts a typed :class:`JiraUser`, the legacy dict shape, or an
        SDK-like object. Probe order matches the legacy code:
        ``accountId`` → ``name`` → ``key`` → ``emailAddress`` →
        ``email`` → ``displayName``. ``email`` is a non-standard alias
        the legacy code probed for; we keep it for back-compat with
        upstream user-mapping fixtures that use it.
        """
        if author is None:
            return []
        if isinstance(author, JiraUser):
            ordered = [
                author.account_id,
                author.name,
                author.key,
                author.email_address,
                author.email_address,  # legacy "email" alias points to the same field
                author.display_name,
            ]
            return [v for v in ordered if isinstance(v, str)]

        def _read(key: str) -> Any:
            if isinstance(author, dict):
                return author.get(key)
            return getattr(author, key, None)

        out: list[str] = []
        for k in ("accountId", "name", "key", "emailAddress", "email", "displayName"):
            v = _read(k)
            if isinstance(v, str):
                out.append(v)
        return out

    def _resolve_user_id(self, author: Any) -> int | None:
        try:
            umap = self.mappings.get_mapping("user") or {}
            for v in self._author_identifiers(author):
                if v in umap:
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
            try:
                fields = JiraIssueFields.from_issue_any(issue)
            except Exception:
                continue
            for a in fields.attachments:
                filename = a.filename
                if not isinstance(filename, str) or not filename.strip():
                    continue
                items.append(
                    {
                        "jira_key": k,
                        "filename": filename,
                        "created": a.created,
                        # Pass the typed author through so ``_resolve_user_id``
                        # can probe its identifier fields uniformly.
                        "author": a.author,
                    },
                )
        return items

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        items = data.get("items", []) if isinstance(data, dict) else []
        wp_map = self.mappings.get_mapping("work_package") or {}

        # Build a lookup from jira_key to typed mapping entry. The
        # outer wp_map key is ``str(jira_id)`` (numeric id), while the
        # inner record carries the human-readable ``jira_key`` — we
        # index by inner ``jira_key`` so attachment items (keyed by
        # the same human-readable key) can join cleanly.
        key_to_entry: dict[str, WorkPackageMappingEntry] = {}
        for raw_entry in wp_map.values():
            inner_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
            if not inner_key:
                continue
            try:
                key_to_entry[str(inner_key)] = WorkPackageMappingEntry.from_legacy(str(inner_key), raw_entry)
            except ValueError:
                continue

        out: list[dict[str, Any]] = []
        for it in items:
            jira_key = it.get("jira_key")
            entry = key_to_entry.get(jira_key)
            if entry is None:
                continue
            wp_id = int(entry.openproject_id)
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
            # FAIL LOUD. Same anti-pattern as the pre-#194 attachments
            # path — ``success=True`` here masks the real cause
            # (upstream ``work_packages_skeleton`` didn't persist
            # its mapping, e.g. ``_save_mapping`` swallowed a write
            # error per #197). Without the WP map this component
            # has nothing to do; surface the missing precondition
            # so the orchestrator's partial-success classifier
            # flags the run instead of cascading silent successes.
            msg = (
                "No work_package mapping available — attachment provenance"
                " cannot run. Run work_packages_skeleton first (or verify"
                " its mapping persisted) and re-run this component."
            )
            self.logger.error(msg)
            return ComponentResult(
                success=False,
                updated=0,
                message=msg,
                errors=["missing_work_package_mapping"],
            )

        # Group jira keys by project for efficient processing. Each entry
        # is normalised through :class:`WorkPackageMappingEntry.from_legacy`
        # — the inner ``jira_key`` is the source of truth (the outer
        # wp_map key is ``str(jira_id)`` in production).
        by_project: dict[str, list[str]] = {}
        for raw_entry in wp_map.values():
            inner_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
            if not inner_key:
                continue
            try:
                entry = WorkPackageMappingEntry.from_legacy(str(inner_key), raw_entry)
            except ValueError:
                continue
            jira_key = str(entry.jira_key)
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
