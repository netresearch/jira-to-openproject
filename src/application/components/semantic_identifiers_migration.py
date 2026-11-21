# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Write Jira keys as native OpenProject semantic work package identifiers (ADR-011).

Off unless ``migration.semantic_identifiers`` is the boolean ``true``. Per migrated work
package the identifier becomes the Jira key and the sequence number the key's number, as the
official OpenProject Jira importer writes them; every earlier key from the issue's ``Key``
changelog (a moved issue) becomes a semantic alias. The Jira key is added as a project slug, so
that the switch to semantic identifiers restores it as the project identifier and keeps the work
package identifiers. Each project's sequence counter is raised above every number its key prefix
already names. A work package with another identifier, and a key, alias or sequence number that
another work package owns, is refused, never overwritten. Nothing goes into a mapping file.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from itertools import batched
from typing import Any

from src import config
from src.application.components import level_issues
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.issue_security import unrestricted_project_jql
from src.application.components.project_named_records import project_id
from src.application.components.security_level_projects_migration import LevelProjectError
from src.infrastructure.jira.issue_pagination import iter_issue_pages
from src.infrastructure.jira.jira_issue_service import fetch_issue_page_with_retry
from src.models import ComponentResult, WorkPackageMappingEntry

SWITCH = "semantic_identifiers"
_BATCH = 100
# Projects::Identifier::SEMANTIC_FORMAT with SEMANTIC_IDENTIFIER_MAX_LENGTH (OpenProject 17.9.7).
_PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,9}", re.ASCII)
_ISSUE_KEY = re.compile(r"([A-Z][A-Z0-9_]*)-([1-9]\d*)", re.ASCII)

_PROJECT = """
require 'json'
project = Project.find_by(id: input_data.fetch('project'))
key = input_data.fetch('key')
reason = nil
if project.nil?
  reason = 'project missing'
else
  slugs = FriendlyId::Slug.where(sluggable_type: 'Project').where('LOWER(slug) = ?', key.downcase)
  taken = slugs.where.not(sluggable_id: project.id).exists? ||
          Project.where('LOWER(identifier) = ?', key.downcase).where.not(id: project.id).exists? ||
          Projects::Identifier::RESERVED_IDENTIFIERS.map(&:upcase).include?(key)
  semantic = ProjectIdentifiers::IdentifierAutofix::ProblematicIdentifiers.valid_format?(project.identifier)
  if taken
    reason = 'project key taken by another project'
  elsif semantic && project.identifier != key
    reason = 'project has another semantic identifier'
  elsif !semantic
    FriendlyId::Slug.find_or_create_by!(sluggable_type: 'Project', sluggable_id: project.id, slug: key, scope: nil)
    reason = 'project key is not the identifier the switch restores' unless project.reload.previous_semantic_identifier == key
  end
end
puts $j2o_start_marker
puts JSON.generate({'refused' => reason})
puts $j2o_end_marker
"""

