# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Rewrite inline image/file references to point to OpenProject attachments.

A minimal Rails script is used to, per work package:
- build a set of attachment filenames
- rewrite markdown link/img targets whose URL ends with one of the filenames
  into the form `(attachment:filename)`
- apply the same rewrite to comment journals' notes
"""

from __future__ import annotations

from src.application.components import level_issues, written_values
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.security_level_projects_migration import LevelProjectError
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.written_guard import WRITTEN_GUARD
from src.models import ComponentResult, WorkPackageMappingEntry


@register_entity_types("inline_refs")
class InlineRefsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _extract(self) -> ComponentResult:
        wp_map = self.mappings.get_mapping("work_package") or {}
        wp_ids: list[int] = []
        level = getattr(self, "_level", None)
        if level is not None:
            wp_map = self._without_level_issues(level, wp_map)
        for key, value in (wp_map or {}).items():
            try:
                entry = WorkPackageMappingEntry.from_legacy(str(key), value)
            except ValueError:
                # Corrupt or unsupported shape — skip silently to preserve the
                # pre-typed silent-skip behaviour at this call site.
                continue
            # ``entry.openproject_id`` is a branded int type (validated by
            # Pydantic on construction) — no further coercion needed.
            wp_ids.append(entry.openproject_id)
        if level is not None:
            # A level work package's references point at its own attachments, in its sub-project.
            wp_ids.extend(level.work_packages[key] for key in sorted(level.work_packages))
        return ComponentResult(success=True, data={"work_package_ids": wp_ids})

    def _without_level_issues(self, level: level_issues.LevelWorkPackages, wp_map: dict) -> dict:
        """The ordinary entries without an issue of a level search or of the archive (ADR-009)."""
        by_key = {self._inner_jira_key(str(outer), value) or str(outer): outer for outer, value in wp_map.items()}
        kept = level.without_level_issues(dict.fromkeys(by_key, 0))
        return {outer: wp_map[outer] for key, outer in by_key.items() if key in kept}

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        ids = data.get("work_package_ids", []) if isinstance(data, dict) else []
        if not ids:
            return ComponentResult(success=True, updated=0)

        # A rewrite of the text J2O wrote last keeps its digest current (ADR-013), so the next
        # content run does not take it for an edit made in OpenProject.
        store = written_values.store()
        expect = store.expected((wp_id, "description") for wp_id in ids)
        items = [{"id": wp_id, "expect": expect.get((wp_id, "description"))} for wp_id in ids]
        script = WRITTEN_GUARD + (
            "require 'json'\n"
            "items = input_data || []\n"
            "updated = 0; failed = 0; digests = {}\n"
            "items.each do |item|\n"
            "  id = item.fetch('id')\n"
            "  begin\n"
            "    wp = WorkPackage.find(id)\n"
            "    names = wp.attachments.pluck(:filename)\n"
            "    if names.any?\n"
            "      union = Regexp.union(names)\n"
            "      re = /\\((?:[^()]*\\/)?(#{union})\\)/i\n"
            "      # Description\n"
            "      desc = (wp.description || '').to_s\n"
            '      new_desc = desc.gsub(re) { "(attachment:#{$1})" }\n'
            "      if new_desc != desc\n"
            "        kept = item['expect'] && j2o_digest.call(j2o_stored.call(wp, 'description')) == item['expect']\n"
            "        wp.description = new_desc\n"
            "        wp.save!\n"
            "        digests[id] = j2o_digest.call(j2o_stored.call(wp, 'description')) if kept\n"
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
            "puts start_marker + {updated: updated, failed: failed, digests: digests}.to_json + end_marker\n"
        )
        res = self.op_client.execute_script_with_data(script, items)
        # The runner wraps the script's JSON as {"status": ..., "data": ...}.
        answer = res.get("data") if isinstance(res, dict) and res.get("status") == "success" else None
        if not isinstance(answer, dict):
            message = res.get("message") if isinstance(res, dict) else res
            self.logger.error("Inline reference rewrite not confirmed: %s", message)
            return ComponentResult(success=False, updated=0, failed=len(ids))
        digests = answer.get("digests") or {}
        store.record((int(wp_id), "description", str(digest)) for wp_id, digest in digests.items())
        updated = int(answer.get("updated", 0))
        failed = int(answer.get("failed", 0))
        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Inline refs migration.

        With the level switch on, the level work packages of this run are resolved before any
        write (ADR-009) and rewritten like every other one; an ordinary entry of an issue of a
        level search or of the archive is refused. The refusals are counted on every return.
        """
        self._level: level_issues.LevelWorkPackages | None = None
        if level_issues.enabled():
            try:
                self._level = level_issues.level_work_packages(
                    self.jira_client, self.op_client, self.mappings, self.logger
                )
            except LevelProjectError as exc:
                self.logger.error("Level work package inline references stopped: %s", exc)
                return ComponentResult(success=False, message=str(exc), errors=["security_level"])
        result = self._run_etl_pipeline("Inline refs")
        return result if self._level is None else self._level.report(result, self.logger)
