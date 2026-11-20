# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Bulk record creation pipeline for the OpenProject Rails console.

Phase 2y of ADR-002 continues the openproject_client.py god-class
decomposition by collecting the bulk-creation pipeline — the largest
single chunk left on the client — onto a focused service.

The service owns:

* **Generic mass-create** — ``bulk_create_records`` mass-creates
  records of any allow-listed Rails model. It serializes the input
  to JSON, transfers it to the container, runs a self-contained Ruby
  script (preferring ``rails runner`` for long scripts and falling
  back to the persistent console), polls a result file plus an
  optional progress sidecar, and reads back a structured envelope
  (``status``, ``created``, ``errors``, ``created_count``,
  ``error_count``, ``total``). The Ruby script knows enough about
  ``WorkPackage`` to apply Jira-key and provenance custom fields.
* **Work-package batch wrapper** — ``batch_create_work_packages``
  is a thin entry point that hands a list of WP payloads to the
  ``performance_optimizer.batch_processor.process_batches`` helper,
  which slices it and calls ``_create_work_packages_batch`` for each
  slice.
* **Work-package batch worker** — ``_create_work_packages_batch``
  builds a separate Ruby script (read JSON from container file,
  pre-fetch all referenced Project / Type / Status / Priority / User
  records to avoid N+1 lookups, then create each WP with provenance
  CFs and optional original timestamps via ``update_columns``) and
  runs it via ``execute_json_query``. Per-WP failures end up in the
  result list; partial success is the normal shape.

``OpenProjectClient`` exposes the service via ``self.bulk_create``
and keeps thin delegators for the same method names so existing
callers — including the private ``_create_work_packages_batch`` call
that ``OpenProjectWorkPackageService.create_work_package`` makes
through ``self._client._create_work_packages_batch`` — keep working
unchanged.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import config
from src.domain import provenance_fields
from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.container_staging import private_container_directory
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.rails_console_client import RubyError
from src.infrastructure.openproject.rails_execution import once_per_staging_directory

_JSON_SUFFIX = ".json"


# User IDs supplied by Jira are distinct from Watcher primary keys. Keep source
# identity and memberships in the same transaction as the row they describe.
_PERSIST_WORK_PACKAGE = """
persist_work_package = lambda do |wp, custom_fields, issue_key, issue_id, watcher_user_ids|
  WorkPackage.transaction(requires_new: true) do
    wp.save!
    field_values = (custom_fields || []).to_h { |field| [Integer(field.fetch('id')), field.fetch('value')] }
    unless field_values.empty?
      wp.custom_field_values = field_values
      wp.save!(touch: false)
    end
    markers = { 'Jira Issue Key' => issue_key, 'J2O Origin Key' => issue_key, 'J2O Origin ID' => issue_id }
    markers['J2O Origin System'] = 'jira' if issue_key.present? || issue_id.present?
    markers.each do |name, value|
      next if value.blank?
      field = WorkPackageCustomField.find_or_create_by!(name: name) do |cf|
        cf.field_format = 'string'
        cf.is_for_all = true
      end
      marker = wp.custom_values.find_or_initialize_by(custom_field_id: field.id)
      if name == 'J2O Origin System' && marker.value.present?
        raise 'Conflicting Jira source system' unless jira_source_labels.include?(marker.value.to_s.strip.downcase)
        next
      end
      next if marker.value == value.to_s
      raise 'Conflicting Jira source identity' if marker.value.present?
      marker.update!(value: value.to_s)
    end
    raise ArgumentError, 'Invalid watcher user list' unless watcher_user_ids.is_a?(Array)
    watcher_user_ids.uniq.each do |id|
      raise ArgumentError, 'Invalid watcher user ID' unless id.is_a?(Integer) && id.positive?
      # Watcher's native validations also require a user who can see this work package.
      Watcher.find_or_create_by!(watchable: wp, user: User.active.find(id))
    end
  end
  true
end
"""


# Default script-load mode for bulk-create scripts. ``console`` ships the
# generated script as a file and ``load``s it inside the persistent tmux
# Rails console — avoiding the ~5-10s cold-start of ``bundle exec rails
# runner`` per batch. Override via ``J2O_SCRIPT_LOAD_MODE=runner`` if the
# tmux console is unavailable.
DEFAULT_SCRIPT_LOAD_MODE = "console"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _bulk_result_name(model: str, basename: str | None) -> str:
    if not basename:
        return f"bulk_result_{model.lower()}_{int(time.time())}_{os.getpid()}_{os.urandom(3).hex()}.json"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", Path(basename).name) or "bulk_result"
    return f"{base.removesuffix(_JSON_SUFFIX)}_{int(time.time())}_{os.getpid()}_{os.urandom(2).hex()}.json"