_WRITE = """
require 'json'
pid = input_data.fetch('project')
origin = WorkPackageCustomField.where(name: 'J2O Origin ID').sole
result = {'written' => 0, 'unchanged' => 0, 'refused' => []}
refuse = lambda do |wp, row, names|
  next 'work package missing' if wp.nil?
  next 'work package in another project' unless wp.project_id == pid
  next 'work package of another Jira issue' unless wp.custom_values.where(custom_field: origin).pluck(:value) == [row.fetch('id')]
  next 'work package has another identifier' if wp.identifier.present? && wp.identifier != row.fetch('key')
  next 'work package has another sequence number' if wp.sequence_number.present? && wp.sequence_number != row.fetch('seq')
  next 'identifier or alias owned by another work package' if WorkPackage.where(identifier: names).where.not(id: wp.id).exists? ||
    WorkPackageSemanticAlias.where(identifier: names).where.not(work_package_id: wp.id).exists?
  next 'sequence number owned by another work package' if WorkPackage.where(project_id: pid, sequence_number: row.fetch('seq')).where.not(id: wp.id).exists?
  nil
end
input_data.fetch('rows').each do |row|
  WorkPackage.transaction do
    wp = WorkPackage.lock.find_by(id: row.fetch('wp'))
    names = [row.fetch('key'), *row.fetch('aliases')]
    names = (names + wp.alias_rows_for_sequence_number(row.fetch('seq')).map { |r| r[:identifier] }).uniq if wp
    reason = refuse.call(wp, row, names)
    if reason
      result['refused'] << {'key' => row.fetch('key'), 'reason' => reason}
      next
    end
    missing = names - wp.semantic_aliases.pluck(:identifier)
    if wp.identifier == row.fetch('key') && missing.empty?
      result['unchanged'] += 1
      next
    end
    wp.update_columns(identifier: row.fetch('key'), sequence_number: row.fetch('seq'))
    WorkPackageSemanticAlias.insert_all(missing.map { |n| {identifier: n, work_package_id: wp.id} }, unique_by: :identifier) if missing.any?
    raise 'Semantic identifier readback failed' unless wp.reload.identifier == row.fetch('key') &&
      WorkPackageSemanticAlias.where(work_package_id: wp.id, identifier: names).count == names.size
    result['written'] += 1
  end
end
puts $j2o_start_marker
puts JSON.generate(result)
puts $j2o_end_marker
"""

# Never lowered. The parent's target covers every identifier and alias with its key prefix, so the
# keys of its level sub-projects and of issues moved out of it are never allocated again.
_COUNTER = """
require 'json'
key = input_data.fetch('key')
pattern = WorkPackage::SemanticIdentifier.slug_prefix_pattern(key)
number = Arel.sql("CAST(substring(identifier from '[0-9]+$') AS integer)")
raise_to = lambda do |id, target|
  Project.where(id: id).where('wp_sequence_counter < ?', target).update_all(wp_sequence_counter: target)
  Project.where(id: id).pick(:wp_sequence_counter)
end
top = [
  WorkPackage.where('identifier ~ ?', pattern).maximum(number),
  WorkPackageSemanticAlias.where('identifier ~ ?', pattern).maximum(number),
  WorkPackage.where(project_id: input_data.fetch('project')).maximum(:sequence_number),
].compact.max || 0
counters = {input_data.fetch('project').to_s => raise_to.call(input_data.fetch('project'), top)}
input_data.fetch('sub_projects').each do |sub|
  counters[sub.to_s] = raise_to.call(sub, WorkPackage.where(project_id: sub).maximum(:sequence_number) || 0)
end
puts $j2o_start_marker
puts JSON.generate({'counters' => counters})
puts $j2o_end_marker
"""


def enabled() -> bool:
    """The component writes only when the switch is the boolean ``true``."""
    return config.migration_config.get(SWITCH) is True


def key_aliases(changelog: object) -> list[str]:
    """Every key the issue had or got, from the ``Key`` items of its changelog."""
    histories = changelog.get("histories") if isinstance(changelog, Mapping) else None
    keys: set[str] = set()
    for history in histories if isinstance(histories, list) else []:
        items = history.get("items") if isinstance(history, Mapping) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, Mapping) and item.get("field") == "Key":
                keys.update(value for value in (item.get("fromString"), item.get("toString")) if isinstance(value, str))
    return sorted(key for key in keys if _ISSUE_KEY.fullmatch(key))


def issue_row(issue: Any, wp_id: int, project_key: str) -> dict[str, Any] | None:
    """The payload row of one issue, or None when its key is no semantic key of the project."""
    match = _ISSUE_KEY.fullmatch(str(issue.key))
    if match is None or match.group(1) != project_key or not str(issue.id).isdecimal():
        return None
    raw = issue.raw if isinstance(getattr(issue, "raw", None), Mapping) else {}
    aliases = [key for key in key_aliases(raw.get("changelog")) if key != issue.key]
    return {"wp": wp_id, "id": str(issue.id), "key": issue.key, "seq": int(match.group(2)), "aliases": aliases}


