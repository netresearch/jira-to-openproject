"""Migrate Jira remote/web links to OpenProject by writing a description section.

Renders a section "Remote Links" containing markdown bullets of [title](url).
This avoids CF proliferation and presents links inline for users.
"""

from __future__ import annotations

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry
from src.models.jira import JiraIssueFields

SECTION_TITLE = "Remote Links"


@register_entity_types("remote_links")
class RemoteLinksMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    @staticmethod
    def _extract_links_from_fields(fields: JiraIssueFields) -> list[tuple[str, str]]:
        """Return ``(title, url)`` pairs from a typed :class:`JiraIssueFields`.

        ``fields.remote_links`` already holds the boundary-flattened
        :class:`JiraRemoteLinkRef` payloads — we only need to drop entries
        with a missing/non-http URL and fill in a sensible title fallback.
        """
        pairs: list[tuple[str, str]] = []
        for ref in fields.remote_links:
            url = ref.url
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            url = url.strip()
            title = ref.title.strip() if isinstance(ref.title, str) and ref.title.strip() else url
            pairs.append((title, url))
        return pairs

    def _extract(self) -> ComponentResult:
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = self._jira_keys_from_wp_map(wp_map)
        if not keys:
            return ComponentResult(success=True, data={"links": {}})
        issues = self._merge_batch_issues(keys)
        links_by_key: dict[str, list[tuple[str, str]]] = {}
        for k, issue in issues.items():
            try:
                fields = JiraIssueFields.from_issue_any(issue)
                pairs = self._extract_links_from_fields(fields)
                if pairs:
                    links_by_key[k] = pairs
            except Exception:
                continue
        return ComponentResult(success=True, data={"links": links_by_key})

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
        return ComponentResult(success=True, data={"markdown": md_by_key})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        md_by_key: dict[str, str] = data.get("markdown", {}) if isinstance(data, dict) else {}
        wp_map = self.mappings.get_mapping("work_package") or {}

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
        failed = 0
        if sections_to_upsert:
            logger.info("Bulk upserting %d remote link sections...", len(sections_to_upsert))
            bulk_result = self.op_client.bulk_upsert_wp_description_sections(sections_to_upsert)
            updated = bulk_result.get("updated", 0)
            failed = bulk_result.get("failed", 0)
            logger.info("Bulk remote links: updated=%d, failed=%d", updated, failed)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Remote links migration."""
        return self._run_etl_pipeline("Remote links")
