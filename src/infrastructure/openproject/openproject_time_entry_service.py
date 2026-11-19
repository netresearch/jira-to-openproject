# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Read and write OpenProject time entries through Rails with Jira worklog provenance."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.container_staging import private_container_directory
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.utils.tempo_attribute_archive import verify_packets

_BULK_TIME_ENTRY_BODY = """\
entries = JSON.parse(File.read(data_path), symbolize_names: true)
results = []
source_mismatch = Class.new(StandardError)
field_names = {
  jira_worklog_key: 'J2O Origin Worklog Key',
  jira_started: 'J2O Source Worklog Started',
  jira_update_author_id: 'J2O Worklog Update Author ID'
}
fields_for = lambda do |row|
  field_names.each_with_object({}) do |(key, name), values|
    next if row[key].nil? || row[key].to_s.empty?
    field = TimeEntryCustomField.find_by(name: name)
    unless field
      TimeEntry.connection.select_value('SELECT pg_advisory_xact_lock(124481108, 0)')
      field = TimeEntryCustomField.find_or_create_by!(name: name) do |f|
        f.assign_attributes(field_format: 'string', is_required: false, is_for_all: false, admin_only: true)
      end
    end
    raise source_mismatch, 'Incompatible source field' unless field.field_format == 'string'
    raise source_mismatch, 'Source metadata field must be private' unless key == :jira_worklog_key || field.admin_only?
    values[field.id] = row[key].to_s
  end
end
tempo_archive_for = lambda do |row, wp, record|
  archive = row.fetch(:tempo_archive)
  raise source_mismatch, 'Missing expected project' unless row[:target_project_id] == wp.project_id
  { 'J2O Origin ID' => archive.fetch(:issue_id), 'J2O Origin URL' => archive.fetch(:issue_url) }.each do |name, expected|
    origin = WorkPackageCustomField.where(name: name).sole
    raise source_mismatch, 'Wrong Jira work package origin' unless wp.custom_values.where(custom_field_id: origin.id).sole.value == expected
  end
  title = 'Jira Tempo Work Attribute Values (age)'
  field = TimeEntryCustomField.find_by(name: title)
  unless field
    TimeEntry.connection.select_value('SELECT pg_advisory_xact_lock(124481108, 1)')
    field = TimeEntryCustomField.find_or_create_by!(name: title) do |f|
      f.assign_attributes(field_format: 'text', admin_only: true, is_for_all: false, is_required: false)
    end
  end
  field = TimeEntryCustomField.where(name: title).sole
  raise source_mismatch, 'Incompatible Tempo archive field' unless field.admin_only? && !field.is_for_all? && field.field_format == 'text'
  values = record.custom_values.where(custom_field_id: field.id)
  value = values.exists? ? values.sole : values.build
  written = value.value.nil? || value.value == ''
  value.update!(value: archive.fetch(:packet)) if written
  { tempo_packet: values.sole.value, tempo_attributes_written: written }
end
entries.each do |row|
  begin
    outcome = TimeEntry.transaction(requires_new: true) do
      wp = WorkPackage.find(row.fetch(:work_package_id))
      raise source_mismatch, 'Work package in a different project' if row[:target_project_id] && wp.project_id != row[:target_project_id]
      activity = TimeEntryActivity.find(row.fetch(:activity_id))
      raise source_mismatch, 'Activity in a different project' if activity.project_id && activity.project_id != wp.project_id
      user = User.find(row.fetch(:user_id))
      User.find(row[:jira_update_author_id]) if row[:jira_update_author_id]
      attrs = { entity: wp, project: wp.project, user: user, logged_by_id: user.id,
                hours: row.fetch(:hours).to_f, spent_on: Date.iso8601(row.fetch(:spent_on)),
                activity_id: row.fetch(:activity_id), comments: row.fetch(:comments) }
      if row[:jira_time_zone]
        attrs.merge!(time_zone: row[:jira_time_zone], start_time: row[:jira_start_minute])
      end
      attrs[:created_at] = Time.iso8601(row[:jira_created_at]) if row[:jira_created_at]
      attrs[:updated_at] = Time.iso8601(row[:jira_updated_at]) if row[:jira_updated_at]
      values = fields_for.call(row)
      record = nil
      if row[:jira_worklog_key].present?
        connection = TimeEntry.connection
        lock_sql = TimeEntry.sanitize_sql_array(["SELECT pg_advisory_xact_lock(124481109, hashtext(?))", row[:jira_worklog_key]])
        connection.select_value(lock_sql)
        field = TimeEntryCustomField.find_sole_by(name: field_names.fetch(:jira_worklog_key))
        matches = CustomValue.where(customized_type: 'TimeEntry', custom_field_id: field.id, value: row[:jira_worklog_key])
        record = TimeEntry.find(matches.sole.customized_id) if matches.exists?
      end
      existing = !record.nil?
      unless existing
        record = TimeEntry.new(attrs)
        record.custom_field_values = values if values.any?
        record.save!
      end
      record.reload
      raise source_mismatch, 'Work log differs from target attributes' unless attrs.all? { |k,v| record.public_send(k) == v }
      if row[:jira_start_minute] && record.start_timestamp.to_time.utc != Time.iso8601(row.fetch(:jira_started)).utc
        raise source_mismatch, 'Native work log start differs from the source instant'
      end
      raise source_mismatch, 'Work log differs from target provenance' unless values.all? { |id,v| record.custom_values.where(custom_field_id: id).sole.value == v }
      result = { index: row[:index], success: true, reused: existing, id: record.id }
      result[:verified_worklog_key] = row[:jira_worklog_key] if row[:jira_started]
      result.merge!(tempo_archive_for.call(row, wp, record)) if row[:tempo_archive]
      if row[:return_record]
        result.merge!(work_package_id: record.entity_id, user_id: record.user_id, activity_id: record.activity_id,
                      hours: record.hours, spent_on: record.spent_on.to_s, comments: record.comments,
                      created_at: record.created_at.to_s, updated_at: record.updated_at.to_s)
      end
      result
    end
    results << outcome
  rescue => e
    results << { index: row[:index], success: false, error: e.is_a?(source_mismatch) ? e.message : e.class.name }
  end
end
created = results.count { |r| r[:success] && !r[:reused] }
reused = results.count { |r| r[:reused] }
File.write(result_path, JSON.generate({ created: created, reused: reused, failed: results.size - created - reused, results: results }))
"""


