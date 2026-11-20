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
cleared = 0
input_data.fetch('rows').each do |row|
  wp = WorkPackage.find(row.fetch('wp'))
  wp.with_lock do
    raise 'Wrong Jira issue origin' unless wp.custom_values.where(custom_field: origin).sole.value == row.fetch('id')
    raise 'Wrong target project' unless wp.project_id == row.fetch('project')
    owner = wp.respond_to?(:type_variant) ? wp.type_variant.effective_source_for(TypeVariant::FORM_CONFIGURATION) : wp.type
    owner.custom_fields << cf unless owner.custom_fields.exists?(cf.id)
    CustomFieldsProject.find_or_create_by!(custom_field: cf, project_id: wp.project_id)
    if row.fetch('value').nil?
      wp.custom_values.where(custom_field: cf).destroy_all
      copies = Journal::CustomizableJournal.where(journal_id: wp.journals.select(:id), custom_field_id: cf.id)
      copies.delete_all
      raise 'Field history removal failed' if wp.custom_values.where(custom_field: cf).exists? || copies.exists?
      cleared += 1
      next
    end
    cv = wp.custom_values.find_or_initialize_by(custom_field: cf)
    next if cv.persisted? && cv.value == row.fetch('value')
    cv.update!(value: row.fetch('value'))
    raise 'Field history readback failed' unless cv.reload.value == row.fetch('value')
    updated += 1
  end
end
puts $j2o_start_marker
puts JSON.generate({'updated' => updated, 'cleared' => cleared})
puts $j2o_end_marker
"""

_STORED = """
require 'json'
cf = WorkPackageCustomField.find_by(name: input_data.fetch('name'))
ids = []
if cf
  in_project = WorkPackage.where(project_id: input_data.fetch('project')).select(:id)
  ids = CustomValue.where(customized_type: 'WorkPackage', custom_field_id: cf.id, customized_id: in_project)
    .where.not(value: [nil, '']).distinct.pluck(:customized_id)
end
origin = WorkPackageCustomField.find_by(name: 'J2O Origin ID')&.id
key = WorkPackageCustomField.find_by(name: 'J2O Origin Key')&.id
values = CustomValue.where(customized_type: 'WorkPackage', customized_id: ids, custom_field_id: [origin, key].compact)
  .pluck(:customized_id, :custom_field_id, :value)
