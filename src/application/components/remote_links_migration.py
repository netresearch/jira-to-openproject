# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Jira remote/web links to OpenProject by writing a description section.

Renders a section "Remote Links" containing markdown bullets of [title](url).
This avoids CF proliferation and presents links inline for users.
"""

from __future__ import annotations

from src.application.components import written_values
from src.application.components.base_migration import register_entity_types
from src.application.components.level_fields import LevelFieldsMigration
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry

SECTION_TITLE = "Remote Links"


@register_entity_types("remote_links")
class RemoteLinksMigration(LevelFieldsMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    @staticmethod
    def _extract_links(remote_links: list[dict]) -> list[tuple[str, str]]:
        """``(title, url)`` pairs of an issue's ``remotelink`` answer; only http and https URLs."""
        pairs: list[tuple[str, str]] = []
        for link in remote_links:
            target = link.get("object") if isinstance(link.get("object"), dict) else {}
            url, title = target.get("url"), target.get("title")
            if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
                continue
            url = url.strip()
            pairs.append((title.strip() if isinstance(title, str) and title.strip() else url, url))
        return pairs

    def _extract(self) -> ComponentResult:
        wp_map = self._work_package_map()
        keys = self._jira_keys_from_wp_map(wp_map)
        if not keys:
            return ComponentResult(success=True, data={"links": {}, "read_failed": []})
        # Only issues the batch read or the level search returned; their links are a separate resource.
        issues = self._merge_batch_issues(keys)
        links_by_key: dict[str, list[tuple[str, str]]] = {}
        read_failed: list[str] = []
        for key in sorted(issues):
            try:
                pairs = self._extract_links(self.jira_client.get_issue_remote_links(key))
            except Exception:
                self.logger.exception("Could not read the remote links of %s", key)
                read_failed.append(key)
                continue
            if pairs:
                links_by_key[key] = pairs
        return ComponentResult(success=True, data={"links": links_by_key, "read_failed": read_failed})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        raw: dict[str, list[tuple[str, str]]] = data.get("links", {}) if isinstance(data, dict) else {}
        md_by_key: dict[str, str] = {}
        for key, pairs in raw.items():
            seen: set[str] = set()
            lines: list[str] = []
            for title, url in pairs:
                sig = f"{title}|{url}"
                if sig in seen:
                    continue
                seen.add(sig)
                safe_title = (title or url).replace("[", r"\[").replace("]", r"\]")
                lines.append(f"- [{safe_title}]({url})")
            if lines:
                md_by_key[key] = "\n".join(lines)
        failed = data.get("read_failed", []) if isinstance(data, dict) else []
        return ComponentResult(success=True, data={"markdown": md_by_key, "read_failed": failed})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        md_by_key: dict[str, str] = data.get("markdown", {}) if isinstance(data, dict) else {}
        wp_map = self._index_by_jira_key(self._work_package_map())

        # Collect all sections for bulk update
        sections_to_upsert: list[dict] = []
        for jira_key, md in md_by_key.items():
            raw_entry = wp_map.get(jira_key)
            if raw_entry is None:
                continue
            try:
                entry = WorkPackageMappingEntry.from_legacy(jira_key, raw_entry)
            except ValueError:
                # Corrupt or unsupported wp_map shape — skip silently to
                # preserve the pre-typed call-site behaviour.
                continue
            wp_id = int(entry.openproject_id)
            sections_to_upsert.append(
                {
                    "work_package_id": wp_id,
                    "section_marker": SECTION_TITLE,
                    "content": md,
                },
            )

        # Bulk upsert all sections in single Rails call
        updated = 0
        # An unreadable remote link answer is a failure, never an issue without links.
        failed = len(data.get("read_failed", [])) if isinstance(data, dict) else 0
        if sections_to_upsert:
            logger.info("Bulk upserting %d remote link sections...", len(sections_to_upsert))
            # The section rewrites the description J2O wrote; its digest follows (ADR-013).
            store = written_values.store()
            expect = store.expected((row["work_package_id"], "description") for row in sections_to_upsert)
            for row in sections_to_upsert:
                row["expect"] = expect.get((row["work_package_id"], "description"))
            bulk_result = self.op_client.bulk_upsert_wp_description_sections(sections_to_upsert)
            updated = bulk_result.get("updated", 0)
            failed += bulk_result.get("failed", 0)
            digests = bulk_result.get("digests") or {}
            store.record((int(wp), "description", str(digest)) for wp, digest in digests.items())
            logger.info("Bulk remote links: updated=%d, failed=%d", updated, failed)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Remote links migration, also for the level work packages (ADR-009)."""
        return self._with_level_fields(lambda: self._run_etl_pipeline("Remote links"))
