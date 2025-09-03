"""Migrate Jira remote/web links to OpenProject by writing a description section.

Renders a section "Remote Links" containing markdown bullets of [title](url).
This avoids CF proliferation and presents links inline for users.
"""

from __future__ import annotations

from typing import Any

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src.config import logger as logger  # type: ignore
    from src import config
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore  # noqa: PLC0415


SECTION_TITLE = "Remote Links"


@register_entity_types("remote_links")
class RemoteLinksMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        import src.mappings as mappings  # noqa: PLC0415

        self.mappings = mappings.Mappings()

    @staticmethod
    def _extract_links_from_fields(fields: Any) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        if not fields:
            return links
        # Prefer dedicated property if present
        candidates = []
        for name in ("remotelinks", "remote_links", "webLinks", "weblinks", "issuelinks"):
            if hasattr(fields, name):
                candidates = getattr(fields, name) or []
                break
        try:
            for item in candidates or []:
                # Common shapes: dicts with 'object': {'url','title'} or direct {'url','title'}
                obj = None
                if isinstance(item, dict):
                    obj = item.get("object", item)
                else:
                    obj = getattr(item, "object", item)
                url = None
                title = None
                if isinstance(obj, dict):
                    url = obj.get("url")
                    title = obj.get("title") or obj.get("summary")
                else:
                    url = getattr(obj, "url", None)
                    title = getattr(obj, "title", None) or getattr(obj, "summary", None)
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    if not isinstance(title, str) or not title.strip():
                        title = url
                    links.append((title.strip(), url.strip()))
        except Exception:  # noqa: BLE001
            return links
        return links

    def _extract(self) -> ComponentResult:  # noqa: D401
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        if not keys:
            return ComponentResult(success=True, data={"links": {}})
        issues = self.jira_client.batch_get_issues(keys)
        links_by_key: dict[str, list[tuple[str, str]]] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                pairs = self._extract_links_from_fields(fields)
                if pairs:
                    links_by_key[k] = pairs
            except Exception:  # noqa: BLE001
                continue
        return ComponentResult(success=True, data={"links": links_by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:  # noqa: D401
        data = extracted.data or {}
        raw: dict[str, list[tuple[str, str]]]= data.get("links", {}) if isinstance(data, dict) else {}
        md_by_key: dict[str, str] = {}
        for key, pairs in raw.items():
            seen: set[str] = set()
            lines: list[str] = []
            for title, url in pairs:
                sig = f"{title}|{url}"
                if sig in seen:
                    continue
                seen.add(sig)
                safe_title = (title or url).replace("[", "\[").replace("]", "\]")
                lines.append(f"- [{safe_title}]({url})")
            if lines:
                md_by_key[key] = "\n".join(lines)
        return ComponentResult(success=True, data={"markdown": md_by_key})

    def _load(self, mapped: ComponentResult) -> ComponentResult:  # noqa: D401
        data = mapped.data or {}
        md_by_key: dict[str, str] = data.get("markdown", {}) if isinstance(data, dict) else {}
        wp_map = self.mappings.get_mapping("work_package") or {}

        updated = 0
        failed = 0
        for jira_key, md in md_by_key.items():
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            try:
                ok = self.op_client.upsert_work_package_description_section(
                    work_package_id=wp_id,
                    section_marker=SECTION_TITLE,
                    content=md,
                )
                if ok:
                    updated += 1
                else:
                    failed += 1
            except Exception:  # noqa: BLE001
                logger.exception("Failed to upsert Remote Links for %s", jira_key)
                failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)


