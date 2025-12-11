"""Migrate Jira time estimates to OpenProject work packages.

Supports:
- timetracking.originalEstimate / remainingEstimate (Jira formatted strings)
- timeoriginalestimate / timeestimate (Jira integer seconds)

Maps to OpenProject attributes:
- estimated_hours (float hours)
- remaining_hours (float hours) [best-effort; may be ignored if field absent]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src.config import logger  # type: ignore
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)

from src import config

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

HOURS_PER_DAY = 8
DAYS_PER_WEEK = 5


@register_entity_types("estimates")
class EstimatesMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)

        self.mappings = config.mappings

    def _extract(self) -> ComponentResult:
        """Extract issues for which we have work package mappings."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        jira_keys = [str(k) for k in wp_map.keys()]
        if not jira_keys:
            return ComponentResult(success=True, extracted=0, data={"issues": {}})

        issues: dict[str, Any] = {}
        try:
            batch_get = getattr(self.jira_client, "batch_get_issues", None)
            if callable(batch_get):
                issues = batch_get(jira_keys)
        except Exception:
            logger.exception("Failed to batch-get Jira issues for estimates extraction")
            issues = {}

        return ComponentResult(success=True, extracted=len(issues), data={"issues": issues})

    @staticmethod
    def _parse_estimate_string_to_hours(value: str | None) -> float | None:
        if not value or not isinstance(value, str):
            return None
        # Accept patterns like "1w 2d 3h 30m"
        total_hours = 0.0
        token = ""
        for ch in value.strip():
            if ch.isdigit() or ch in {".", ","}:
                token += "." if ch == "," else ch
                continue
            # unit boundary
            if token:
                try:
                    num = float(token)
                except Exception:
                    num = 0.0
                if ch.lower() == "w":
                    total_hours += num * HOURS_PER_DAY * DAYS_PER_WEEK
                elif ch.lower() == "d":
                    total_hours += num * HOURS_PER_DAY
                elif ch.lower() == "h":
                    total_hours += num
                elif ch.lower() == "m":
                    total_hours += num / 60.0
                token = ""
            # ignore other separators (space etc.)
        # trailing number without unit: assume hours if present
        if token:
            try:
                total_hours += float(token)
            except Exception:
                pass
        return total_hours if total_hours > 0 else None

    @staticmethod
    def _seconds_to_hours(value: float | None) -> float | None:
        try:
            if value is None:
                return None
            return float(value) / 3600.0
        except Exception:
            return None

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        issues: dict[str, Any] = (extracted.data or {}).get("issues", {}) if extracted.data else {}
        if not issues:
            return ComponentResult(success=True, data={"updates": []})

        wp_map = self.mappings.get_mapping("work_package") or {}
        updates: list[dict[str, Any]] = []

        for key, issue in issues.items():
            try:
                wp_entry = wp_map.get(key)
                wp_id = None
                if isinstance(wp_entry, dict):
                    wp_id = wp_entry.get("openproject_id")
                elif isinstance(wp_entry, int):
                    wp_id = wp_entry
                if not wp_id:
                    continue

                fields = getattr(issue, "fields", None)
                if not fields:
                    continue
                # Prefer explicit seconds fields when available
                orig_sec = getattr(fields, "timeoriginalestimate", None)
                rem_sec = getattr(fields, "timeestimate", None)
                orig_hours = self._seconds_to_hours(orig_sec)
                rem_hours = self._seconds_to_hours(rem_sec)

                # Fall back to timetracking strings
                if orig_hours is None or rem_hours is None:
                    tt = getattr(fields, "timetracking", None)
                    if tt:
                        if orig_hours is None:
                            orig_hours = self._parse_estimate_string_to_hours(
                                getattr(tt, "originalEstimate", None),
                            )
                        if rem_hours is None:
                            rem_hours = self._parse_estimate_string_to_hours(
                                getattr(tt, "remainingEstimate", None),
                            )

                update_rec: dict[str, Any] = {"id": int(wp_id)}
                if orig_hours is not None:
                    update_rec["estimated_hours"] = float(orig_hours)
                if rem_hours is not None:
                    update_rec["remaining_hours"] = float(rem_hours)

                # Only enqueue if at least one field present
                if len(update_rec) > 1:
                    updates.append(update_rec)
            except Exception:  # noqa: BLE001
                continue

        return ComponentResult(success=True, data={"updates": updates}, mapped_fields_count=len(updates))

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        updates: list[dict[str, Any]] = (mapped.data or {}).get("updates", []) if mapped.data else []
        if not updates:
            return ComponentResult(success=True, updated=0)

        updated = 0
        failed = 0
        try:
            res = self.op_client.batch_update_work_packages(updates)
            updated = int(res.get("updated", 0)) if isinstance(res, dict) else 0
        except Exception:
            logger.exception("Failed to batch update estimates on work packages")
            failed = len(updates)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run estimates migration using ETL pattern."""
        logger.info("Starting estimates migration...")
        try:
            extracted = self._extract()
            if not extracted.success:
                return ComponentResult(
                    success=False,
                    message="Estimates extraction failed",
                    errors=extracted.errors or ["estimates extraction failed"],
                )

            mapped = self._map(extracted)
            if not mapped.success:
                return ComponentResult(
                    success=False,
                    message="Estimates mapping failed",
                    errors=mapped.errors or ["estimates mapping failed"],
                )

            result = self._load(mapped)
            logger.info(
                "Estimates migration completed: success=%s, updated=%s, failed=%s",
                result.success,
                result.updated,
                result.failed,
            )
            return result
        except Exception as e:
            logger.exception("Estimates migration failed")
            return ComponentResult(
                success=False,
                message=f"Estimates migration failed: {e}",
                errors=[str(e)],
            )