puts $j2o_start_marker
puts JSON.generate({'ids' => ids, 'origin' => origin, 'key' => key, 'values' => values})
puts $j2o_end_marker
"""

_COUNTS = (
    "issues_seen",
    "issues_written",
    "cleared",
    "unchanged",
    "no_kept_changes",
    "incomplete",
    "restricted_skipped",
    "not_returned",
    "unsupported_project_type",
    "target_unconfirmed",
    "failed",
    "clear_unconfirmed",
    "source_changes",
    "kept_changes",
    "withheld_changes",
)
_SKIPS = ("incomplete", "restricted_skipped", "not_returned", "unsupported_project_type", "target_unconfirmed")


def approved_field_ids(value: Any) -> set[str]:
    """Read reviewed standard custom-field IDs from a list or a comma-separated string."""
    items = value.split(",") if isinstance(value, str) else value or []
    approved = {str(item).strip() for item in items if str(item).strip()}
    if any(not re.fullmatch(r"customfield_\d+", fid, re.ASCII) for fid in approved):
        msg = "history_approved_field_ids accepts only Jira customfield_<number> IDs"
        raise ValueError(msg)
    return approved


def _complete_page(page: object) -> bool:
    try:
        return isinstance(page, dict) and is_complete_page(page)
    except KeyError, TypeError:
        return False


def _row_outcome(
    row: dict, stored: dict[int, Any], origins: dict[int, set[str]], in_project: set
) -> tuple[str | None, dict]:
    """Name the counter for a row that needs no write, or return ``None`` with the row to write."""
    if row["id"] is None and len(origins.get(row["wp"], ())) == 1:
        row = {**row, "id": next(iter(origins[row["wp"]]))}
    if origins.get(row["wp"]) != {row["id"]} or row["wp"] not in in_project:
        return ("clear_unconfirmed" if row["value"] is None else "target_unconfirmed"), row
    if stored.get(row["wp"]) == row["value"]:
        return "unchanged", row
    return None, row


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

    def _mapped_targets(self) -> dict[str, dict[str, tuple[int, str | None]]]:
        projects: dict[str, dict[str, tuple[int, str | None]]] = {}
        for origin, value in (self.mappings.get_mapping("work_package") or {}).items():
            key = self._inner_jira_key(str(origin), value)
            if not key:
                continue
            row = WorkPackageMappingEntry.from_legacy(key, value)
            sid = str(origin) if str(origin).isdecimal() else None
            projects.setdefault(self._issue_project_key(key), {})[key] = (int(row.openproject_id), sid)
        return projects

    def _extract(self) -> ComponentResult:
        approved = approved_field_ids(config.migration_config.get("history_approved_field_ids"))
        fields = custom_fields(self.jira_client.get_custom_fields(), approved)
        wanted = set(config.selected_jira_projects())
        mapped = self._mapped_targets()
        # Every mapped work package, selected or not, so recovery never claims one another project maps.
        mapped_wps = {wp for targets in mapped.values() for wp, _ in targets.values()}
        counts: Counter[str] = Counter()
        projects = {}
        for key, mapping in (self.mappings.get_mapping("project") or {}).items():
            pid = project_id(mapping)
            if (wanted and key not in wanted) or not pid:
                continue
            rows = self._project_plan(
                key, pid, dict(mapped.get(key, {})), mapped_wps=mapped_wps, fields=fields, counts=counts
            )
            if rows:
                projects[key] = {"project_id": pid, "rows": rows}
        return ComponentResult(success=True, data={"projects": projects}, details=dict(counts))

    def _project_plan(
        self, key: str, pid: int, targets: dict, *, mapped_wps: set[int], fields: set, counts: Counter[str]
    ) -> list[dict]:
        unkeyed = self._recover_targets(pid, targets, mapped_wps)
        if not targets:
            return unkeyed
        rows, seen = self._project_rows(key, targets, fields, counts)
        counts["not_returned"] += len(targets) - seen
        return rows + unkeyed

    def _recover_targets(self, pid: int, targets: dict, mapped_wps: set[int]) -> list[dict]:
        """Add stored values the work-package mapping misses to the targets, by their provenance.

        The mapping is a cache, so a value stored in the project stays checked when its entry is gone.
        A stored value without a usable J2O Origin Key, or for an issue another work package already
        targets, gets a clear row instead.
        """
        data = self._script(_STORED, {"name": FIELD_NAME, "project": pid})
        provenance: dict[int, dict[str, str]] = {}
        for wp, cf_id, value in data["values"]:
            name = {data["origin"]: "id", data["key"]: "key"}.get(cf_id)
            if name:
                provenance.setdefault(wp, {})[name] = str(value)
        clears = []
        for wp in data["ids"]:
            if wp in mapped_wps:
                continue
            key, sid = provenance.get(wp, {}).get("key"), provenance.get(wp, {}).get("id")
            if key and key not in targets and sid and sid.isdecimal():
                targets[key] = (wp, sid)
            else:
                clears.append(self._clear_row(wp, sid))
        return clears

    def _project_rows(self, key: str, targets: dict, fields: set, counts: Counter[str]) -> tuple[list[dict], int]:
        rows = []
        seen = set()
        for issue in self._source_issues(key):
            if issue.key not in targets or issue.key in seen:
                continue
            seen.add(issue.key)
            counts["issues_seen"] += 1
            wp_id, expected = targets[issue.key]
            row = self._issue_row(issue, wp_id, expected, fields, counts)
            if row is not None:
                rows.append(row)
        # A mapped issue the unrestricted search no longer returns may have gained a security level: clear its value.
        rows.extend(self._clear_row(wp_id, sid) for key, (wp_id, sid) in targets.items() if key not in seen)
        return rows, len(seen)

    def _issue_row(
        self, issue: Any, wp_id: int, expected: str | None, fields: set, counts: Counter[str]
    ) -> dict | None:
        """Return the issue's write or clear row, or ``None`` when the issue is skipped and its value kept."""
        raw = issue.raw
        if not permits_unrestricted_result(issue):
            counts["restricted_skipped"] += 1
            return self._clear_row(wp_id, issue.id)
        if raw["fields"]["project"].get("projectTypeKey") not in _PROJECT_TYPES:
            counts["unsupported_project_type"] += 1  # Customer-facing audiences need a separate access check.
            return None
        if not str(issue.id).isdecimal() or (expected is not None and str(issue.id) != expected):
            counts["target_unconfirmed"] += 1
            return None
        page = raw.get("changelog")
        if not _complete_page(page):
            counts["incomplete"] += 1
            return None
        events = kept_events(page["histories"], fields, project_visible=True)
        source = sum(len(h["items"]) for h in page["histories"])
        kept = sum(len(event["changes"]) for event in events)
        counts.update(source_changes=source, kept_changes=kept, withheld_changes=source - kept)
        if not events:
            counts["no_kept_changes"] += 1
            return self._clear_row(wp_id, issue.id)
        return {"wp": wp_id, "id": str(issue.id), "value": canonical_history(str(issue.id), events)}

    @staticmethod
    def _clear_row(wp_id: int, issue_id: object) -> dict:
        """Request removal of a stored value whose issue is no longer audience-safe.

        Without a known Jira issue ID the target's own stored J2O Origin ID confirms it at write time.
        """
        return {"wp": wp_id, "id": None if issue_id is None else str(issue_id), "value": None}

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        counts: Counter[str] = Counter(dict.fromkeys(_COUNTS, 0))
        counts.update(mapped.details or {})
        projects = (mapped.data or {}).get("projects", {})
        if projects:
            self._write_projects(projects, counts)
        warnings = [f"{counts[name]} mapped work packages skipped: {name}" for name in _SKIPS if counts[name]]
        for warning in warnings:
            self.logger.warning(warning)
        return ComponentResult(
            success=not (counts["failed"] or counts["clear_unconfirmed"]),
            updated=counts["issues_written"],
            failed=counts["failed"],
            details=dict(counts),
            warnings=warnings,
        )

    def _write_projects(self, projects: dict, counts: Counter[str]) -> None:
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
                    clears = sum(row["value"] is None for row in batch)
                    counts["failed"] += len(batch) - clears
                    counts["clear_unconfirmed"] += clears

    def _write_batch(self, field: int, pid: int, rows: list[dict], counts: Counter[str]) -> None:
        stored, origins, in_project = self._read_targets(field, pid, rows)
        local: Counter[str] = Counter()  # Merged only after the batch succeeded; a failure counts each row once.
        changed = []
        for row in rows:
            if row["value"] is None and row["wp"] not in stored:
                continue  # nothing stored, nothing to clear
            outcome, target = _row_outcome(row, stored, origins, in_project)
            if outcome:
                local[outcome] += 1
            else:
                changed.append({**target, "project": pid})
        if changed:
            local.update(self._write_rows(field, changed))
        counts.update(local)

    def _read_targets(self, field: int, pid: int, rows: list[dict]) -> tuple[dict[int, Any], dict[int, set[str]], set]:
        data = self._script(_READ, {"field": field, "ids": [row["wp"] for row in rows]})
        stored: dict[int, Any] = {}
        origins: dict[int, set[str]] = {}
        for wp, cf_id, value in data["rows"]:
            if cf_id == field:
                stored[wp] = value
            elif cf_id == data["origin"]:
                origins.setdefault(wp, set()).add(value)
        in_project = {wp for wp, project in data["projects"] if project == pid}
        return stored, origins, in_project

    def _write_rows(self, field: int, changed: list[dict]) -> dict[str, int]:
        result = self._script(_WRITE, {"field": field, "name": FIELD_NAME, "rows": changed})
        updated, cleared = result.get("updated"), result.get("cleared")
        clears = sum(row["value"] is None for row in changed)
        if (
            type(updated) is not int
            or type(cleared) is not int
            or not 0 <= updated <= len(changed) - clears
            or cleared != clears
        ):
            msg = "OpenProject returned an invalid field history write count"
            raise ValueError(msg)
        return {"issues_written": updated, "cleared": cleared, "unchanged": len(changed) - clears - updated}

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
