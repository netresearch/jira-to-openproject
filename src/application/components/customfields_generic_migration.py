# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Generic migration for unmapped Jira customfield_* values to OpenProject CFs.

Relies on existing custom field mapping (name/type) and ensures missing CFs
are created, then sets values on corresponding work packages.

Note on the dict access patterns kept here
------------------------------------------
This component intentionally retains a couple of structural dict reads
that other Phase 7 migrations have replaced with typed models:

* The Jira side iterates ``dir(fields)`` for dynamic ``customfield_*``
  attributes. These vary per tenant and are not modelled by
  :class:`JiraIssueFields` — a typed wrapper would have to fall back to
  attribute access on the raw fields object anyway, so we keep the
  direct ``getattr`` here.
* ``cf_mapping`` is the persisted CF-name/type table produced by the
  ``custom_field`` migration. It is a plain mapping of metadata, not a
  polymorphic ``int|dict`` ladder — typing it would not eliminate any
  ``isinstance`` branches at this call site.

The work-package mapping dict, on the other hand, IS the polymorphic
shape Phase 7 set out to retire — it is normalised through
:class:`WorkPackageMappingEntry.from_legacy` below.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from src.application.components import written_values
from src.application.components.base_migration import register_entity_types
from src.application.components.level_fields import LevelFieldsMigration
from src.application.components.list_field_options import is_sprint_field
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.written_guard import WRITTEN_GUARD
from src.models import ComponentResult, WorkPackageMappingEntry

# Written only while the target holds what J2O wrote last (ADR-013).
_WRITE_VALUE = (
    WRITTEN_GUARD
    + """
wp = WorkPackage.find(input_data.fetch('wp_id'))
field = WorkPackageCustomField.find(input_data.fetch('field_id'))
guard = input_data.fetch('guard')
attr = "customField#{field.id}"
raise "Guard for another field than #{attr}" unless guard.fetch('attr') == attr
status = digest = nil
wp.with_lock do
  value = input_data.fetch('value')
  before = j2o_stored.call(wp, attr)
  wanted = [CustomValue.new(custom_field: field, value: value).value].compact.reject(&:empty?).sort
  state = j2o_decide.call(guard, before, wanted, j2o_untouched.call(wp, attr))
  status = {unchanged: 'unchanged', changed: 'target_changed'}.fetch(state, 'updated')
  if status == 'updated'
    cv = wp.custom_values.find_or_initialize_by(custom_field_id: field.id)
    cv.value = value
    cv.save!
  end
  # Read under the row lock, so a concurrent edit cannot become J2O's record.
  digest = j2o_digest.call(j2o_stored.call(wp, attr)) unless status == 'target_changed'
end
row = {id: wp.id, attr: attr, status: status}
row[:digest] = digest if digest
puts $j2o_start_marker
puts JSON.generate({updated: status == 'updated' ? 1 : 0, results: [row]})
puts $j2o_end_marker
"""
)


