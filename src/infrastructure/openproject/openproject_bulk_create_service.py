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
  ``WorkPackage`` to apply Jira-key + provenance custom fields and
  optionally execute a journal-creation template loaded from
  ``src/ruby/create_work_package_journals.rb``.
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
import shlex
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import config
from src.infrastructure.exceptions import QueryExecutionError

if TYPE_CHECKING:
    from src.infrastructure.openproject.openproject_client import OpenProjectClient


class OpenProjectBulkCreateService:
    """Generic mass-create + WP-specific batch creation for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── generic mass-create ──────────────────────────────────────────────

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
        # Validate model name against allowlist to prevent injection
        client._validate_model_name(model)

        if not isinstance(records, list):
            _msg = "records must be a list of dicts"
            raise QueryExecutionError(_msg)

        # Prepare local JSON payload
        temp_dir = Path(client.file_manager.data_dir) / "bulk_create"
        temp_dir.mkdir(parents=True, exist_ok=True)
        local_json = temp_dir / f"{model.lower()}_bulk_{os.urandom(4).hex()}.json"
        try:
            with local_json.open("w", encoding="utf-8") as f:
                json.dump(records, f)
        except Exception as e:
            _msg = f"Failed to serialize records: {e}"
            raise QueryExecutionError(_msg) from e

        # Transfer JSON to container
        container_json = Path("/tmp") / local_json.name
        client.transfer_file_to_container(local_json, container_json)

        # BUG #32 FIX: Load journal creation .rb file content as template for WorkPackage migrations
        # This avoids Ruby scoping issues with the `load` statement
        journal_creation_ruby = ""
        if model == "WorkPackage":
            local_journal_rb = Path(__file__).parent.parent / "ruby" / "create_work_package_journals.rb"
            if local_journal_rb.exists():
                try:
                    with local_journal_rb.open("r", encoding="utf-8") as f:
                        # Read the .rb file content and prepare it for inline insertion
                        rb_content = f.read()
                        # Remove the header comments (first 9 lines) to avoid duplication
                        lines = rb_content.split("\n")
                        # Keep everything after line 9 (the actual Ruby code)
                        journal_creation_ruby = "\n".join(lines[9:])
                except Exception as e:
                    self._logger.warning(f"Failed to load journal creation template: {e}")
                    journal_creation_ruby = ""

        # Result file path in container and local debug path.
        # Always ensure uniqueness to avoid collisions across batches.
        # Sanitise ``result_basename``: it's caller-supplied and lands
        # both in filesystem paths AND in a Ruby single-quoted literal.
        # Reduce to a strict ``[A-Za-z0-9._-]`` basename so a
        # value like ``../etc/passwd`` or ``foo'; system('rm -rf /') #``
        # can't escape the temp dir or break out of the Ruby string.
        if result_basename:
            base = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(result_basename)).name)
            if not base:
                base = "bulk_result"
            if not base.endswith(".json"):
                base = f"{base}.json"
            unique_suffix = f"_{int(time.time())}_{os.getpid()}_{os.urandom(2).hex()}"
            # Insert suffix before .json
            if base.lower().endswith(".json"):
                result_name = base[:-5] + unique_suffix + ".json"
            else:
                result_name = base + unique_suffix
        else:
            result_name = f"bulk_result_{model.lower()}_{int(time.time())}_{os.getpid()}_{os.urandom(3).hex()}.json"
        container_result = Path("/tmp") / result_name
        local_result = temp_dir / result_name

        # Progress file within the container, mirrored locally for monitoring
        container_progress = Path("/tmp") / (result_name + ".progress")
        local_progress = local_result.with_suffix(local_result.suffix + ".progress")

        # Compose minimal Ruby script
        # Provenance hint for bulk create
        def _bulk_hint() -> str:
            try:
                ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                proj = (config.jira_config or {}).get("project_filter")
                proj_part = f" project={proj}" if proj else ""
                return f"j2o: migration/bulk_create model={model}{proj_part} ts={ts} pid={os.getpid()}"
            except Exception:
                return f"j2o: migration/bulk_create model={model} pid={os.getpid()}"

        header = (
            f"# {_bulk_hint()}\n"
            "require 'json'\n"
            "require 'logger'\n"
            "begin; require 'fileutils'; rescue; end\n"
            f"model_name = '{model}'\n"
            f"data_path = '{container_json.as_posix()}'\n"
            f"result_path = '{container_result.as_posix()}'\n"
            # Ensure progress ENV defaults are present in both console and runner modes
            f"ENV['J2O_BULK_PROGRESS_FILE'] ||= '{container_progress.as_posix()}'\n"
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
            "progress_file = ENV['J2O_BULK_PROGRESS_FILE']\n"
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
            "  # Debug: Inspect attrs hash for Bug #32\n"
            "  if idx == 0 && model_name == 'WorkPackage'\n"
            '    puts "[BUG32-DEBUG] attrs.class = #{attrs.class}"\n'
            '    puts "[BUG32-DEBUG] attrs.keys.count = #{attrs.keys.count}"\n'
            '    puts "[BUG32-DEBUG] attrs.keys = #{attrs.keys.inspect}"\n'
            "    puts \"[BUG32-DEBUG] attrs['_rails_operations'] present? #{!attrs['_rails_operations'].nil?}\"\n"
            '    puts "[BUG32-DEBUG] attrs[:_rails_operations] present? #{!attrs[:_rails_operations].nil?}"\n'
            "    if attrs['_rails_operations']\n"
            "      puts \"[BUG32-DEBUG] _rails_operations count = #{attrs['_rails_operations'].length}\"\n"
            "    end\n"
            "    STDOUT.flush\n"
            "  end\n"
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
            "    rails_ops = nil\n"
            "    jira_id = nil\n"
            "    jira_key = nil\n"
            "    jira_issue_key = nil\n"
            "    begin\n"
            "      cf_data = attrs.delete('custom_fields') if attrs.key?('custom_fields')\n"
            "      rails_ops = attrs.delete('_rails_operations') if attrs.key?('_rails_operations')\n"
            "      jira_id = attrs.delete('jira_id') if attrs.key?('jira_id')\n"
            "      jira_key = attrs.delete('jira_key') if attrs.key?('jira_key')\n"
            "      jira_issue_key = attrs.delete('jira_issue_key') if attrs.key?('jira_issue_key')\n"
            "    rescue\n"
            "    end\n"
            "    begin\n"
            "      rec.assign_attributes(attrs)\n"
            "    rescue => e\n"
            "      # If assign fails, proceed to save with preassigned associations only\n"
            "    end\n"
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
            "    if rec.save\n"
            "      # Apply ALL custom fields AFTER work package is saved (Jira key + J2O Origin fields)\n"
            "      if model_name == 'WorkPackage'\n"
            "        begin\n"
            "          cf_map = {}\n"
            "          # Add Jira Issue Key custom field if present (use extracted vars, not attrs)\n"
            "          key = jira_issue_key || jira_key\n"
            "          if key\n"
            "            begin\n"
            "              cf_jira = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Issue Key')\n"
            "              if !cf_jira\n"
            "                cf_jira = CustomField.new(name: 'Jira Issue Key', field_format: 'string',\n"
            "                  is_required: false, is_for_all: true, type: 'WorkPackageCustomField')\n"
            "                cf_jira.save\n"
            "              end\n"
            "              cf_map[cf_jira.id] = key if cf_jira && cf_jira.id\n"
            "            rescue\n"
            "            end\n"
            "          end\n"
            "          # Add J2O Origin custom fields\n"
            "          if cf_data && cf_data.respond_to?(:each)\n"
            "            cf_data.each do |cfh|\n"
            "              begin\n"
            "                cid = (cfh['id'] || cfh[:id]).to_i\n"
            "                val = cfh['value'] || cfh[:value]\n"
            "                next if cid <= 0 || val.nil?\n"
            "                cf_map[cid] = val\n"
            "              rescue; end\n"
            "            end\n"
            "          end\n"
            "          # Set all custom fields at once\n"
            "          if cf_map.any?\n"
            "            rec.custom_field_values = cf_map\n"
            "            rec.save\n"
            '            puts "J2O bulk item #{idx}: Set #{cf_map.size} custom fields" if verbose\n'
            "          end\n"
            "        rescue => e\n"
            '          puts "J2O bulk item #{idx}: CF assignment error: #{e.class}: #{e.message}" if verbose\n'
            "        end\n"
            "      end\n"
            "      # BUG #32 FIX: Journal creation logic loaded from template\n"
            + (
                "\n".join(f"      {line}" for line in journal_creation_ruby.split("\n"))
                if journal_creation_ruby
                else ""
            )
            + "\n"
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

        # Decide execution mode: prefer rails runner for long scripts to avoid pasting into console
        full_script = header + ruby
        max_lines_env = os.environ.get("J2O_SCRIPT_RUNNER_MAX_LINES")
        char_thresh_env = os.environ.get("J2O_SCRIPT_RUNNER_THRESHOLD")
        try:
            max_lines = int(max_lines_env) if max_lines_env else 10
        except Exception:
            max_lines = 10
        try:
            char_threshold = int(char_thresh_env) if char_thresh_env else 200
        except Exception:
            char_threshold = 200

        script_lines = full_script.count("\n") + 1
        use_runner = (script_lines >= max_lines) or (len(full_script) >= char_threshold)

        output: str | None = None
        if use_runner:
            runner_script_path = f"/tmp/j2o_bulk_{os.urandom(4).hex()}.rb"
            local_tmp = Path(client.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
            local_tmp.parent.mkdir(parents=True, exist_ok=True)
            with local_tmp.open("w", encoding="utf-8") as f:
                f.write(full_script)
            client.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))
            mode = (os.environ.get("J2O_SCRIPT_LOAD_MODE") or "runner").lower()
            allow_runner_fallback = str(os.environ.get("J2O_ALLOW_RUNNER_FALLBACK", "0")).lower() in {"1", "true"}
            if mode == "console":
                try:
                    _console_output = client.rails_client.execute(
                        f"load '{runner_script_path}'",
                        timeout=timeout or 120,
                        suppress_output=True,
                    )
                except Exception as e:
                    if not allow_runner_fallback:
                        msg = "Rails console execution failed and runner fallback is disabled"
                        raise QueryExecutionError(
                            msg,
                        ) from e
                    runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                    try:
                        stdout, stderr, rc = client.docker_client.execute_command(
                            runner_cmd,
                            timeout=timeout or 120,
                            env={
                                "J2O_BULK_RUBY_VERBOSE": os.environ.get("J2O_BULK_RUBY_VERBOSE", "1"),
                                "J2O_BULK_PROGRESS_FILE": container_progress.as_posix(),
                                "J2O_BULK_PROGRESS_N": os.environ.get("J2O_BULK_PROGRESS_N", "50"),
                            },
                        )
                    except subprocess.TimeoutExpired as te:
                        # Best-effort remote cleanup of the timed-out runner
                        try:
                            client.docker_client.execute_command(
                                f'pkill -f "rails runner {runner_script_path}" || true',
                                timeout=10,
                            )
                        except Exception:
                            pass
                        msg = f"rails runner timed out for {runner_script_path}"
                        raise QueryExecutionError(
                            msg,
                        ) from te
                    if rc != 0:
                        q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(q_msg) from e
                    if stdout:
                        self._logger.info("runner stdout: %s", stdout[:500])
            else:
                runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                try:
                    stdout, stderr, rc = client.docker_client.execute_command(
                        runner_cmd,
                        timeout=timeout or 120,
                        env={
                            "J2O_BULK_RUBY_VERBOSE": os.environ.get("J2O_BULK_RUBY_VERBOSE", "1"),
                            "J2O_BULK_PROGRESS_FILE": container_progress.as_posix(),
                            "J2O_BULK_PROGRESS_N": os.environ.get("J2O_BULK_PROGRESS_N", "50"),
                        },
                    )
                except subprocess.TimeoutExpired as te:
                    # Best-effort remote cleanup of the timed-out runner
                    try:
                        client.docker_client.execute_command(
                            f'pkill -f "rails runner {runner_script_path}" || true',
                            timeout=10,
                        )
                    except Exception:
                        pass
                    msg = f"rails runner timed out for {runner_script_path}"
                    raise QueryExecutionError(
                        msg,
                    ) from te
                if rc != 0:
                    q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                    raise QueryExecutionError(q_msg)
                if stdout:
                    self._logger.info("runner stdout: %s", stdout[:10000])
        else:
            # Execute via persistent Rails console with suppressed output (file-based result only)
            try:
                # Allow opt-in console progress visibility
                suppress = os.environ.get("J2O_BULK_PROGRESS_CONSOLE", "0") != "1"
                output = client.rails_client.execute(full_script, timeout=timeout or 120, suppress_output=suppress)
            except Exception as e:
                _msg = f"Rails execution failed for bulk_create_records: {e}"
                raise QueryExecutionError(_msg) from e

        # Poll-copy result back to local (allow slow writes on busy systems)
        max_wait_seconds_env = os.environ.get("J2O_BULK_RESULT_WAIT_SECONDS")
        try:
            max_wait_seconds = int(max_wait_seconds_env) if max_wait_seconds_env else 180
        except Exception:
            max_wait_seconds = 180
        poll_interval = 1.0
        waited = 0.0
        copied = False
        # Stall detection and heartbeats
        stall_env = os.environ.get("J2O_BULK_STALL_SECONDS")
        try:
            stall_seconds = int(stall_env) if stall_env else 120
        except Exception:
            stall_seconds = 120
        last_progress_len = -1
        last_progress_change_at = 0.0
        last_heartbeat_logged = -10.0
        runner_script_known = "runner_script_path" in locals()
        while waited < max_wait_seconds:
            # Avoid noisy SSH errors: first, check for existence using Docker API
            if client.docker_client.check_file_exists_in_container(container_result):
                # Attempt direct copy from container to local
                try:
                    client.transfer_file_from_container(container_result, local_result)
                    copied = True
                    break
                except FileNotFoundError:
                    # Race: file appeared in stat but not yet readable; keep polling
                    pass
                except Exception:
                    # Fall back to next poll iteration
                    pass

            # If an error sidecar file exists, fetch it for diagnostics
            try:
                err_remote = Path(container_result.as_posix() + ".error.json")
                if client.docker_client.check_file_exists_in_container(err_remote):
                    err_local = local_result.with_suffix(local_result.suffix + ".error.json")
                    client.transfer_file_from_container(err_remote, err_local)
                    try:
                        with err_local.open("r", encoding="utf-8") as ef:
                            err_txt = ef.read()[:500]
                        self._logger.error("Bulk runner error: %s", err_txt)
                    except Exception:
                        pass
            except Exception:
                pass

            # Probe progress file occasionally to provide live feedback and detect stalls
            try:
                if client.docker_client.check_file_exists_in_container(container_progress):
                    # Copy progress file locally at a modest cadence
                    if (waited - last_heartbeat_logged) >= 5.0:
                        try:
                            client.transfer_file_from_container(container_progress, local_progress)
                            prog_text = ""
                            try:
                                with local_progress.open("r", encoding="utf-8") as pf:
                                    prog_text = pf.read()
                            except Exception:
                                prog_text = ""
                            prog_len = len(prog_text)
                            # Count dots as a rough processed counter
                            processed_est = prog_text.count(".")
                            # Extract total from START line if present
                            total_est = None
                            try:
                                for line in prog_text.splitlines():
                                    if line.startswith("START total="):
                                        total_est = int(line.split("=", 1)[1])
                                        break
                            except Exception:
                                total_est = None
                            self._logger.info(
                                "Bulk progress: ~%s%s processed (waited %.0fs)",
                                processed_est,
                                f"/{total_est}" if total_est is not None else "",
                                waited,
                            )
                            if prog_len != last_progress_len:
                                last_progress_len = prog_len
                                last_progress_change_at = waited
                            elif (waited - last_progress_change_at) >= stall_seconds:
                                # Consider the run stalled; attempt to stop runner and error out
                                try:
                                    if runner_script_known:
                                        client.docker_client.execute_command(
                                            f'pkill -f "rails runner {runner_script_path}" || true',
                                            timeout=10,
                                        )
                                except Exception:
                                    pass
                                msg = f"bulk_create_records stalled for {stall_seconds}s without progress"
                                raise QueryExecutionError(
                                    msg,
                                )
                            last_heartbeat_logged = waited
                        except Exception:
                            # Ignore progress read errors; continue polling
                            pass
            except Exception:
                pass

            # Periodic heartbeat even without progress file
            try:
                if (waited - last_heartbeat_logged) >= 10.0:
                    self._logger.info(
                        "Waiting for bulk result file %s (waited %.0fs)",
                        container_result,
                        waited,
                    )
                    last_heartbeat_logged = waited
            except Exception:
                pass

            time.sleep(poll_interval)
            waited += poll_interval

        if not copied:
            _msg = "Result file not found after bulk_create_records execution"
            raise QueryExecutionError(_msg)

        # Parse and return result. Wrap in try/finally to clean up
        # container temp files on the happy path (the most common
        # exit point and where disk pressure accumulates). Failures
        # earlier in the method body — JSON serialisation,
        # ``transfer_file_to_container``, the Rails execution path —
        # still leak their respective temps, matching the
        # pre-extraction behaviour; an unhappy-path-cleanup pass can
        # land as a follow-up.
        try:
            try:
                with local_result.open("r", encoding="utf-8") as f:
                    result = json.load(f)
                    # Attach raw output snippet for callers that want to persist it
                    if isinstance(output, str):
                        result["output"] = output[:2000]
                    return result
            except Exception as e:
                _msg = f"Failed to parse result JSON: {e}"
                raise QueryExecutionError(_msg) from e
        finally:
            # Best-effort cleanup of container temp files. Failures
            # log at debug only so they don't mask the real result.
            for cpath in (container_json, container_result, container_progress):
                try:
                    client.docker_client.execute_command(
                        f"rm -f {shlex.quote(cpath.as_posix())}",
                    )
                except Exception as cleanup_err:
                    self._logger.debug(
                        "Non-critical: failed to remove container temp %s: %s",
                        cpath,
                        cleanup_err,
                    )

    # ── work-package batch wrappers ──────────────────────────────────────

    def batch_create_work_packages(
        self,
        work_packages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple work packages in batches for optimal performance."""
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
        if not work_packages:
            return {"created": 0, "failed": 0, "results": []}

        client = self._client

        # Write JSON to a temp file in container to avoid escaping issues
        batch_id = uuid.uuid4().hex[:8]
        container_json_path = f"/tmp/j2o_batch_{batch_id}.json"

        # Write JSON to local temp file, then transfer to container.
        # ``local_json_path`` is captured BEFORE ``json.dump`` so the
        # finally-block cleanup still works if dump raises (e.g. on
        # non-JSON-serialisable data) — otherwise ``unlink`` would
        # raise ``UnboundLocalError`` and mask the original error.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            local_json_path = f.name
            json.dump(work_packages, f)

        try:
            client.docker_client.transfer_file_to_container(Path(local_json_path), Path(container_json_path))
        finally:
            # Guard against ``FileNotFoundError`` so a missing temp
            # (e.g. if a future change ever moves the dump into a
            # try/except that deletes on failure) doesn't shadow the
            # real error.
            try:
                os.unlink(local_json_path)
            except FileNotFoundError:
                pass

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

            # Set status - using pre-fetched lookup
            if wp_data['status_id']
              wp.status = statuses_by_id[wp_data['status_id']]
            elsif wp_data['status_name']
              wp.status = statuses_by_name[wp_data['status_name']]
            end

            # Set priority - using pre-fetched lookup
            if wp_data['priority_id']
              wp.priority = priorities_by_id[wp_data['priority_id']]
            elsif wp_data['priority_name']
              wp.priority = priorities_by_name[wp_data['priority_name']]
            end

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

              # Set original timestamps if provided (using update_columns to bypass callbacks)
              timestamp_attrs = {{}}
              timestamp_attrs[:created_at] = Time.parse(wp_data['created_at']) if wp_data['created_at']
              timestamp_attrs[:updated_at] = Time.parse(wp_data['updated_at']) if wp_data['updated_at']
              wp.update_columns(timestamp_attrs) if timestamp_attrs.any?

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

        operation_succeeded = False  # Track success for debug file preservation
        try:
            result = client.execute_json_query(script)
            operation_succeeded = True
            return result if isinstance(result, dict) else {"created": 0, "failed": len(work_packages), "results": []}
        except Exception as e:
            msg = f"Failed to batch create work packages: {e}"
            raise QueryExecutionError(msg) from e
        finally:
            # Clean up container JSON file - preserve on error for debugging
            preserve_on_error = config.migration_config.get("preserve_debug_files_on_error", True)
            should_cleanup = operation_succeeded or not preserve_on_error
            if not should_cleanup:
                self._logger.warning(
                    "Preserving debug file due to error: %s (set preserve_debug_files_on_error=false to auto-cleanup)",
                    container_json_path,
                )
            else:
                try:
                    # ``shlex.quote`` is defence-in-depth here — the
                    # current ``container_json_path`` is hex-only via
                    # ``uuid.uuid4().hex[:8]``, but quoting matches
                    # the codebase's standard pattern for ``rm -f``
                    # commands routed through ``execute_command`` and
                    # protects against future changes to the path
                    # source.
                    client.docker_client.execute_command(
                        f"rm -f {shlex.quote(container_json_path)}",
                    )
                except Exception as cleanup_err:
                    self._logger.warning(
                        "Failed to cleanup container temp file %s: %s",
                        container_json_path,
                        cleanup_err,
                    )
