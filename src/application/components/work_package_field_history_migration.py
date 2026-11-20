# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Keep each migrated issue's audience-safe Jira field changes as structured history.

Native OpenProject journals cannot keep each change's reader audience (see
ADR-001), so the selected changes are stored as canonical JSON in one text
custom field per work package. The selection is shared with ``j2o-history``
(:mod:`src.infrastructure.jira.field_history`). Only issues that Jira returns
for ``level IS EMPTY`` in software and business projects are written; the
OpenProject target must carry the issue's ``J2O Origin ID`` and sit in the
mapped project. A value is written only when it differs from the stored one.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from itertools import batched
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.issue_security import permits_unrestricted_result, unrestricted_project_jql
from src.application.components.project_named_records import project_id
from src.infrastructure.jira.field_history import custom_fields, is_complete_page, kept_events
from src.infrastructure.jira.issue_pagination import iter_issue_pages
from src.infrastructure.jira.jira_issue_service import fetch_issue_page_with_retry
from src.models import ComponentResult, WorkPackageMappingEntry

FIELD_NAME = "Jira Field Changes (structured history)"
_BATCH = 100
_PROJECT_TYPES = {"software", "business"}

_READ = """
require 'json'
origin = WorkPackageCustomField.find_by(name: 'J2O Origin ID')
ids = input_data.fetch('ids')
fields = [input_data.fetch('field'), origin&.id].compact
rows = CustomValue.where(customized_type: 'WorkPackage', customized_id: ids, custom_field_id: fields)
  .pluck(:customized_id, :custom_field_id, :value)
projects = WorkPackage.where(id: ids).pluck(:id, :project_id)
puts $j2o_start_marker
puts JSON.generate({'origin' => origin&.id, 'projects' => projects, 'rows' => rows})
puts $j2o_end_marker
"""

_WRITE = """
require 'json'
cf = WorkPackageCustomField.find(input_data.fetch('field'))
raise 'Wrong field history field' unless cf.name == input_data.fetch('name') && cf.field_format == 'text'
origin = WorkPackageCustomField.where(name: 'J2O Origin ID').sole
updated = 0
input_data.fetch('rows').each do |row|
  wp = WorkPackage.find(row.fetch('wp'))
  wp.with_lock do
    raise 'Wrong Jira issue origin' unless wp.custom_values.where(custom_field: origin).sole.value == row.fetch('id')
    raise 'Wrong target project' unless wp.project_id == row.fetch('project')
    owner = wp.respond_to?(:type_variant) ? wp.type_variant.effective_source_for(TypeVariant::FORM_CONFIGURATION) : wp.type
    owner.custom_fields << cf unless owner.custom_fields.exists?(cf.id)
    CustomFieldsProject.find_or_create_by!(custom_field: cf, project_id: wp.project_id)
    cv = wp.custom_values.find_or_initialize_by(custom_field: cf)
    next if cv.persisted? && cv.value == row.fetch('value')
    cv.update!(value: row.fetch('value'))
    raise 'Field history readback failed' unless cv.reload.value == row.fetch('value')
    updated += 1
  end
end
puts $j2o_start_marker
puts JSON.generate({'updated' => updated})
puts $j2o_end_marker
"""

_COUNTS = (
    "issues_seen",
    "issues_written",
    "unchanged",
    "no_kept_changes",
    "incomplete",
    "restricted_skipped",
    "not_returned",
    "unsupported_project_type",
    "target_unconfirmed",
    "failed",
    "source_changes",
    "kept_changes",
    "withheld_changes",
)


def approved_field_ids(value: Any) -> set[str]:
    """Read reviewed standard custom-field IDs from a list or a comma-separated string."""
    items = value.split(",") if isinstance(value, str) else value or []
    approved = {str(item).strip() for item in items if str(item).strip()}
    if any(not re.fullmatch(r"customfield_\d+", fid, re.ASCII) for fid in approved):
        msg = "history_approved_field_ids accepts only Jira customfield_<number> IDs"
        raise ValueError(msg)
    return approved


