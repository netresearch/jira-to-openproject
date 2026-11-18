# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from typing import Any

# Timeout in seconds for Rails console queries fetching default IDs
_DEFAULT_TIMEOUT = 180


def _query_ids(op_client: Any, query: str) -> list[Any]:
    try:
        result = op_client.execute_large_query_to_json_file(query, timeout=_DEFAULT_TIMEOUT)
        return result if isinstance(result, list) else []
    except Exception:
        return []


def _first_id(ids: list[Any], fallback: int | None = None) -> int | None:
    try:
        return int(ids[0])
    except IndexError, TypeError, ValueError:
        return fallback


def choose_default_type_id(op_client: Any) -> int:
    """Pick the first available type by position."""
    type_id = _first_id(_query_ids(op_client, "Type.order(:position).pluck(:id)"))
    return 1 if type_id is None else type_id


def _default_author_id(op_client: Any, configured: int | str | None) -> int | str | None:
    if configured:
        try:
            author_id = int(configured)
        except TypeError, ValueError:
            return configured
        if author_id:
            return author_id
    return _first_id(_query_ids(op_client, "User.where(admin: true).limit(1).pluck(:id)"))


def work_package_defaults(
    op_client: Any,
    project_id: int | None,
    fallback_admin_user_id: int | str | None,
) -> tuple[dict[str, Any], list[Any]]:
    """Retrieve defaults once per batch, including the status list for legacy status validation."""
    type_id = choose_default_type_id(op_client)
    statuses = _query_ids(op_client, "Status.order(:position).pluck(:id)")
    priority_id = _first_id(_query_ids(op_client, "IssuePriority.order(:position).pluck(:id)"))
    return {
        "type_id": type_id,
        "status_id": _first_id(statuses, 1),
        "priority_id": priority_id,
        "author_id": _default_author_id(op_client, fallback_admin_user_id),
        "project_id": project_id,
    }, statuses


def fill_missing_fields(record: dict[str, Any], defaults: dict[str, Any]) -> None:
    """Fill unset fields with available target defaults."""
    for key, value in defaults.items():
        if value and not record.get(key):
            record[key] = value


def apply_required_defaults(
    records: list[dict[str, Any]],
    *,
    project_id: int | None,
    op_client: Any,
    fallback_admin_user_id: int | str | None,
) -> None:
    """Fill missing project, type, status, priority, and author IDs."""
    defaults, _ = work_package_defaults(op_client, project_id, fallback_admin_user_id)
    for record in records:
        fill_missing_fields(record, defaults)