@register_entity_types("customfields_generic")
class CustomFieldsGenericMigration(LevelFieldsMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    @staticmethod
    def _to_string_value(value: Any) -> str:
        """Normalize Jira CF value to a string for OP CF storage."""
        if value is None:
            return ""
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()[:10]
        if isinstance(value, list):
            # Join list items by comma using their display/name/value when present
            normalized: list[str] = []
            for v in value:
                if isinstance(v, dict):
                    normalized.append(
                        str(
                            v.get("name") or v.get("value") or v.get("displayName") or v.get("id") or v,
                        ),
                    )
                else:
                    normalized.append(str(getattr(v, "name", v)))
            return ", ".join(normalized)
        if isinstance(value, dict):
            return str(value.get("name") or value.get("value") or value.get("displayName") or value)
        # Fallback for objects with .name
        return str(getattr(value, "name", value))

    def _extract(self) -> ComponentResult:
        """Extract unmapped customfield_* values per issue mapped to a WP."""
        wp_map = self._work_package_map()
        keys = self._jira_keys_from_wp_map(wp_map)
        issues = self._merge_batch_issues(keys)

        # Use existing CF mapping to decide names/types
        cf_mapping = self.mappings.get_mapping("custom_field") or {}

        # Production wp_map is keyed by the numeric Jira ID with the human key
        # nested under ``jira_key``; ``issues`` is keyed by the human key. Index
        # entries by their human key so the lookup below works for both the
        # production (numeric-ID) and legacy/test (human-key) mapping shapes
        # instead of silently skipping every issue (#260).
        entry_by_jira_key = self._index_by_jira_key(wp_map)

        values_by_wp: dict[int, list[tuple[str, str, str]]] = {}
        wp_to_project: dict[int, int] = {}  # Track project ID for each WP
        for jira_key, issue in issues.items():
            # Walk raw ``fields`` for dynamic ``customfield_*`` attributes —
            # the per-tenant set of CFs is not modelled by JiraIssueFields,
            # so attribute access on the underlying object is correct here.
            fields = getattr(issue, "fields", None)
            if not fields:
                continue
            raw_entry = entry_by_jira_key.get(jira_key)
            if raw_entry is None:
                continue
            try:
                entry = WorkPackageMappingEntry.from_legacy(jira_key, raw_entry)
            except ValueError:
                # Corrupt or unsupported wp_map shape — skip silently to
                # preserve the pre-typed call-site behaviour.
                continue
            wp_id = int(entry.openproject_id)
            # Track project ID for selective enablement
            if entry.openproject_project_id is not None:
                wp_to_project[wp_id] = int(entry.openproject_project_id)

            specs = self._unmapped_values(fields, cf_mapping)
            if specs:
                values_by_wp[wp_id] = specs

        return ComponentResult(success=True, data={"values_by_wp": values_by_wp, "wp_to_project": wp_to_project})

    def _unmapped_values(self, fields, cf_mapping):
        values = []
        for attr in dir(fields):
            if not attr.startswith("customfield_"):
                continue
            value = getattr(fields, attr, None)
            mapping = cf_mapping.get(attr) or {}
            if isinstance(mapping, int) or mapping.get("openproject_id"):
                continue  # The content pass writes this field in its native format.
            if is_sprint_field(mapping):
                continue  # The sprint and epic component owns sprint data (ADR-017).
            if value in (None, "", [], {}):
                continue
            normalized = self._to_string_value(value)
            if normalized:
                name = mapping.get("openproject_name") or mapping.get("jira_name") or attr
                values.append((name, mapping.get("openproject_type", "text"), normalized))
        return values

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        values_by_wp: dict[int, list[tuple[str, str, str]]] = (mapped.data or {}).get("values_by_wp", {})  # type: ignore[assignment]
        wp_to_project: dict[int, int] = (mapped.data or {}).get("wp_to_project", {})  # type: ignore[assignment]
        if not values_by_wp:
            return ComponentResult(success=True, updated=0)

        updated = 0
        failed = 0
        # Track projects per CF for selective enablement
        cf_to_projects: dict[int, set[int]] = {}

        # Ensure CFs exist and apply values per WP
        for wp_id, cf_specs in values_by_wp.items():
            project_id = wp_to_project.get(wp_id)
            # Deduplicate CF names for this WP
            applied, rejected = self._apply_work_package_fields(cf_specs, project_id, cf_to_projects, wp_id)
            updated += applied
            failed += rejected

        # Enable CFs only for projects that have values
        for cf_id, project_ids in cf_to_projects.items():
            self._enable_cf_for_projects(cf_id, project_ids)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def _apply_work_package_fields(self, cf_specs, project_id, cf_to_projects, wp_id):
        updated = failed = 0
        # Deduplicate CF names for this WP
        seen: set[str] = set()
        for name, field_format, value in cf_specs:
            if name in seen:
                continue
            seen.add(name)
            try:
                cf_id = self._ensure_wp_custom_field(name, field_format or "text")
                # Track project for selective enablement
                if project_id:
                    cf_to_projects.setdefault(cf_id, set()).add(project_id)
                self._set_value(wp_id, cf_id, value)
                updated += 1
            except Exception:
                logger.exception("Failed to apply CF '%s' for WP %s", name, wp_id)
                failed += 1
        return updated, failed

    def _set_value(self, work_package_id, field_id, value):
        store = written_values.store()
        row = {"id": int(work_package_id), f"customField{int(field_id)}": value}
        guarded = written_values.guarded([row], store, overwrite=written_values.overwrite_allowed())
        payload = {"wp_id": work_package_id, "field_id": field_id, "value": value, "guard": guarded[0]["_j2o_guard"]}
        result = self.op_client.execute_script_with_data(_WRITE_VALUE, payload)
        answer = result.get("data") if isinstance(result, dict) else None
        if not isinstance(answer, dict):
            msg = "OpenProject did not confirm the custom field value"
            raise ValueError(msg)
        outcome = written_values.settle(store, guarded, answer)
        if outcome.acknowledged != 1:
            msg = "OpenProject did not confirm the custom field value"
            raise ValueError(msg)
        if outcome.changed:
            logger.warning("Kept field value changed in OpenProject since J2O wrote it: %s", outcome.changed[0])

    def run(self) -> ComponentResult:
        """Run Custom fields (generic) migration, also for the level work packages (ADR-009)."""
        return self._with_level_fields(lambda: self._run_etl_pipeline("Custom fields (generic)"))
