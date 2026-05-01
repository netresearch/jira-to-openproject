"""Time-entry helpers for the OpenProject Rails console.

Phase 2q of ADR-002 continues the openproject_client.py god-class
decomposition by collecting the four time-entry helpers onto a focused
service. The service owns:

* **Activities** — ``get_time_entry_activities`` fetches all active
  ``TimeEntryActivity`` records via a file-based JSON query.
* **Single write** — ``create_time_entry`` extracts IDs from the
  OpenProject API-format ``_embedded`` payload, builds a Ruby script
  with provenance-CF assignment, and calls
  ``execute_query_to_json_file``.
* **Reads** — ``get_time_entries`` returns time entries with optional
  ``work_package_id`` / ``user_id`` filters.
* **Batch write** — ``batch_create_time_entries`` transfers a JSON
  payload to the container and runs a Rails runner script that
  creates ``TimeEntry`` records in bulk, writing structured results
  to a result file. Per-entry failures are captured in the result
  rather than raising, so partial success is the normal shape.

``OpenProjectClient`` exposes the service via ``self.time_entries``
and keeps thin delegators for the same method names so existing
callers work unchanged.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.openproject_client import OpenProjectClient


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
            conditions.append(f"work_package_id: {work_package_id}")
        if user_id:
            conditions.append(f"user_id: {user_id}")

        where_clause = f".where({', '.join(conditions)})" if conditions else ""

        # Build Ruby expression that avoids relying on a 'work_package' association (use explicit lookup)
        query = (
            f"TimeEntry{where_clause}.limit({limit})"
            ".map do |entry| "
            "wp = (begin WorkPackage.find_by(id: entry.work_package_id); rescue; nil end); "
            "act = (begin entry.activity; rescue; nil end); usr = (begin entry.user; rescue; nil end); "
            "{ id: entry.id, "
            "work_package_id: entry.work_package_id, "
            "work_package_subject: (wp ? wp.subject : nil), "
            "user_id: entry.user_id, user_name: (usr ? usr.name : nil), "
            "activity_id: entry.activity_id, activity_name: (act ? act.name : nil), "
            "hours: entry.hours.to_f, spent_on: entry.spent_on.to_s, "
            "comments: entry.comments, created_at: entry.created_at.to_s, updated_at: entry.updated_at.to_s, "
            "custom_fields: (begin cf = entry.custom_field_values; cf.respond_to?(:to_json) ? cf : {}; rescue; {} end) } end"
        )

        try:
            # Use a unique container file to avoid collisions across concurrent calls
            unique_name = f"/tmp/j2o_time_entries_{os.getpid()}_{int(time.time())}_{os.urandom(2).hex()}.json"
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
        # Lazy import: ``escape_ruby_single_quoted`` lives on
        # openproject_client; lazy keeps the service ↔ client cycle out
        # of module-load time.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        # Extract embedded references and convert to IDs
        embedded = time_entry_data.get("_embedded", {})

        # Get work package ID from href
        work_package_href = embedded.get("workPackage", {}).get("href", "")
        work_package_id = None
        if work_package_href:
            # Extract ID from href like "/api/v3/work_packages/123"
            match = re.search(r"/work_packages/(\d+)", work_package_href)
            if match:
                work_package_id = int(match.group(1))

        # Get user ID from href
        user_href = embedded.get("user", {}).get("href", "")
        user_id = None
        if user_href:
            # Extract ID from href like "/api/v3/users/456"
            match = re.search(r"/users/(\d+)", user_href)
            if match:
                user_id = int(match.group(1))

        # Get activity ID from href
        activity_href = embedded.get("activity", {}).get("href", "")
        activity_id = None
        if activity_href:
            # Extract ID from href like "/api/v3/time_entries/activities/789"
            match = re.search(r"/activities/(\d+)", activity_href)
            if match:
                activity_id = int(match.group(1))

        if not all([work_package_id, user_id, activity_id]):
            msg = (
                f"Missing required IDs: work_package_id={work_package_id}, user_id={user_id}, activity_id={activity_id}"
            )
            raise ValueError(
                msg,
            )

        # Normalize comment value (can be string or {raw,text})
        comment_obj = time_entry_data.get("comment", "")
        if isinstance(comment_obj, dict):
            comment_str = comment_obj.get("raw") or comment_obj.get("text") or str(comment_obj)
        else:
            comment_str = str(comment_obj)

        # Prepare the script with proper Ruby syntax
        script = f"""
        begin
          require 'logger'
          begin; Rails.logger.level = Logger::WARN; rescue; end
          begin; ActiveJob::Base.logger = Logger.new(nil); rescue; end
          begin; GoodJob.logger = Logger.new(nil); rescue; end
          time_entry = TimeEntry.new(
            entity_id: {work_package_id},
            entity_type: 'WorkPackage',
            user_id: {user_id},
            logged_by_id: {user_id},
            activity_id: {activity_id},
            hours: {float(time_entry_data.get("hours", 0))},
            spent_on: Date.parse('{escape_ruby_single_quoted(time_entry_data.get("spentOn", ""))}'),
            comments: '{escape_ruby_single_quoted(comment_str)}'
          )

          # Ensure project is set from associated work package to satisfy validations
          begin
            wp = WorkPackage.find_by(id: {work_package_id})
            if wp
              time_entry.entity = wp
              time_entry.project = wp.project
            end
          rescue => e
            # ignore association errors here; validations will surface below
          end

          # Provenance CF for time entries: J2O Origin Worklog Key
          begin
            key = '{escape_ruby_single_quoted(str(time_entry_data.get("_meta", {}).get("jira_worklog_key") or ""))}'
            # Empty strings are truthy in Ruby — skip CF assignment
            # explicitly when the key is missing/blank so we don't
            # create or set provenance for entries without a worklog
            # source.
            if !key.nil? && !key.strip.empty?
              cf = CustomField.find_by(type: 'TimeEntryCustomField', name: 'J2O Origin Worklog Key')
              if !cf
                cf = CustomField.new(name: 'J2O Origin Worklog Key', field_format: 'string',
                  is_required: false, is_for_all: true, type: 'TimeEntryCustomField')
                cf.save
              end
              begin
                time_entry.custom_field_values = {{ cf.id => key }}
              rescue => e
                # ignore CF assignment errors
              end
            end
          rescue => e
            # ignore provenance CF errors
          end

          if time_entry.save
            {{
              id: time_entry.id,
              work_package_id: time_entry.entity_id,
              user_id: time_entry.user_id,
              activity_id: time_entry.activity_id,
              hours: time_entry.hours.to_f,
              spent_on: time_entry.spent_on.to_s,
              comments: time_entry.comments,
              created_at: time_entry.created_at.to_s,
              updated_at: time_entry.updated_at.to_s
            }}
          else
            {{
              error: "Validation failed",
              errors: time_entry.errors.full_messages
            }}
          end
        rescue => e
          {{
            error: "Creation failed",
            message: e.message,
            backtrace: e.backtrace.first(3)
          }}
        end
        """

        try:
            result = self._client.execute_query_to_json_file(script)

            if isinstance(result, dict):
                if result.get("error"):
                    self._logger.warning("Time entry creation failed: %s", result)
                    return None
                return result

            self._logger.warning("Unexpected time entry creation result: %s", result)
            return None

        except Exception as e:
            msg = f"Failed to create time entry: {e}"
            raise QueryExecutionError(msg) from e

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

        client = self._client

        # Build entries data with necessary fields and ID extraction.
        # Track skipped (malformed) entries explicitly so the returned
        # summary reflects every input index, not just the ones that
        # made it into the Ruby payload — callers can then reconcile
        # input list ↔ output ``results`` 1:1.
        entries_data: list[dict[str, Any]] = []
        skipped_results: list[dict[str, Any]] = []
        for i, entry_data in enumerate(time_entries):
            embedded = entry_data.get("_embedded", {})

            def extract_id(pattern: str, href: str) -> int | None:
                m = re.search(pattern, href or "")
                return int(m.group(1)) if m else None

            work_package_id = extract_id(r"/work_packages/(\d+)", embedded.get("workPackage", {}).get("href", ""))
            user_id = extract_id(r"/users/(\d+)", embedded.get("user", {}).get("href", ""))
            activity_id = extract_id(r"/activities/(\d+)", embedded.get("activity", {}).get("href", ""))

            if not all([work_package_id, user_id, activity_id]):
                # Record a structured failure for the caller — preserves
                # the 1:1 input/output mapping the bulk callers rely on.
                skipped_results.append(
                    {
                        "index": i,
                        "success": False,
                        "error": (
                            "Missing required IDs: "
                            f"work_package_id={work_package_id}, user_id={user_id}, activity_id={activity_id}"
                        ),
                    },
                )
                continue

            # Normalize comment to string
            comment_obj = entry_data.get("comment", "")
            if isinstance(comment_obj, dict):
                comment_str = comment_obj.get("raw") or comment_obj.get("text") or str(comment_obj)
            else:
                comment_str = str(comment_obj)

            entries_data.append(
                {
                    "index": i,
                    "work_package_id": work_package_id,
                    "user_id": user_id,
                    "activity_id": activity_id,
                    "hours": entry_data.get("hours", 0),
                    "spent_on": entry_data.get("spentOn", ""),
                    "comments": comment_str,
                    "jira_worklog_key": (entry_data.get("_meta", {}) or {}).get("jira_worklog_key"),
                },
            )

        if not entries_data:
            return {
                "created": 0,
                "failed": len(time_entries),
                "results": skipped_results,
            }

        # Prepare local JSON payload
        temp_dir = Path(client.file_manager.data_dir) / "bulk_create"
        temp_dir.mkdir(parents=True, exist_ok=True)
        local_json = temp_dir / f"time_entries_bulk_{os.urandom(4).hex()}.json"
        with local_json.open("w", encoding="utf-8") as f:
            json.dump(entries_data, f)

        # Transfer JSON to container and define result path
        container_json = Path("/tmp") / local_json.name
        client.transfer_file_to_container(local_json, container_json)

        result_name = f"bulk_result_time_entries_{os.urandom(3).hex()}.json"
        container_result = Path("/tmp") / result_name
        local_result = temp_dir / result_name

        # Build Ruby runner that writes results JSON to file via helper assembly
        header_lines = [
            f"data_path = '{container_json.as_posix()}'",
            f"result_path = '{container_result.as_posix()}'",
        ]
        ruby_lines = [
            "begin; Rails.logger.level = Logger::WARN; rescue; end",
            "begin; ActiveJob::Base.logger = Logger.new(nil); rescue; end",
            "begin; GoodJob.logger = Logger.new(nil); rescue; end",
            "entries = JSON.parse(File.read(data_path), symbolize_names: true)",
            "results = []",
            "created_count = 0",
            "failed_count = 0",
            "entries.each do |entry|",
            "  begin",
            "    te = TimeEntry.new(",
            "      activity_id: entry[:activity_id],",
            "      hours: entry[:hours],",
            "      spent_on: Date.parse(entry[:spent_on]),",
            "      comments: entry[:comments],",
            "      entity_id: entry[:work_package_id],",
            "      entity_type: 'WorkPackage'",
            "    )",
            "    begin",
            "      wp = WorkPackage.find_by(id: entry[:work_package_id])",
            "      if wp.nil?",
            "        failed_count += 1",
            "        results << { index: entry[:index], success: false, error: 'WorkPackage not found: ' + entry[:work_package_id].to_s }",
            "        next",
            "      end",
            "      te.entity = wp",
            "      te.project = wp.project",
            "    rescue => e",
            "      failed_count += 1",
            "      results << { index: entry[:index], success: false, error: 'WorkPackage lookup failed: ' + e.message }",
            "      next",
            "    end",
            "    begin",
            "      user = User.find_by(id: entry[:user_id])",
            "      if user.nil?",
            "        failed_count += 1",
            "        results << { index: entry[:index], success: false, error: 'User not found: ' + entry[:user_id].to_s }",
            "        next",
            "      end",
            "      te.user = user",
            "      te.user_id = entry[:user_id]",
            "      te.logged_by_id = entry[:user_id]",
            "    rescue => e",
            "      failed_count += 1",
            "      results << { index: entry[:index], success: false, error: 'User lookup failed: ' + e.message }",
            "      next",
            "    end",
            "    begin",
            "      key = entry[:jira_worklog_key]",
            # Use the canonical 'J2O Origin Worklog Key' name so this
            # batch path writes provenance to the SAME custom field as
            # ``create_time_entry`` and ``ensure_origin_custom_fields`` —
            # the previous 'Jira Worklog Key' would split provenance
            # across two CFs and break idempotency lookups.
            #
            # Empty strings are truthy in Ruby, so the existence check
            # has to be explicit (``!key.nil? && !key.to_s.empty?``)
            # instead of the bare ``if key`` that the pre-extraction
            # client code used.
            "      if !key.nil? && !key.to_s.empty?",
            "        cf = CustomField.find_by(type: 'TimeEntryCustomField', name: 'J2O Origin Worklog Key')",
            "        if !cf",
            "          cf = CustomField.new(name: 'J2O Origin Worklog Key', field_format: 'string', is_required: false, is_for_all: true, type: 'TimeEntryCustomField')",
            "          cf.save",
            "        end",
            "        begin",
            "          te.custom_field_values = { cf.id => key }",
            "        rescue => e",
            "        end",
            "      end",
            "    rescue => e",
            "    end",
            "    if te.save",
            "      created_count += 1",
            "      results << { index: entry[:index], success: true, id: te.id }",
            "    else",
            "      failed_count += 1",
            "      results << { index: entry[:index], success: false, errors: te.errors.full_messages }",
            "    end",
            "  rescue => e",
            "    failed_count += 1",
            "    results << { index: entry[:index], success: false, error: e.message }",
            "  end",
            "end",
            "File.write(result_path, JSON.generate({ created: created_count, failed: failed_count, results: results }))",
        ]
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

            # Merge the malformed-input failures captured during the
            # Python pre-pass with the Ruby-side per-entry results so
            # the returned dict reflects every input index 1:1.
            if skipped_results:
                ruby_result.setdefault("results", []).extend(skipped_results)
                ruby_result["failed"] = int(ruby_result.get("failed", 0)) + len(skipped_results)
            return ruby_result
        finally:
            # Best-effort cleanup of local + container temp files.
            # Non-critical: we already have the parsed result in memory
            # by this point, and any cleanup failure is logged at debug
            # so it doesn't mask the real result.
            for path in (local_json, local_result):
                try:
                    if path.exists():
                        path.unlink()
                except Exception as cleanup_err:
                    self._logger.debug(
                        "Non-critical: failed to remove local temp %s: %s",
                        path,
                        cleanup_err,
                    )
            for cpath in (container_json, container_result):
                try:
                    import shlex

                    rm_cmd = f"docker exec {shlex.quote(client.container_name)} rm -f {shlex.quote(cpath.as_posix())}"
                    client.ssh_client.execute_command(rm_cmd, check=False)
                except Exception as cleanup_err:
                    self._logger.debug(
                        "Non-critical: failed to remove container temp %s: %s",
                        cpath,
                        cleanup_err,
                    )
