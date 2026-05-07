"""Backfill assignee + provenance CFs on existing OP work packages.

Two distinct gaps the regular work-package migration cannot close on
its own once a WP is already in OP:

* **Bug A**: ``WorkPackage.assigned_to_id`` was left ``NULL`` on the
  ~4075 WPs created before PR #175 fixed the Jira→OP user resolution
  in the create payload. Audit shows 0.2% assignee coverage on NRS
  even after a full re-run because ``work_package_migration`` only
  sets ``assigned_to_id`` on the *create* path; existing WPs whose
  user-mapping miss happened in an earlier run never get re-set.

* **Bug D2 backfill**: ``_build_provenance_custom_field_entries`` (PR
  #178) populates the 8 provenance CFs only at create-time. Existing
  WPs have ``populated=0`` for every CF except the bootstrapped
  ``J2O Origin Key``. The audit reports them as
  ``WP CF '<name>' under-populated``.

This component iterates the persisted ``work_package_mapping``, fetches
each issue from Jira in batches, and emits a single Rails script per
batch that conditionally sets the missing fields. Conditional update
keeps the operation idempotent — a WP that already has a non-null
``assigned_to_id`` (or already-populated CF) is left untouched and
counted as ``skipped``.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.work_package_skeleton_migration import (
    _build_provenance_custom_field_entries,
)
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult


@register_entity_types("wp_metadata_backfill")
class WpMetadataBackfillMigration(BaseMigration):
    """Phase: backfill assignee + provenance CFs on existing WPs."""

    BATCH_SIZE = 50

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        msg = (
            "WpMetadataBackfillMigration is a transformation-only migration"
            " and does not support idempotent workflow. It operates on the"
            " persisted work_package mapping and fetches Jira data per-batch."
        )
        raise ValueError(msg)

    def _resolve_user_id(self, user_obj: Any) -> int | None:
        """Probe the user mapping using the canonical multi-identifier order.

        Mirrors :class:`AttachmentProvenanceMigration._resolve_user_id` and
        :class:`WatcherMigration._resolve_user_id`: ``account_id`` →
        ``name`` → ``key`` → ``email_address`` → ``display_name``. Cloud
        instances key on ``account_id``; Server/DC on ``name``. Returns
        ``None`` if no probe matches the ``user_mapping`` — caller treats
        that as ``user_unmapped`` and skips the assignee field.
        """
        if user_obj is None:
            return None
        umap = self.mappings.get_mapping("user") or {}

        def _read(attr: str) -> Any:
            if isinstance(user_obj, dict):
                return user_obj.get(attr)
            return getattr(user_obj, attr, None)

        for key in ("accountId", "name", "key", "emailAddress", "displayName"):
            v = _read(key)
            if not isinstance(v, str):
                continue
            rec = umap.get(v)
            if isinstance(rec, dict):
                op_id = rec.get("openproject_id")
                if isinstance(op_id, int):
                    return op_id
            if isinstance(rec, int):
                return rec
        return None

    def _get_provenance_cf_ids(self) -> dict[str, int]:
        """Resolve all 8 WP provenance CF ids via ``ensure_custom_field``.

        Identical contract to
        :meth:`WorkPackageSkeletonMigration._get_provenance_cf_ids` —
        kept duplicated here rather than imported because the skeleton
        method is bound to its instance and would require constructing
        a skeleton migration just to read CF ids.
        """
        string_cfs = (
            "J2O Origin Key",
            "J2O Origin ID",
            "J2O Origin System",
            "J2O Origin URL",
            "J2O Project Key",
            "J2O Project ID",
        )
        date_cfs = ("J2O First Migration Date", "J2O Last Update Date")
        ids: dict[str, int] = {}
        for name in string_cfs:
            try:
                cf = self.op_client.ensure_custom_field(name=name, field_format="string")
                if cf and cf.get("id"):
                    ids[name] = int(cf["id"])
            except Exception as exc:
                self.logger.warning("Failed to fetch provenance CF '%s': %s", name, exc)
        for name in date_cfs:
            try:
                cf = self.op_client.ensure_custom_field(name=name, field_format="date")
                if cf and cf.get("id"):
                    ids[name] = int(cf["id"])
            except Exception as exc:
                self.logger.warning("Failed to fetch provenance CF '%s': %s", name, exc)
        return ids

    def _jira_base_url(self) -> str:
        try:
            jira_cfg = getattr(config, "jira_config", None) or {}
            return str(jira_cfg.get("url") or "")
        except Exception:
            return ""

    @staticmethod
    def _rails_script() -> str:
        """Rails script that conditionally backfills assignee + CFs.

        Idempotency rules:

        * ``assigned_to_id`` is set only when the OP value is currently
          ``NULL`` AND the payload provides a non-null id.
        * Each CF is set only when the existing :class:`CustomValue`
          row is blank AND the payload provides a non-blank value.

        Output contract: the script MUST print the JSON payload wrapped
        between ``$j2o_start_marker`` and ``$j2o_end_marker`` so
        :meth:`OpenProjectRailsRunner.execute_script_with_data` can
        parse it into the envelope's ``data`` field. Without those
        markers the runner returns ``status="error"`` and the caller
        can't read the counters even though the Ruby work itself ran.
        Caught by PR #201 review (copilot-pull-request-reviewer).

        Returned counters: ``wp_missing``, ``updated_assignee``,
        ``updated_cf``, ``skipped``, ``failed``.
        """
        return (
            "require 'json'\n"
            "start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'\n"
            "end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'\n"
            "recs = input_data\n"
            "stats = {'wp_missing' => 0, 'updated_assignee' => 0, 'updated_cf' => 0,"
            " 'skipped' => 0, 'failed' => 0}\n"
            "recs.each do |r|\n"
            "  begin\n"
            "    wp = WorkPackage.find_by(id: r['work_package_id'])\n"
            "    unless wp\n"
            "      stats['wp_missing'] += 1\n"
            "      next\n"
            "    end\n"
            "    changed = false\n"
            "    if r['assigned_to_id'] && wp.assigned_to_id.nil?\n"
            "      wp.assigned_to_id = r['assigned_to_id']\n"
            "      stats['updated_assignee'] += 1\n"
            "      changed = true\n"
            "    end\n"
            "    (r['custom_fields'] || []).each do |cf|\n"
            "      cv = wp.custom_values.find_or_initialize_by(custom_field_id: cf['id'])\n"
            "      if cv.value.blank? && !cf['value'].to_s.empty?\n"
            "        cv.value = cf['value']\n"
            "        cv.save!\n"
            "        stats['updated_cf'] += 1\n"
            "        changed = true\n"
            "      end\n"
            "    end\n"
            "    if changed\n"
            "      wp.save! if wp.changed?\n"
            "    else\n"
            "      stats['skipped'] += 1\n"
            "    end\n"
            "  rescue => e\n"
            "    stats['failed'] += 1\n"
            "  end\n"
            "end\n"
            "puts start_marker\n"
            "puts stats.to_json\n"
            "puts end_marker\n"
        )

    def _build_record(
        self,
        wp_id: int,
        jira_issue: Any,
        cf_ids: dict[str, int],
        jira_base_url: str,
        today_iso: str,
    ) -> dict[str, Any] | None:
        """Build a single update record for the Rails batch.

        Returns ``None`` when neither the assignee nor any CF has a
        value — there's nothing to send and counting it as ``skipped``
        in the orchestrator avoids a useless Rails call.
        """
        assigned_to_id: int | None = None
        try:
            assignee = getattr(jira_issue.fields, "assignee", None)
        except AttributeError:
            assignee = None
        if assignee is not None:
            assigned_to_id = self._resolve_user_id(assignee)

        cf_entries = _build_provenance_custom_field_entries(
            jira_issue,
            cf_ids,
            jira_base_url=jira_base_url,
            today_iso=today_iso,
        )

        if assigned_to_id is None and not cf_entries:
            return None

        return {
            "work_package_id": int(wp_id),
            "assigned_to_id": assigned_to_id,
            "custom_fields": cf_entries,
        }

    def run(self) -> ComponentResult:  # type: ignore[override]
        self.logger.info("Starting WP metadata backfill (assignee + provenance CFs)")

        wp_map = self.mappings.get_mapping("work_package") or {}
        if not wp_map:
            msg = (
                "No work_package mapping available — backfill cannot run."
                " Run work_packages_skeleton first (or verify the mapping"
                " persisted)."
            )
            self.logger.error(msg)
            return ComponentResult(
                success=False,
                message=msg,
                errors=["missing_work_package_mapping"],
            )

        # Build (wp_id, jira_key) records from the mapping. Skip legacy
        # bare-int rows — they have no recoverable Jira key, same
        # filter pattern as ``AttachmentsMigration._wp_lookup_by_jira_key``.
        records: list[tuple[int, str]] = []
        for outer_key, raw in wp_map.items():
            if not isinstance(raw, dict):
                continue
            jira_key = raw.get("jira_key") or outer_key
            wp_id = raw.get("openproject_id")
            if not (jira_key and wp_id):
                continue
            try:
                records.append((int(wp_id), str(jira_key)))
            except TypeError, ValueError:
                continue

        if not records:
            msg = (
                f"work_package mapping present ({len(wp_map)} entries) but"
                " contains no usable rows for backfill (no entry has a"
                " recoverable Jira key — likely all legacy bare-int entries)."
            )
            self.logger.error(msg)
            return ComponentResult(
                success=False,
                message=msg,
                errors=["missing_work_package_mapping"],
            )

        cf_ids = self._get_provenance_cf_ids()
        jira_base_url = self._jira_base_url()
        today_iso = datetime.now(UTC).date().isoformat()
        self.logger.info(
            "Resolved %d provenance CF ids; jira_base_url=%r",
            len(cf_ids),
            jira_base_url,
        )

        rails_script = self._rails_script()
        totals: Counter[str] = Counter()
        skip_reasons: Counter[str] = Counter()

        for i in range(0, len(records), self.BATCH_SIZE):
            batch = records[i : i + self.BATCH_SIZE]
            jira_keys = [k for _, k in batch]
            try:
                issues = self._merge_batch_issues(jira_keys)
            except Exception:
                self.logger.exception(
                    "Failed to batch-fetch Jira issues for backfill batch %d",
                    i // self.BATCH_SIZE,
                )
                skip_reasons["jira_batch_failed"] += len(batch)
                continue

            payload: list[dict[str, Any]] = []
            for wp_id, jira_key in batch:
                issue = issues.get(jira_key)
                if issue is None:
                    skip_reasons["jira_issue_missing"] += 1
                    continue
                rec = self._build_record(wp_id, issue, cf_ids, jira_base_url, today_iso)
                if rec is None:
                    skip_reasons["nothing_to_update"] += 1
                    continue
                payload.append(rec)

            if not payload:
                continue

            try:
                envelope = self.op_client.execute_script_with_data(rails_script, payload)
            except Exception:
                self.logger.exception(
                    "Rails backfill failed for batch %d (%d records)",
                    i // self.BATCH_SIZE,
                    len(payload),
                )
                skip_reasons["rails_call_failed"] += len(payload)
                continue

            # ``execute_script_with_data`` returns an envelope:
            # ``{status, message, data, output}``. The actual counters
            # live under ``data`` (the parsed JSON the Ruby script
            # printed between ``$j2o_start_marker`` / ``$j2o_end_marker``).
            # If markers were missing → ``status="error"`` and ``data``
            # is absent; treat as a Rails-side failure for this batch
            # so the operator sees it instead of a silent ``updated=0``.
            # Caught by PR #201 review (copilot-pull-request-reviewer).
            if not isinstance(envelope, dict):
                skip_reasons["rails_envelope_malformed"] += len(payload)
                continue
            status = envelope.get("status")
            if status != "success":
                self.logger.warning(
                    "Rails backfill batch %d returned status=%r message=%r",
                    i // self.BATCH_SIZE,
                    status,
                    envelope.get("message"),
                )
                skip_reasons["rails_status_not_success"] += len(payload)
                continue
            data = envelope.get("data") or {}
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, int):
                        totals[k] += v

        updated = totals.get("updated_assignee", 0) + totals.get("updated_cf", 0)
        failed = totals.get("failed", 0)
        msg = (
            f"WP metadata backfill: assignee_updates={totals.get('updated_assignee', 0)},"
            f" cf_updates={totals.get('updated_cf', 0)},"
            f" skipped={totals.get('skipped', 0)},"
            f" wp_missing={totals.get('wp_missing', 0)},"
            f" failed={failed}"
        )
        self.logger.info(msg)

        return ComponentResult(
            success=failed == 0,
            updated=updated,
            failed=failed,
            message=msg,
            details={
                **dict(totals),
                "skip_reasons": dict(skip_reasons),
                "records_examined": len(records),
            },
        )
