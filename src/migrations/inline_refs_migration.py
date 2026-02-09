"""Rewrite inline image/file references to point to OpenProject attachments.

A minimal Rails script is used to, per work package:
- build a set of attachment filenames
- rewrite markdown link/img targets whose URL ends with one of the filenames
  into the form `(attachment:filename)`
- apply the same rewrite to comment journals' notes
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient


@register_entity_types("inline_refs")
class InlineRefsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _extract(self) -> ComponentResult:
        wp_map = self.mappings.get_mapping("work_package") or {}
        wp_ids: list[int] = []
        for entry in (wp_map or {}).values():
            if isinstance(entry, dict) and entry.get("openproject_id"):
                try:
                    wp_ids.append(int(entry["openproject_id"]))  # type: ignore[arg-type]
                except Exception:
                    continue
        return ComponentResult(success=True, data={"work_package_ids": wp_ids})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        ids = data.get("work_package_ids", []) if isinstance(data, dict) else []
        if not ids:
            return ComponentResult(success=True, updated=0)

        script = (
            "require 'json'\n"
            "ids = input_data || []\n"
            "updated = 0; failed = 0\n"
            "ids.each do |id|\n"
            "  begin\n"
            "    wp = WorkPackage.find(id)\n"
            "    names = wp.attachments.pluck(:filename)\n"
            "    if names.any?\n"
            "      union = Regexp.union(names.map { |n| Regexp.escape(n) })\n"
            "      re = /\\((?:[^()]*\\/)?(#{union})\\)/i\n"
            "      # Description\n"
            "      desc = (wp.description || '').to_s\n"
            '      new_desc = desc.gsub(re) { "(attachment:#{$1})" }\n'
            "      if new_desc != desc\n"
            "        wp.description = new_desc\n"
            "        wp.save!\n"
            "        updated += 1\n"
            "      end\n"
            "      # Comments\n"
            "      Journal.where(journable: wp).where.not(notes: [nil, '']).find_each do |j|\n"
            "        notes = j.notes.to_s\n"
            '        new_notes = notes.gsub(re) { "(attachment:#{$1})" }\n'
            "        if new_notes != notes\n"
            "          j.update_columns(notes: new_notes)\n"
            "          updated += 1\n"
            "        end\n"
            "      end\n"
            "    end\n"
            "  rescue => e\n"
            "    failed += 1\n"
            "  end\n"
            "end\n"
            "start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'\n"
            "end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'\n"
            "puts start_marker + {updated: updated, failed: failed}.to_json + end_marker\n"
        )
        res = self.op_client.execute_script_with_data(script, ids)
        updated = int(res.get("updated", 0)) if isinstance(res, dict) else 0
        failed = int(res.get("failed", 0)) if isinstance(res, dict) else 0
        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Inline refs migration."""
        return self._run_etl_pipeline("Inline refs")
