# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Map Jira component/version names to project-scoped OpenProject records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from src.config import logger
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.application.components.base_migration import BaseMigration

type RecordMap = dict[str, dict[str, int]]
type RecordModel = Literal["Category", "Version"]


def project_id(entry: Any) -> int | None:
    """Read both the legacy integer mapping and the current record shape."""
    if isinstance(entry, dict):
        try:
            return int(entry["openproject_id"])
        except KeyError, TypeError, ValueError, OverflowError:
            return None
    return entry if isinstance(entry, int) else None


def _existing_records(migration: BaseMigration, model: RecordModel, project_ids: set[int]) -> RecordMap | None:
    if not project_ids:
        return {}
    ids = ", ".join(map(str, sorted(project_ids)))
    query = f"{model}.where(project_id: [{ids}]).select(:id,:name,:project_id)"
    try:
        rows = migration.op_client.execute_json_query(query)
    except Exception:
        logger.exception("Failed to list existing %s records", model)
        return None
    if not isinstance(rows, list):
        return None
    indexed: RecordMap = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = str(int(row["project_id"]))
            record_id = int(row["id"])
        except KeyError, TypeError, ValueError, OverflowError:
            continue
        indexed.setdefault(pid, {})[str(row.get("name"))] = record_id
    return indexed


def map_project_records(migration: BaseMigration, extracted: ComponentResult, model: RecordModel) -> ComponentResult:
    """Reuse existing records, create missing names and refresh their assigned IDs."""
    stem = model.lower()
    data = extracted.data if isinstance(extracted.data, dict) else {}
    by_project = data.get("by_project", {})
    result = {f"{stem}_map": {}, "issues": data.get("issues", {})}
    if not by_project:
        return ComponentResult(success=True, created_types=0, data=result)
    projects = migration.mappings.get_mapping("project") or {}
    mapped_ids = {key: pid for key in by_project if (pid := project_id(projects.get(key)))}
    pids = set(mapped_ids.values())
    indexed = _existing_records(migration, model, pids) or {}
    missing = [
        {"name": name, "project_id": pid}
        for key, pid in mapped_ids.items()
        for name in by_project[key]
        if name not in indexed.get(str(pid), {})
    ]
    created = 0
    if missing:
        try:
            response = migration.op_client.bulk_create_records(model, missing)
            created = int(response.get("created_count", 0)) if isinstance(response, dict) else len(missing)
        except Exception:
            logger.exception("Failed to create %s records in bulk", model)
        refreshed = _existing_records(migration, model, pids)
        if refreshed is not None:
            indexed = refreshed
    try:
        migration.mappings.set_mapping(stem, indexed)
    except Exception:
        logger.exception("Failed to persist %s mapping", stem)
    result[f"{stem}_map"] = indexed
    return ComponentResult(success=True, created_types=created, data=result)


def apply_work_package_updates(migration: BaseMigration, updates: list[dict[str, Any]], failed: int) -> ComponentResult:
    """Apply an assignment batch, preserving failures from earlier parsing."""
    updated = 0
    if updates:
        try:
            response = migration.op_client.batch_update_work_packages(updates)
            updated = int(response.get("updated", 0)) if isinstance(response, dict) else len(updates)
            if isinstance(response, dict):
                failed += int(response.get("failed", 0))
        except Exception:
            logger.exception("Failed to update work package assignments")
            failed += len(updates)
    return ComponentResult(success=failed == 0, updated=updated, failed=failed)