def _bulk_hint(model: str) -> str:
    try:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        proj = (config.jira_config or {}).get("project_filter")
        proj_part = f" project={proj}" if proj else ""
        return f"j2o: migration/bulk_create model={model}{proj_part} ts={ts} pid={os.getpid()}"
    except Exception:
        return f"j2o: migration/bulk_create model={model} pid={os.getpid()}"


class LegacyJournalReplayError(QueryExecutionError):
    """A caller requested legacy journal operations on work packages."""

    def __init__(self) -> None:
        super().__init__(
            "Legacy work-package journal replay is disabled. Use work_packages_skeleton "
            "and work_packages_content for work packages and j2o-comments migrate for "
            "protected comments. See docs/protected-comments.md."
        )


def reject_legacy_journal_operations(records: list[dict[str, Any]]) -> None:
    """Reject the entire batch before copying data or writing a target record."""
    for record in records:
        if not isinstance(record, dict):
            msg = "Work-package attributes must be a dict"
            raise QueryExecutionError(msg)
        operations = record.get("_rails_operations")
        if operations is not None and operations != []:
            raise LegacyJournalReplayError


class OpenProjectBulkCreateService:
    """Generic mass-create + WP-specific batch creation for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── helpers ──────────────────────────────────────────────────────────

    def bulk_create_records(
        self,
        model: str,
        records: list[dict[str, Any]],
        *,
        timeout: int | None = None,
        result_basename: str | None = None,
    ) -> dict[str, Any]:
        """Create many records for a given Rails model using a minimal Ruby script.

        Policy: Ruby performs only create; all mapping/sanitization/defaults must be done in Python.

        Args:
            model: Rails model name (e.g., "WorkPackage")
            records: List of sanitized attribute dicts suitable for mass-assignment
            timeout: Optional execution timeout
            result_basename: Optional basename used for the result file in the container

        Returns:
            Result envelope with keys: status, created, errors, created_count, error_count, total

        Raises:
            QueryExecutionError: On execution or retrieval failure

        """
        client = self._client
        if not isinstance(records, list):
            _msg = "records must be a list of dicts"
            raise QueryExecutionError(_msg)

        if model == "WorkPackage":
            reject_legacy_journal_operations(records)
        client._validate_model_name(model)

        temp_root = Path(client.file_manager.data_dir) / "bulk_create"
        temp_root.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(dir=temp_root) as local,
            private_container_directory(client.docker_client) as directory,
        ):
            return self._bulk_create_in_directory(
                model, records, timeout=timeout, result_basename=result_basename, directory=directory, local=Path(local)
            )

    def _bulk_create_in_directory(
        self,
        model: str,
        records: list[dict[str, Any]],
        *,
        timeout: int | None,
        result_basename: str | None,
        directory: Path,
        local: Path,
    ) -> dict[str, Any]:
        client = self._client
        # Prepare local JSON payload
        temp_dir = Path(client.file_manager.data_dir) / "bulk_create"
        local_json = local / "input.json"
        try:
            with local_json.open("w", encoding="utf-8") as f:
                json.dump(records, f)
        except Exception as e:
            _msg = f"Failed to serialize records: {e}"
            raise QueryExecutionError(_msg) from e

        # Transfer JSON to container
        container_json = directory / local_json.name
        client.transfer_file_to_container(local_json, container_json)

        result_name = _bulk_result_name(model, result_basename)
        container_result = directory / result_name
        local_result = temp_dir / result_name
        progress = container_result.with_suffix(".json.progress")
        script = self._bulk_script(model, container_json, container_result, progress)
        script = once_per_staging_directory(script, directory)
        output, runner = self._execute_bulk_script(script, local, progress, timeout)
        self._wait_for_bulk_result(container_result, local_result, runner)
        try:
            with local_result.open(encoding="utf-8") as f:
                result = json.load(f)
            if isinstance(output, str):
                result["output"] = output[:2000]
            return result
        except Exception as e:
            msg = f"Failed to parse result JSON: {e}"
            raise QueryExecutionError(msg) from e

    def _bulk_script(self, model: str, container_json: Path, container_result: Path, container_progress: Path) -> str:
        header = (
            f"# {_bulk_hint(model)}\n"
            "require 'json'\n"
            "require 'logger'\n"
            "begin; require 'fileutils'; rescue; end\n"
            f"model_name = '{model}'\n"
            f"jira_source_labels = {json.dumps(provenance_fields.JIRA_SOURCE_LABELS)}\n"
            f"{_PERSIST_WORK_PACKAGE}\n"
            f"data_path = '{container_json.as_posix()}'\n"
            f"result_path = '{container_result.as_posix()}'\n"
            # Ensure progress ENV defaults are present in both console and runner modes
            f"progress_file = '{container_progress.as_posix()}'\n"
            "ENV['J2O_BULK_PROGRESS_N'] ||= (ENV['J2O_BULK_PROGRESS_N'] || '50')\n"
        )
        ruby = (
            "# BUG #32 FIX: Disable stdout buffering completely\n"
            "$stdout.sync = true\n"
            "$stderr.sync = true\n"
            "puts '[RUBY] Script execution starting...'\n"
            "STDOUT.flush\n"
            "begin; Rails.logger.level = Logger::WARN; rescue; end\n"
            "begin; ActiveJob::Base.logger = Logger.new(nil); rescue; end\n"
            "begin; GoodJob.logger = Logger.new(nil); rescue; end\n"
            "verbose = (ENV['J2O_BULK_RUBY_VERBOSE'] == '1')\n"
            'puts "[RUBY] Verbose mode: #{verbose}"\n'
            "STDOUT.flush\n"
            "begin; FileUtils.rm_f(progress_file); rescue; end if progress_file\n"
            "progress_n = (ENV['J2O_BULK_PROGRESS_N'] || '50').to_i\n"
            "begin\n"
            "model = Object.const_get(model_name)\n"
            "data = JSON.parse(File.read(data_path))\n"
            "created = []\n"
            "errors = []\n"
            'puts "J2O bulk start: model=#{model_name} total=#{data.length} result=#{result_path}" if verbose\n'
            "begin; File.open(progress_file, 'a'){|f| f.write(\"START total=#{data.length}\\n\") }; rescue; end if progress_file\n"
            "data.each_with_index do |attrs, idx|\n"
            "  begin\n"
            "    pref_attrs = nil\n"
            "    rec = model.new\n"
            "    # Minimal association pre-assignments for WorkPackage to satisfy validations\n"
            "    if model_name == 'WorkPackage'\n"
            "      begin\n"
            "        rec.project_id = attrs['project_id'] if attrs.key?('project_id')\n"
            "        if attrs.key?('type_id') && attrs['type_id']\n"
            "          rec.type = Type.find_by(id: attrs['type_id'])\n"
            "        end\n"
            "        if attrs.key?('status_id') && attrs['status_id']\n"
            "          rec.status = Status.find_by(id: attrs['status_id'])\n"
            "        end\n"
            "        if attrs.key?('priority_id') && attrs['priority_id']\n"
            "          rec.priority = IssuePriority.find_by(id: attrs['priority_id'])\n"
            "        end\n"
            "        if attrs.key?('author_id') && attrs['author_id']\n"
            "          rec.author = User.find_by(id: attrs['author_id'])\n"
            "        end\n"
            "        # Ruby-side safety defaults when not provided (keeps script minimal)\n"
            "        rec.status ||= Status.order(:position).first\n"
            "        rec.priority ||= IssuePriority.order(:position).first\n"
            "        rec.type ||= Type.order(:position).first\n"
            "        # Keep keys; assign_attributes can safely set *_id again if present\n"
            "      rescue => e\n"
            "        # continue with remaining attributes\n"
            "      end\n"
            "    end\n"
            "    if model_name == 'User'\n"
            "      begin\n"
            "        pref_attrs = attrs.delete('pref_attributes')\n"
            "      rescue\n"
            "        pref_attrs = nil\n"
            "      end\n"
            "    end\n"
            "    # Extract and remove custom_fields, _rails_operations, and Jira keys from attrs before assign_attributes\n"
            "    # Jira keys are NOT valid WorkPackage attributes - would cause UnknownAttributeError\n"
            "    cf_data = nil\n"
            "    jira_id = nil\n"
            "    jira_key = nil\n"
            "    jira_issue_key = nil\n"
            "    begin\n"
            "      cf_data = attrs.delete('custom_fields') if attrs.key?('custom_fields')\n"
            "      attrs.delete('_rails_operations')\n"
            "      jira_id = attrs.delete('jira_id')\n"
            "      jira_issue_id = attrs.delete('jira_issue_id')\n"
            "      jira_id ||= jira_issue_id\n"
            "      jira_key = attrs.delete('jira_key') if attrs.key?('jira_key')\n"
            "      jira_issue_key = attrs.delete('jira_issue_key') if attrs.key?('jira_issue_key')\n"
            "    rescue\n"
            "    end\n"
            "    watcher_user_ids = attrs.delete('watcher_ids') || [] if model_name == 'WorkPackage'\n"
            "    rec.assign_attributes(attrs)\n"
            "    # Ensure defaults are applied AFTER assign_attributes to avoid blank overrides\n"
            "    if model_name == 'WorkPackage'\n"
            "      begin\n"
            "        rec.status ||= Status.order(:position).first\n"
            "        rec.priority ||= IssuePriority.order(:position).first\n"
            "        rec.type ||= Type.order(:position).first\n"
            "      rescue => e\n"
            "      end\n"
            "    end\n"
            "    # Provenance and preference handling\n"
            "    begin\n"
            "      if model_name == 'User' && pref_attrs.respond_to?(:each)\n"
            "        pref = rec.pref || rec.build_pref\n"
            "        pref_attrs.each do |k, v|\n"
            '          setter = "#{k}="\n'
            "          pref.public_send(setter, v) if pref.respond_to?(setter)\n"
            "        end\n"
            "        begin; pref.save; rescue; end\n"
            "      end\n"
            "    rescue\n"
            "    end\n"
            "    saved = if model_name == 'WorkPackage'\n"
            "      persist_work_package.call(rec, cf_data, jira_issue_key || jira_key, jira_id, watcher_user_ids)\n"
            "    else\n"
            "      rec.save\n"
            "    end\n"
            "    if saved\n"
            "      created << {'index' => idx, 'id' => rec.id}\n"
            '      puts "J2O bulk item #{idx}: saved id=#{rec.id}" if verbose\n'
            "    else\n"
            "      errors << {'index' => idx, 'errors' => rec.errors.full_messages}\n"
            "      puts \"J2O bulk item #{idx}: failed #{rec.errors.full_messages.join(', ')}\" if verbose\n"
            "    end\n"
            "    if progress_n > 0 && ((idx + 1) % progress_n == 0)\n"
            "      begin; File.open(progress_file, 'a'){|f| f.write('.') }; rescue; end if progress_file\n"
            "      puts '.' if verbose\n"
            "    end\n"
            "    if verbose && progress_n > 0 && ((idx + 1) % (progress_n * 10) == 0)\n"
            '      puts "processed=#{idx + 1}/#{data.length}"\n'
            "    end\n"
            "  rescue => e\n"
            "    errors << {'index' => idx, 'errors' => [e.message]}\n"
            '    puts "J2O bulk item #{idx}: exception #{e.class}: #{e.message}" if verbose\n'
            "  end\n"
            "end\n"
            "result = {\n"
            "  'status' => 'success',\n"
            "  'created' => created,\n"
            "  'errors' => errors,\n"
            "  'created_count' => created.length,\n"
            "  'error_count' => errors.length,\n"
            "  'total' => data.length\n"
            "}\n"
            "File.open(result_path, 'w') do |f|\n"
            "  f.write(JSON.generate(result))\n"
            "  begin; f.flush; f.fsync; rescue; end\n"
            "end\n"
            "begin; FileUtils.chmod(0644, result_path); rescue; end\n"
            'puts "J2O bulk done: created=#{created.length} errors=#{errors.length} total=#{data.length} -> #{result_path}" if verbose\n'
            "begin; File.open(progress_file, 'a'){|f| f.write(\"\\nDONE #{created.length}/#{data.length}\\n\") }; rescue; end if progress_file\n"
            "rescue => top_e\n"
            "  begin\n"
            "    err = { 'status' => 'error', 'message' => top_e.message, 'backtrace' => (top_e.backtrace || []).take(20) }\n"
            "    File.open(result_path + '.error.json', 'w') do |f|\n"
            "      f.write(JSON.generate(err))\n"
            "      begin; f.flush; f.fsync; rescue; end\n"
            "    end\n"
            "    begin; FileUtils.chmod(0644, result_path + '.error.json'); rescue; end\n"
            '    puts "J2O bulk error: #{top_e.class}: #{top_e.message} -> #{result_path}.error.json" if verbose\n'
            "  rescue; end\n"
            "end\n"
        )

        return header + ruby

    def _execute_bulk_script(
        self, script: str, local: Path, progress: Path, timeout: int | None
    ) -> tuple[str | None, Path | None]:
        max_lines = _env_int("J2O_SCRIPT_RUNNER_MAX_LINES", 10)
        max_chars = _env_int("J2O_SCRIPT_RUNNER_THRESHOLD", 200)
        if script.count("\n") + 1 < max_lines and len(script) < max_chars:
            try:
                suppress_output = os.getenv("J2O_BULK_PROGRESS_CONSOLE", "0") != "1"
                return self._client.rails_client.execute(
                    script, timeout=timeout or 120, suppress_output=suppress_output
                ), None
            except Exception as exc:
                msg = f"Rails execution failed for bulk_create_records: {exc}"
                raise QueryExecutionError(msg) from exc

        runner = progress.parent / "bulk.rb"
        source = local / runner.name
        source.write_text(script, encoding="utf-8")
        self._client.docker_client.transfer_file_to_container(source, runner)
        if (os.getenv("J2O_SCRIPT_LOAD_MODE") or DEFAULT_SCRIPT_LOAD_MODE).lower() == "console":
            try:
                self._client.rails_client.execute(
                    f"load '{runner.as_posix()}'", timeout=timeout or 120, suppress_output=True
                )
                return None, runner
            except RubyError as exc:
                # The script ran and failed; the runner would only meet its once-only guard.
                msg = f"Rails execution failed for bulk_create_records: {exc}"
                raise QueryExecutionError(msg) from exc
            except Exception as exc:
                if os.getenv("J2O_ALLOW_RUNNER_FALLBACK", "0").lower() not in {"1", "true"}:
                    msg = "Rails console execution failed and runner fallback is disabled"
                    raise QueryExecutionError(msg) from exc
        self._run_bulk_runner(runner, progress, timeout)
        return None, runner

    def _stop_bulk_runner(self, runner: Path | None) -> None:
        if runner is None:
            return
        try:
            self._client.docker_client.execute_command(
                f'pkill -f "rails runner {runner.as_posix()}" || true', timeout=10
            )
        except Exception:
            self._logger.debug("Could not stop timed-out bulk runner %s", runner, exc_info=True)

    def _run_bulk_runner(self, runner: Path, progress: Path, timeout: int | None) -> None:
        try:
            stdout, stderr, status = self._client.docker_client.execute_command(
                f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner.as_posix()}",
                timeout=timeout or 120,
                env={
                    "J2O_BULK_RUBY_VERBOSE": os.getenv("J2O_BULK_RUBY_VERBOSE", "1"),
                    "J2O_BULK_PROGRESS_FILE": progress.as_posix(),
                    "J2O_BULK_PROGRESS_N": os.getenv("J2O_BULK_PROGRESS_N", "50"),
                },
            )
        except subprocess.TimeoutExpired as exc:
            self._stop_bulk_runner(runner)
            msg = f"rails runner timed out for {runner}"
            raise QueryExecutionError(msg) from exc
        if status:
            msg = f"rails runner failed (rc={status}): {stderr[:500]}"
            raise QueryExecutionError(msg)
        if stdout:
            self._logger.info("runner stdout: %s", stdout[:10000])

    def _copy_if_ready(self, remote: Path, local: Path) -> bool:
        try:
            if not self._client.docker_client.check_file_exists_in_container(remote):
                return False
            self._client.transfer_file_from_container(remote, local)
        except Exception:
            self._logger.debug("Bulk file not yet readable: %s", remote, exc_info=True)
            return False
        return True

    def _read_sidecar(self, remote: Path, local: Path, suffix: str) -> str | None:
        source = remote.with_suffix(remote.suffix + suffix)
        target = local.with_suffix(local.suffix + suffix)
        if not self._copy_if_ready(source, target):
            return None
        try:
            return target.read_text(encoding="utf-8")
        except OSError:
            self._logger.debug("Could not read bulk sidecar %s", target, exc_info=True)
            return None

    def _wait_for_bulk_result(self, remote: Path, local: Path, runner: Path | None) -> None:
        wait_seconds = _env_int("J2O_BULK_RESULT_WAIT_SECONDS", 180)
        stall_seconds = _env_int("J2O_BULK_STALL_SECONDS", 120)
        progress_size, last_change = -1, 0
        for elapsed in range(wait_seconds):
            if self._copy_if_ready(remote, local):
                return
            error = self._read_sidecar(remote, local, ".error.json")
            if error:
                self._logger.error("Bulk runner error: %s", error[:500])
            if elapsed % 5 == 0:
                progress = self._read_sidecar(remote, local, ".progress")
                if progress is None:
                    self._logger.info("Waiting for bulk result file %s (waited %ds)", remote, elapsed)
                elif len(progress) != progress_size:
                    progress_size, last_change = len(progress), elapsed
                    self._logger.info("Bulk progress: ~%s processed (waited %ds)", progress.count("."), elapsed)
                elif elapsed - last_change >= stall_seconds:
                    self._stop_bulk_runner(runner)
                    msg = f"bulk_create_records stalled for {stall_seconds}s without progress"
                    raise QueryExecutionError(msg)
            time.sleep(1)
        msg = "Result file not found after bulk_create_records execution"
        raise QueryExecutionError(msg)

    # ── work-package batch wrappers ──────────────────────────────────────

    def batch_create_work_packages(
        self,
        work_packages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple work packages in batches for optimal performance."""
        reject_legacy_journal_operations(work_packages)
        if not work_packages:
            return {"created": 0, "failed": 0, "results": []}

        return self._client.performance_optimizer.batch_processor.process_batches(
            work_packages,
            self._create_work_packages_batch,
        )

    def _create_work_packages_batch(
        self,
        work_packages: list[dict[str, Any]],
        **_kwargs: object,
    ) -> dict[str, Any]:
        """Create a batch of work packages using Rails."""
        reject_legacy_journal_operations(work_packages)
        if not work_packages:
            return {"created": 0, "failed": 0, "results": []}

        client = self._client

        with private_container_directory(client.docker_client) as directory:
            return self._execute_work_package_batch(work_packages, directory)

    def _execute_work_package_batch(self, work_packages: list[dict[str, Any]], directory: Path) -> dict[str, Any]:
        client = self._client
        container_json_path = (directory / "work_packages.json").as_posix()
        with tempfile.NamedTemporaryFile(mode="w", suffix=_JSON_SUFFIX, encoding="utf-8") as payload:
            json.dump(work_packages, payload)
            payload.flush()
            client.docker_client.transfer_file_to_container(Path(payload.name), Path(container_json_path))

        # Build batch work package creation script - read JSON from file
        script = f"""
        work_packages_data = JSON.parse(File.read('{container_json_path}'))
        created_count = 0
        failed_count = 0
        results = []

        # Pre-fetch all referenced entities to avoid N+1 queries (6N -> 5 constant queries)
        project_ids = work_packages_data.map {{ |d| d['project_id'] }}.compact.uniq
        type_ids = work_packages_data.map {{ |d| d['type_id'] }}.compact.uniq
        type_names = work_packages_data.map {{ |d| d['type_name'] }}.compact.uniq
        status_ids = work_packages_data.map {{ |d| d['status_id'] }}.compact.uniq
        status_names = work_packages_data.map {{ |d| d['status_name'] }}.compact.uniq
        priority_ids = work_packages_data.map {{ |d| d['priority_id'] }}.compact.uniq
        priority_names = work_packages_data.map {{ |d| d['priority_name'] }}.compact.uniq
        user_ids = work_packages_data.flat_map {{ |d| [d['author_id'], d['assigned_to_id']] }}.compact.uniq

        projects_by_id = Project.where(id: project_ids).index_by(&:id)
        types_by_id = Type.where(id: type_ids).index_by(&:id)
        types_by_name = Type.where(name: type_names).index_by(&:name)
        statuses_by_id = Status.where(id: status_ids).index_by(&:id)
        statuses_by_name = Status.where(name: status_names).index_by(&:name)
        priorities_by_id = IssuePriority.where(id: priority_ids).index_by(&:id)
        priorities_by_name = IssuePriority.where(name: priority_names).index_by(&:name)
        users_by_id = User.where(id: user_ids).index_by(&:id)

        work_packages_data.each do |wp_data|
          begin
            # Create work package with provided attributes
            wp = WorkPackage.new

            # Set basic attributes
            wp.subject = wp_data['subject'] if wp_data['subject']
            wp.description = wp_data['description'] if wp_data['description']

            # Set project (required) - using pre-fetched lookup
            if wp_data['project_id']
              wp.project = projects_by_id[wp_data['project_id']]
            end

            # Set type (required) - using pre-fetched lookup
            if wp_data['type_id']
              wp.type = types_by_id[wp_data['type_id']]
            elsif wp_data['type_name']
              wp.type = types_by_name[wp_data['type_name']]
            end
            # Safety net for type (issue #260) - same shape as status below.
            wp.type ||= (@default_type ||= Type.order(:position).first)

            # Set status - using pre-fetched lookup
            if wp_data['status_id']
              wp.status = statuses_by_id[wp_data['status_id']]
            elsif wp_data['status_name']
              wp.status = statuses_by_name[wp_data['status_name']]
            end
            # Safety net (issue #260): if the requested status_id or
            # status_name didn't resolve to a real Status record (e.g.
            # after an OP version upgrade that renumbered records, or
            # when the Python side fell back to a default that doesn't
            # exist on this instance), fall back to the first Status by
            # position so the WP doesn't fail validation with
            # "Status can't be blank". Memoise on the script's binding
            # via ``@default_status`` so we don't run
            # ``Status.order(:position).first`` once per failing WP
            # (N+1 inside the batch loop). Mirrors the single-WP code
            # paths earlier in this file.
            wp.status ||= (@default_status ||= Status.order(:position).first)

            # Set priority - using pre-fetched lookup
            if wp_data['priority_id']
              wp.priority = priorities_by_id[wp_data['priority_id']]
            elsif wp_data['priority_name']
              wp.priority = priorities_by_name[wp_data['priority_name']]
            end
            # Safety net for priority (issue #260) - same shape as status above.
            wp.priority ||= (@default_priority ||= IssuePriority.order(:position).first)

            # Set author - using pre-fetched lookup
            if wp_data['author_id']
              wp.author = users_by_id[wp_data['author_id']]
            end

            # Set assignee - using pre-fetched lookup
            if wp_data['assigned_to_id']
              wp.assigned_to = users_by_id[wp_data['assigned_to_id']]
            end

            # Assign provenance custom fields if provided as [{{id, value}}]
            begin
              cf_items = wp_data['custom_fields']
              if cf_items && cf_items.respond_to?(:each)
                cf_map = {{}}
                cf_items.each do |cf|
                  begin
                    cid = (cf['id'] || cf[:id])
                    val = (cf['value'] || cf[:value])
                    cf_map[cid] = val if cid
                  rescue
                  end
                end
                if cf_map.any?
                  begin
                    wp.custom_field_values = cf_map
                  rescue
                  end
                end
              end
            rescue
            end

            # Save the work package
            if wp.save
              created_count += 1

              # Set original timestamps if provided (using update_columns
              # to bypass callbacks). Wrapped in its own begin/rescue so
              # a single bad timestamp doesn't roll the whole WP into the
              # failure bucket — and so the failure surfaces in results
              # for diagnosis instead of being silently swallowed.
              begin
                timestamp_attrs = {{}}
                timestamp_attrs[:created_at] = Time.parse(wp_data['created_at']) if wp_data['created_at']
                timestamp_attrs[:updated_at] = Time.parse(wp_data['updated_at']) if wp_data['updated_at']
                wp.update_columns(timestamp_attrs) if timestamp_attrs.any?
              rescue => ts_err
                results << {{
                  id: wp.id,
                  status: 'timestamp_failed',
                  subject: wp.subject,
                  error: "timestamp update failed: #{{ts_err.class}}: #{{ts_err.message}}",
                  raw_created_at: wp_data['created_at'],
                  raw_updated_at: wp_data['updated_at']
                }}
                next
              end

              results << {{ id: wp.id, status: 'created', subject: wp.subject }}
            else
              failed_count += 1
              results << {{
                subject: wp_data['subject'],
                status: 'failed',
                errors: wp.errors.full_messages
              }}
            end

          rescue => e
            failed_count += 1
            results << {{
              subject: wp_data['subject'],
              status: 'failed',
              error: e.message
            }}
          end
        end

        {{
          created: created_count,
          failed: failed_count,
          results: results
        }}
        """

        try:
            result = client.execute_json_query(script)
            return result if isinstance(result, dict) else {"created": 0, "failed": len(work_packages), "results": []}
        except Exception as e:
            msg = f"Failed to batch create work packages: {e}"
            raise QueryExecutionError(msg) from e
