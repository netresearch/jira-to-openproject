# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Jira time estimates to OpenProject work packages.

Supports:
- timetracking.originalEstimate / remainingEstimate (Jira formatted strings)
- timeoriginalestimate / timeestimate (Jira integer seconds)

Maps to OpenProject attributes:
- estimated_hours (float hours)
- remaining_hours (float hours) [best-effort; may be ignored if field absent]
"""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import register_entity_types
from src.application.components.level_fields import LevelFieldsMigration
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry

HOURS_PER_DAY = 8
DAYS_PER_WEEK = 5


@register_entity_types("estimates")
class EstimatesMigration(LevelFieldsMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _extract(self) -> ComponentResult:
        """Extract issues for which we have work package mappings."""
        wp_map = self._work_package_map()
        jira_keys = self._jira_keys_from_wp_map(wp_map)
        if not jira_keys:
            return ComponentResult(success=True, extracted=0, data={"issues": {}})

        issues: dict[str, Any] = self._merge_batch_issues(jira_keys)

        return ComponentResult(success=True, extracted=len(issues), data={"issues": issues})

    @staticmethod
    def _parse_estimate_string_to_hours(value: str | None) -> float | None:
        if not value or not isinstance(value, str):
            return None
        units = {"w": HOURS_PER_DAY * DAYS_PER_WEEK, "d": HOURS_PER_DAY, "h": 1.0, "m": 1 / 60}
        total_hours = 0.0
        number = ""
        for char in value.strip():
            if char.isdigit() or char in ".,":
                number += char.replace(",", ".")
                continue
            total_hours += EstimatesMigration._number(number) * units.get(char.lower(), 0)
            number = ""
        total_hours += EstimatesMigration._number(number)
        return total_hours if total_hours > 0 else None

    @staticmethod
    def _number(token: str) -> float:
        try:
            return float(token)
        except ValueError:
            return 0.0

    @classmethod
    def _hours(cls, fields, seconds_field: str, display_field: str) -> float | None:
        seconds = getattr(fields, seconds_field, None)
        if seconds is not None:
            try:
                return float(seconds) / 3600
            except ValueError, TypeError:
                pass
        tracking = getattr(fields, "timetracking", None)
        return cls._parse_estimate_string_to_hours(getattr(tracking, display_field, None))

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        issues = (extracted.data or {}).get("issues", {})
        wp_map = self._work_package_map()
        updates = []
        for outer_key, raw in wp_map.items():
            key = self._inner_jira_key(outer_key, raw)
            fields = getattr(issues.get(key), "fields", None)
            if not fields:
                continue
            try:
                entry = WorkPackageMappingEntry.from_legacy(key, raw)
            except ValueError:
                continue
            values = {
                "estimated_hours": self._hours(fields, "timeoriginalestimate", "originalEstimate"),
                "remaining_hours": self._hours(fields, "timeestimate", "remainingEstimate"),
            }
            present = {name: value for name, value in values.items() if value is not None}
            if present:
                updates.append({"id": int(entry.openproject_id), **present})
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
            failed = max(len(updates) - updated, int((res or {}).get("failed", 0)))
        except Exception:
            logger.exception("Failed to batch update estimates on work packages")
            failed = len(updates)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Estimates migration, also for the level work packages (ADR-009)."""
        return self._with_level_fields(lambda: self._run_etl_pipeline("Estimates"))