def _linked_id(embedded: dict, name: str, resource: str) -> int | None:
    href = embedded.get(name, {}).get("href") or ""
    match = re.search(rf"/{resource}/(\d+)", href)
    return int(match.group(1)) if match else None


def _time_entry_ids(entry: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    embedded = entry.get("_embedded", {})
    return (
        _linked_id(embedded, "workPackage", "work_packages"),
        _linked_id(embedded, "user", "users"),
        _linked_id(embedded, "activity", "activities"),
    )


def _comment_text(entry: dict[str, Any]) -> str:
    comment = entry.get("comment", "")
    if isinstance(comment, dict):
        return comment.get("raw", comment.get("text", ""))
    return str(comment or "")


class OpenProjectTimeEntryService:
    """Time-entry Rails-console helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_time_entry_activities(self) -> list[dict[str, Any]]:
        """Get all available time entry activities from OpenProject.

        Returns:
            List of time entry activity dictionaries with id, name, and other properties

        Raises:
            QueryExecutionError: If the query fails

        """
        # Use file-based JSON retrieval to avoid console control-character issues.
        # Unique container filename per call so concurrent invocations
        # (e.g. across parallel migrations) don't overwrite each other's
        # results before they're read back.
        query = (
            "TimeEntryActivity.active.map { |activity| "
            "{ id: activity.id, name: activity.name, position: activity.position, "
            "is_default: activity.is_default, active: activity.active } }"
        )
        container_file = self._client._generate_unique_temp_filename("time_entry_activities")

        try:
            result = self._client.execute_large_query_to_json_file(
                query,
                container_file=container_file,
                timeout=60,
            )
            return result if isinstance(result, list) else []
        except Exception as e:
            msg = f"Failed to retrieve time entry activities: {e}"
            raise QueryExecutionError(msg) from e

    def get_time_entries(
        self,
        work_package_id: int | None = None,
        user_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get time entries from OpenProject with optional filtering.

        Args:
            work_package_id: Filter by work package ID
            user_id: Filter by user ID
            limit: Maximum number of entries to return

        Returns:
            List of time entry dictionaries

        Raises:
            QueryExecutionError: If the query fails

        """
        conditions = []
        if work_package_id:
            conditions.append(f"entity_id: {int(work_package_id)}, entity_type: 'WorkPackage'")
        if user_id:
            conditions.append(f"user_id: {int(user_id)}")

        where_clause = f".where({', '.join(conditions)})" if conditions else ""

        # Build Ruby expression that avoids relying on a 'work_package' association (use explicit lookup)
        query = (
            f"TimeEntry{where_clause}.limit({int(limit)})"
            ".map do |entry| "
            "wp = entry.entity if entry.entity_type == 'WorkPackage'; "
            "act = (begin entry.activity; rescue; nil end); usr = (begin entry.user; rescue; nil end); "
            "{ id: entry.id, "
            "work_package_id: wp&.id, "
            "work_package_subject: (wp ? wp.subject : nil), "
            "user_id: entry.user_id, user_name: (usr ? usr.name : nil), "
            "activity_id: entry.activity_id, activity_name: (act ? act.name : nil), "
            "hours: entry.hours.to_f, spent_on: entry.spent_on.to_s, "
            "comments: entry.comments, created_at: entry.created_at.to_s, updated_at: entry.updated_at.to_s, "
            "custom_fields: entry.custom_values.as_json(only: [:custom_field_id, :value]) } end"
        )

        try:
            # Use a unique container file to avoid collisions across concurrent calls
            unique_name = f"j2o_time_entries_{os.getpid()}_{int(time.time())}_{os.urandom(2).hex()}.json"
            result = self._client.execute_large_query_to_json_file(
                query,
                container_file=unique_name,
                timeout=120,
            )
            return result if isinstance(result, list) else []
        except Exception as e:
            msg = "Failed to retrieve time entries."
            raise QueryExecutionError(msg) from e

    # ── writes ───────────────────────────────────────────────────────────

    def create_time_entry(
        self,
        time_entry_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create a time entry in OpenProject.

        Args:
            time_entry_data: Time entry data in OpenProject API format

        Returns:
            Created time entry data with ID, or None if creation failed

        Raises:
            QueryExecutionError: If the creation fails
            ValueError: If the embedded payload is missing required IDs

        """
        row = self._entry_row(time_entry_data, 0)
        row["return_record"] = True
        result = self._submit_rows([row], [])["results"][0]
        return result if result.get("success") else None

    @staticmethod
    def _entry_row(entry: dict[str, Any], index: int) -> dict[str, Any]:
        wp, user, activity = _time_entry_ids(entry)
        if not all((wp, user, activity)):
            msg = f"Missing required IDs: work_package_id={wp}, user_id={user}, activity_id={activity}"
            raise ValueError(msg)
        meta = entry.get("_meta") or {}
        if ("jira_work_log_id" in meta or "tempo_worklog_id" in meta) and not meta.get("jira_worklog_key"):
            msg = "The source work log has no origin key."
            raise ValueError(msg)
        if meta.get("tempo_attributes") and not meta.get("tempo_archive"):
            msg = "Tempo values require a verified and encrypted Jira worklog."
            raise ValueError(msg)
        if archive := meta.get("tempo_archive"):
            if not all(meta.get(k) for k in ("jira_started", "jira_worklog_key", "target_project_id")):
                msg = "The protected Tempo archive requires a verified Jira origin and target project."
                raise ValueError(msg)
            issue = meta["jira_worklog_key"].partition(":")[0]
            if not archive["issue_url"].endswith("/browse/" + issue) or not re.fullmatch(
                r"[1-9]\d*", archive["issue_id"], re.ASCII
            ):
                msg = "The protected Tempo archive points to a different Jira issue."
                raise ValueError(msg)
            verify_packets(archive["packet"], archive["packet"], meta["jira_worklog_key"])
        keys = (
            "target_project_id",
            "jira_worklog_key",
            "jira_started",
            "jira_update_author_id",
            "jira_time_zone",
            "jira_start_minute",
            "jira_created_at",
            "jira_updated_at",
        )
        return {
            "index": index,
            "work_package_id": wp,
            "user_id": user,
            "activity_id": activity,
            "hours": entry.get("hours"),
            "spent_on": entry.get("spentOn"),
            "comments": _comment_text(entry),
            **({"tempo_archive": {k: archive[k] for k in ("packet", "issue_id", "issue_url")}} if archive else {}),
            **{key: meta[key] for key in keys if meta.get(key) is not None},
        }

    def batch_create_time_entries(
        self,
        time_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple time entries via file-based JSON in the container.

        This avoids console output parsing by writing input and results to files
        inside the container and reading results back via docker exec.

        Args:
            time_entries: List of time entry data dictionaries

        Returns:
            Dictionary with creation results and statistics. The Ruby
            runner catches per-entry exceptions and continues, so the
            returned dict can have ``failed > 0`` alongside ``created >
            0`` — partial success is the normal shape, not an error.

        Raises:
            QueryExecutionError: If the Rails execution itself fails or
                the result file cannot be retrieved or parsed (in
                contrast to per-entry failures, which are reflected in
                the returned ``results`` array rather than raising).

        """
        if not time_entries:
            return {"created": 0, "failed": 0, "results": []}

        entries_data = []
        skipped_results = []
        for i, entry in enumerate(time_entries):
            try:
                entries_data.append(self._entry_row(entry, i))
            except ValueError as exc:
                skipped_results.append({"index": i, "success": False, "error": str(exc)})

        if not entries_data:
            return {
                "created": 0,
                "failed": len(time_entries),
                "results": skipped_results,
            }

        return self._submit_rows(entries_data, skipped_results)

    def _submit_rows(self, entries_data: list[dict], skipped_results: list[dict]) -> dict[str, Any]:
        client = self._client
        temp_root = Path(client.file_manager.data_dir) / "bulk_create"
        temp_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=temp_root) as local, private_container_directory(client.docker_client) as directory:
            return self._write_time_entries(entries_data, skipped_results, Path(local), directory)

    def _write_time_entries(
        self,
        entries_data: list[dict[str, Any]],
        skipped_results: list[dict[str, Any]],
        temp_dir: Path,
        directory: Path,
    ) -> dict[str, Any]:
        client = self._client
        local_json = temp_dir / "time_entries.json"
        with local_json.open("w", encoding="utf-8") as f:
            json.dump(entries_data, f)
        container_json = directory / local_json.name
        client.transfer_file_to_container(local_json, container_json)
        container_result = directory / "result.json"
        local_result = temp_dir / container_result.name

        # Build Ruby runner that writes results JSON to file via helper assembly
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        header_lines = [
            f"data_path = '{escape_ruby_single_quoted(container_json.as_posix())}'",
            f"result_path = '{escape_ruby_single_quoted(container_result.as_posix())}'",
        ]
        ruby_lines = _BULK_TIME_ENTRY_BODY.splitlines()
        # Wrap the body in an outer ``begin/rescue/ensure`` so any
        # top-level Ruby error (e.g. JSON parse failure on the input
        # file, exception before the per-entry loop) writes an
        # actionable error payload to ``result_path``. Without this,
        # ``rails_client.execute(..., suppress_output=True)`` would
        # eat the error and the only diagnostic would be a missing
        # result file with no context.
        wrapped_ruby_lines = (
            ["begin"]
            + ["  " + line for line in ruby_lines]
            + [
                "rescue => e",
                "  File.write(result_path, JSON.generate({",
                "    created: 0,",
                "    failed: -1,",
                "    error: e.class.name + ': ' + e.message,",
                "    backtrace: e.backtrace.first(5),",
                "    results: []",
                "  }))",
                "  raise",
                "end",
            ]
        )
        ruby = (
            "\n".join(
                [
                    "require 'json'",
                    "require 'date'",
                    "require 'logger'",
                    *header_lines,
                    *wrapped_ruby_lines,
                ],
            )
            + "\n"
        )

        try:
            _ = client.rails_client.execute(ruby, timeout=120, suppress_output=True)
        except Exception as e:
            msg = f"Rails execution failed for batch_create_time_entries: {e}"
            raise QueryExecutionError(msg) from e

        # Retrieve result file
        max_wait_seconds = 30
        poll_interval = 1.0
        waited = 0.0
        while waited < max_wait_seconds:
            try:
                client.transfer_file_from_container(container_result, local_result)
                break
            except Exception:
                time.sleep(poll_interval)
                waited += poll_interval

        if not local_result.exists():
            msg = "Result file not found after batch_create_time_entries execution"
            raise QueryExecutionError(msg)

        with local_result.open("r", encoding="utf-8") as f:
            ruby_result: dict[str, Any] = json.load(f)

        self._verify_tempo_receipts(entries_data, ruby_result)
        # Merge the malformed-input failures captured during the
        # Python pre-pass with the Ruby-side per-entry results so
        # the returned dict reflects every input index 1:1.
        if skipped_results:
            ruby_result.setdefault("results", []).extend(skipped_results)
            ruby_result["failed"] = int(ruby_result.get("failed", 0)) + len(skipped_results)
        return ruby_result

    @staticmethod
    def _verify_tempo_receipts(rows: list[dict], result: dict) -> None:
        by_index: dict[int, list[dict]] = {}
        for receipt in result.get("results", []):
            by_index.setdefault(receipt["index"], []).append(receipt)
        for row in rows:
            if "tempo_archive" not in row:
                continue
            matches = by_index.get(row["index"], [])
            if len(matches) != 1:
                msg = "Missing or ambiguous Tempo archive receipt."
                raise QueryExecutionError(msg)
            if matches[0].get("success"):
                OpenProjectTimeEntryService._verify_tempo_receipt(row, matches[0], result)

    @staticmethod
    def _verify_tempo_receipt(row: dict, receipt: dict, result: dict) -> None:
        try:
            if receipt.get("verified_worklog_key") != row["jira_worklog_key"] or not receipt.get("id"):
                msg = "Tempo archive receipt has the wrong origin."
                raise ValueError(msg)
            verify_packets(row["tempo_archive"]["packet"], receipt.pop("tempo_packet", ""), row["jira_worklog_key"])
        except ValueError as exc:
            key = "reused" if receipt.get("reused") else "created"
            result[key] -= 1
            result["failed"] += 1
            receipt.clear()
            receipt.update(index=row["index"], success=False, error=str(exc))
        else:
            receipt["tempo_attributes_verified"] = True