def _batch_outcome(result: dict, size: int) -> tuple[int, int, list[dict]] | None:
    """Written, unchanged and refused rows of one batch; None unless they account for every row."""
    written, unchanged, refused = result.get("written"), result.get("unchanged"), result.get("refused")
    if type(written) is not int or type(unchanged) is not int or not isinstance(refused, list):
        return None
    if not all(isinstance(row, dict) for row in refused) or min(written, unchanged) < 0:
        return None
    return (written, unchanged, refused) if written + unchanged + len(refused) == size else None


@register_entity_types("semantic_identifiers")
class SemanticIdentifiersMigration(BaseMigration):
    """Give every migrated work package its Jira key as its semantic identifier."""

    def run(self) -> ComponentResult:
        """Write identifiers, aliases, project slugs and counters; with the switch off, nothing."""
        if not enabled():
            return ComponentResult(success=True, message="Semantic identifiers are off", details={"enabled": False})
        self._level: level_issues.LevelWorkPackages | None = None
        if level_issues.enabled():
            try:
                self._level = level_issues.level_work_packages(
                    self.jira_client, self.op_client, self.mappings, self.logger
                )
            except LevelProjectError as exc:
                self.logger.error("Semantic identifiers of level work packages stopped: %s", exc)
                return ComponentResult(success=False, message=str(exc), errors=["security_level"])
        self._counts: Counter[str] = Counter()
        self._refused: list[str] = []
        self._counters: dict[str, int] = {}
        targets = self._targets()
        for key, pid in self._projects():
            self._project(key, pid, targets.get(key, {}))
        return self._result()

    def run_with_change_detection(self, entity_type: str | None = None) -> ComponentResult:
        """Recheck every work package on every pass; unchanged ones are counted, not written."""
        return self.run()

    def _projects(self) -> Iterator[tuple[str, int]]:
        wanted = set(config.selected_jira_projects())
        for key, mapping in sorted((self.mappings.get_mapping("project") or {}).items()):
            pid = project_id(mapping)
            if pid and (not wanted or key in wanted):
                yield key, pid

    def _targets(self) -> dict[str, dict[str, int]]:
        """Jira project key to Jira issue ID to work package ID, from the work package mapping."""
        by_key: dict[str, tuple[str, int]] = {}
        for origin, value in (self.mappings.get_mapping("work_package") or {}).items():
            key = self._inner_jira_key(str(origin), value)
            if key and str(origin).isdecimal():
                by_key[key] = (str(origin), int(WorkPackageMappingEntry.from_legacy(key, value).openproject_id))
        if self._level is not None:
            kept = self._level.without_level_issues({key: wp for key, (_, wp) in by_key.items()})
            by_key = {key: pair for key, pair in by_key.items() if key in kept}
        targets: dict[str, dict[str, int]] = {}
        for key, (origin, wp) in by_key.items():
            targets.setdefault(self._issue_project_key(key), {})[origin] = wp
        return targets

    def _project(self, key: str, pid: int, targets: dict[str, int]) -> None:
        if not _PROJECT_KEY.fullmatch(key):
            self._refuse_project(key, "the Jira key is not a semantic project identifier")
            return
        refused = self._script(_PROJECT, {"project": pid, "key": key}).get("refused")
        if refused is not None:
            self._refuse_project(key, str(refused))
            return
        self._counts["projects"] += 1
        level_rows = self._level_rows(key)
        self._write(key, pid, self._rows(key, self._source_issues(key), targets))
        for sub, rows in sorted(level_rows.items()):
            self._write(key, sub, rows)
        counters = self._script(_COUNTER, {"project": pid, "key": key, "sub_projects": sorted(level_rows)})
        self._counters.update(counters.get("counters") or {})

    def _refuse_project(self, key: str, reason: str) -> None:
        self._counts["projects_refused"] += 1
        self._refused.append(f"{key}: {reason}")

    def _source_issues(self, key: str) -> Iterator[Any]:
        jql = f"{unrestricted_project_jql(key)} ORDER BY id"

        def fetch(offset: int, size: int) -> list:
            return fetch_issue_page_with_retry(
                self.jira_client,
                jql=jql,
                start_at=offset,
                max_results=size,
                fields="security",
                expand="changelog",
                project_key=key,
            )

        return iter_issue_pages(key, fetch, _BATCH)

    def _rows(self, key: str, issues: Iterable[Any], targets: Mapping[str, int]) -> list[dict[str, Any]]:
        """Rows of the mapped issues the search returns; the others are counted as ``not_returned``."""
        rows = []
        seen = set()
        for issue in issues:
            wp_id = targets.get(str(issue.id))
            if wp_id is None or str(issue.id) in seen:
                continue
            seen.add(str(issue.id))
            row = issue_row(issue, wp_id, key)
            if row is None:
                self._refused.append(f"{issue.key}: not a semantic key of {key}")
            else:
                rows.append(row)
        self._counts["not_returned"] += len(set(targets) - seen)
        return rows

    def _level_rows(self, key: str) -> dict[int, list[dict[str, Any]]]:
        """Rows per level sub-project of the Jira project; their prefix is the parent's key (ADR-011)."""
        level = self._level
        wps = {} if level is None else level.by_project().get(key, {})
        if level is None or not wps:
            return {}
        rows: dict[int, list[dict[str, Any]]] = {}
        seen = set()
        for issue in level_issues.iter_level_issues(self.jira_client, key, _BATCH, "security", "changelog"):
            target = level.targets.get(issue.key) if issue.key in wps and issue.key not in seen else None
            if target is None:
                continue
            seen.add(issue.key)
            row = issue_row(issue, wps[issue.key], key)
            if level_issues.issue_level_id(issue) != target.level_id or row is None:
                level.refused.append(f"{issue.key}: changed its level or key after the selection")
            else:
                rows.setdefault(target.project_id, []).append(row)
        level.refused.extend(f"{k}: not returned by the level search" for k in sorted(set(wps) - seen))
        return rows

    def _write(self, key: str, pid: int, rows: list[dict[str, Any]]) -> None:
        for batch in batched(rows, _BATCH, strict=False):
            try:
                outcome = _batch_outcome(self._script(_WRITE, {"project": pid, "rows": list(batch)}), len(batch))
            except Exception:
                self.logger.exception("Could not write semantic identifiers for %s", key)
                outcome = None
            if outcome is None:
                self.logger.error("OpenProject did not confirm the semantic identifiers of a batch of %s", key)
                self._counts["failed"] += len(batch)
                continue
            written, unchanged, refused = outcome
            self._counts.update(written=written, unchanged=unchanged)
            self._refused.extend(f"{row.get('key')}: {row.get('reason')}" for row in refused)

    def _script(self, script: str, payload: dict) -> dict:
        envelope = self.op_client.execute_script_with_data(script, payload)
        data = envelope.get("data") if isinstance(envelope, dict) and envelope.get("status") == "success" else None
        if not isinstance(data, dict):
            msg = "OpenProject did not confirm the semantic identifier script"
            raise TypeError(msg)
        return data

    def _result(self) -> ComponentResult:
        for reason in self._refused:
            self.logger.error("Semantic identifier refused: %s", reason)
        if self._counts["not_returned"]:
            # Moved, deleted or newly restricted issues: their work packages keep no Jira key (README check).
            self.logger.warning("%d mapped work packages not returned by Jira", self._counts["not_returned"])
        self._counts["refused"] = len(self._refused)
        result = ComponentResult(
            success=not (self._refused or self._counts["failed"]),
            updated=self._counts["written"],
            failed=self._counts["failed"] + len(self._refused),
            details={"enabled": True, **self._counts, "counters": self._counters},
        )
        return result if self._level is None else self._level.report(result, self.logger)
