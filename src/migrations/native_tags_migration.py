"""Migrate labels to native OpenProject tags (with color best-effort).

Extract Jira issue labels for mapped work packages, ensure Tag records exist,
and assign tags to each work package idempotently. If Tag has a `color`
attribute, set a deterministic color from the tag name hash.
"""

from __future__ import annotations

import hashlib
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


@register_entity_types("native_tags")
class NativeTagsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        import src.mappings as mappings  # noqa: PLC0415

        # Use configured data directory for mapping IO consistency, but
        # remain compatible with test doubles that expect zero-arg init.
        try:
            self.mappings = mappings.Mappings(config.get_path("data"))
        except TypeError:
            self.mappings = mappings.Mappings()

    @staticmethod
    def _coerce_labels(fields: Any) -> list[str]:
        out: list[str] = []
        if not fields:
            return out
        labels = getattr(fields, "labels", None)
        try:
            for v in labels or []:
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
        except Exception:  # noqa: BLE001
            return out
        return out

    @staticmethod
    def _name_to_color_hex(name: str) -> str:
        # Deterministic pleasant-ish color from name
        h = hashlib.sha256(name.encode("utf-8")).hexdigest()
        # Use middle bytes to avoid extremes; ensure good contrast probable
        r = int(h[8:10], 16)
        g = int(h[12:14], 16)
        b = int(h[16:18], 16)
        return f"#%02x%02x%02x" % (r, g, b)

    def _extract(self) -> ComponentResult:  # noqa: D401
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        if not keys:
            return ComponentResult(success=True, data={"by_key": {}})
        issues = self.jira_client.batch_get_issues(keys)
        by_key: dict[str, list[str]] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                labels = self._coerce_labels(fields)
                if labels:
                    by_key[k] = sorted(set(labels))
            except Exception:  # noqa: BLE001
                continue
        return ComponentResult(success=True, data={"by_key": by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:  # noqa: D401
        data = extracted.data or {}
        by_key: dict[str, list[str]] = data.get("by_key", {}) if isinstance(data, dict) else {}
        wp_map = self.mappings.get_mapping("work_package") or {}
        updates: list[dict[str, Any]] = []
        for jira_key, names in by_key.items():
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            tag_defs = [{"name": n, "color": self._name_to_color_hex(n)} for n in names]
            updates.append({"work_package_id": wp_id, "tags": tag_defs})
        return ComponentResult(success=True, data={"updates": updates})

    def _load(self, mapped: ComponentResult) -> ComponentResult:  # noqa: D401
        data = mapped.data or {}
        updates = data.get("updates", []) if isinstance(data, dict) else []
        if not updates:
            return ComponentResult(success=True, updated=0)
        # Script ensures Tag records, then assigns to WP via wp.tags= collection if available,
        # else tries tag_list interface.
        script = (
            "require 'json'\n"
            "recs = ARGV.first || []\n"
            "updated = 0; failed = 0\n"
            "recs.each do |r|\n"
            "  begin\n"
            "    wp = WorkPackage.find(r['work_package_id'])\n"
            "    tag_models = []\n"
            "    (r['tags'] || []).each do |t|\n"
            "      name = (t['name'] || '').to_s.strip\n"
            "      next if name.empty?\n"
            "      tag = nil\n"
            "      if defined?(Tag)\n"
            "        tag = Tag.where(name: name).first_or_initialize\n"
            "        if tag.respond_to?(:color) && t['color']\n"
            "          tag.color = t['color']\n"
            "        end\n"
            "        tag.save!\n"
            "        tag_models << tag\n"
            "      end\n"
            "    end\n"
            "    if tag_models.any? && wp.respond_to?(:tags)\n"
            "      wp.tags = tag_models\n"
            "      wp.save!\n"
            "      updated += 1\n"
            "    elsif (r['tags'] || []).any? && wp.respond_to?(:tag_list)\n"
            "      names = (r['tags'] || []).map { |x| x['name'] }.compact\n"
            "      wp.tag_list = names\n"
            "      wp.save!\n"
            "      updated += 1\n"
            "    end\n"
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