def canonical_history(issue_id: str, events: list[dict]) -> str:
    """Encode the kept events in the stable form compared on every run."""
    document = {"jiraIssueId": issue_id, "events": events}
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@register_entity_types("work_package_field_history")
class WorkPackageFieldHistoryMigration(BaseMigration):
    """Write audience-safe Jira field-change history to confirmed work packages."""

    def _source_issues(self, key: str) -> Iterator:
        jql = f"{unrestricted_project_jql(key)} ORDER BY id"

        def fetch(offset: int, size: int) -> list:
            return fetch_issue_page_with_retry(
                self.jira_client,
                jql=jql,
                start_at=offset,
                max_results=size,
                fields="project,security",
                expand="changelog",
                project_key=key,
            )

        return iter_issue_pages(key, fetch, _BATCH)

    def _mapped_targets(self, wanted: set[str]) -> dict[str, dict[str, tuple[int, str | None]]]:
        projects: dict[str, dict[str, tuple[int, str | None]]] = {}
        for origin, value in (self.mappings.get_mapping("work_package") or {}).items():
            key = self._inner_jira_key(str(origin), value)
            if not key or (wanted and self._issue_project_key(key) not in wanted):
                continue
            row = WorkPackageMappingEntry.from_legacy(key, value)
            sid = str(origin) if str(origin).isdecimal() else None
            projects.setdefault(self._issue_project_key(key), {})[key] = (int(row.openproject_id), sid)
        return projects

    def _extract(self) -> ComponentResult:
        approved = approved_field_ids(config.migration_config.get("history_approved_field_ids"))
        fields = custom_fields(self.jira_client.get_custom_fields(), approved)
        selected = config.jira_config.get("projects") or []
        wanted = {k.strip() for k in (selected.split(",") if isinstance(selected, str) else selected)}
        mapped = self._mapped_targets(wanted)
        counts: Counter[str] = Counter()
        projects = {}
        for key, mapping in (self.mappings.get_mapping("project") or {}).items():
            pid = project_id(mapping)
            targets = mapped.get(key)
            if (wanted and key not in wanted) or not pid or not targets:
                continue
            rows, seen = self._project_rows(key, targets, fields, counts)
            counts["not_returned"] += len(targets) - seen
            if rows:
                projects[key] = {"project_id": pid, "rows": rows}
        return ComponentResult(success=True, data={"projects": projects}, details=dict(counts))

    def _project_rows(self, key: str, targets: dict, fields: set, counts: Counter[str]) -> tuple[list[dict], int]:
        rows = []
        seen = set()
        for issue in self._source_issues(key):
            if issue.key not in targets or issue.key in seen:
                continue
            seen.add(issue.key)
            counts["issues_seen"] += 1
            raw = issue.raw
            wp_id, expected = targets[issue.key]
            if not permits_unrestricted_result(issue):
                counts["restricted_skipped"] += 1
                continue
            if raw["fields"]["project"].get("projectTypeKey") not in _PROJECT_TYPES:
                counts["unsupported_project_type"] += 1  # Customer-facing audiences need a separate access check.
                continue
            if not str(issue.id).isdecimal() or (expected is not None and str(issue.id) != expected):
                counts["target_unconfirmed"] += 1
                continue
            page = raw.get("changelog")
            try:
                complete = isinstance(page, dict) and is_complete_page(page)
            except KeyError, TypeError:
                complete = False
            if not complete:
                counts["incomplete"] += 1
                continue
            events = kept_events(page["histories"], fields)
            source = sum(len(h["items"]) for h in page["histories"])
            kept = sum(len(event["changes"]) for event in events)
            counts.update(source_changes=source, kept_changes=kept, withheld_changes=source - kept)
            if not events:
                counts["no_kept_changes"] += 1
                continue
            rows.append({"wp": wp_id, "id": str(issue.id), "value": canonical_history(str(issue.id), events)})
        return rows, len(seen)

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        counts: Counter[str] = Counter(dict.fromkeys(_COUNTS, 0))
        counts.update(mapped.details or {})
        projects = (mapped.data or {}).get("projects", {})
        if projects:
            field = self.op_client.ensure_wp_custom_field_id(FIELD_NAME, "text")
            if not field:
                msg = f"OpenProject did not return the '{FIELD_NAME}' custom field"
                raise ValueError(msg)
            for key, project in projects.items():
                for batch in batched(project["rows"], _BATCH, strict=False):
                    try:
                        self._write_batch(field, project["project_id"], list(batch), counts)
                    except Exception:
                        self.logger.exception("Could not write field history for %s", key)
                        counts["failed"] += len(batch)
        warnings = [
            f"{counts[name]} mapped work packages skipped: {name}"
            for name in (
                "incomplete",
                "restricted_skipped",
                "not_returned",
                "unsupported_project_type",
                "target_unconfirmed",
            )
            if counts[name]
        ]
        for warning in warnings:
            self.logger.warning(warning)
        return ComponentResult(
            success=not counts["failed"],
            updated=counts["issues_written"],
            failed=counts["failed"],
            details=dict(counts),
            warnings=warnings,
        )

    def _write_batch(self, field: int, pid: int, rows: list[dict], counts: Counter[str]) -> None:
        data = self._script(_READ, {"field": field, "ids": [row["wp"] for row in rows]})
        stored = {}
        origins: dict[int, set[str]] = {}
        for wp, cf_id, value in data["rows"]:
            if cf_id == field:
                stored[wp] = value
            elif cf_id == data["origin"]:
                origins.setdefault(wp, set()).add(value)
        in_project = {wp for wp, project in data["projects"] if project == pid}
        local: Counter[str] = Counter()  # Merged only after the batch succeeded; a failure counts each row once.
        changed = []
        for row in rows:
            if origins.get(row["wp"]) != {row["id"]} or row["wp"] not in in_project:
                local["target_unconfirmed"] += 1
            elif stored.get(row["wp"]) == row["value"]:
                local["unchanged"] += 1
            else:
                changed.append({**row, "project": pid})
        if changed:
            updated = self._script(_WRITE, {"field": field, "name": FIELD_NAME, "rows": changed}).get("updated")
            if type(updated) is not int or not 0 <= updated <= len(changed):
                msg = "OpenProject returned an invalid field history write count"
                raise ValueError(msg)
            local.update(issues_written=updated, unchanged=len(changed) - updated)
        counts.update(local)

    def _script(self, script: str, payload: dict) -> dict:
        envelope = self.op_client.execute_script_with_data(script, payload)
        data = envelope.get("data") if isinstance(envelope, dict) and envelope.get("status") == "success" else None
        if not isinstance(data, dict):
            msg = "OpenProject did not confirm the field history script"
            raise TypeError(msg)
        return data

    def run(self) -> ComponentResult:
        """Run the field history pass."""
        return self._run_etl_pipeline("Work package field history")

    def run_with_change_detection(self, entity_type: str | None = None) -> ComponentResult:
        """Recheck source visibility and stored values on every pass."""
        return self.run()
