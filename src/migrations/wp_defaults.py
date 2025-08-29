from typing import Any


def choose_default_type_id(op_client: Any) -> int:
    """Pick a default Type ID, preferring the first by position, else 1."""
    try:
        type_ids = op_client.execute_large_query_to_json_file(
            "Type.order(:position).pluck(:id)",
            timeout=30,
        )
        if isinstance(type_ids, list) and type_ids:
            return int(type_ids[0])
    except Exception:
        pass
    return 1


def apply_required_defaults(
    records: list[dict[str, Any]],
    *,
    project_id: int | None,
    op_client: Any,
    fallback_admin_user_id: int | str | None,
) -> None:
    """Fill in missing required fields on WorkPackage records.

    Sets type_id, status_id, priority_id, author_id if missing.
    """
    default_type_id = choose_default_type_id(op_client)

    default_status_id = 1
    try:
        status_ids = op_client.execute_large_query_to_json_file(
            "Status.order(:position).pluck(:id)",
            timeout=30,
        )
        if isinstance(status_ids, list) and status_ids:
            default_status_id = int(status_ids[0])
    except Exception:
        pass

    default_priority_id = None
    try:
        pr_ids = op_client.execute_large_query_to_json_file(
            "IssuePriority.order(:position).pluck(:id)",
            timeout=30,
        )
        if isinstance(pr_ids, list) and pr_ids:
            default_priority_id = int(pr_ids[0])
    except Exception:
        default_priority_id = None

    # Author can be provided as int or str (environment/CLI); normalize but preserve type-safety
    default_author_id: int | str | None = None
    if fallback_admin_user_id:
        try:
            default_author_id = int(fallback_admin_user_id)
        except Exception:
            # If int conversion fails (e.g., it's a username string), use as-is
            default_author_id = fallback_admin_user_id
    if not default_author_id:
        try:
            admin_ids = op_client.execute_large_query_to_json_file(
                "User.where(admin: true).limit(1).pluck(:id)",
                timeout=30,
            )
            if isinstance(admin_ids, list) and admin_ids:
                default_author_id = int(admin_ids[0])
        except Exception:
            default_author_id = None

    for wp in records:
        if not wp.get("type_id"):
            wp["type_id"] = default_type_id
        if not wp.get("status_id") and default_status_id:
            wp["status_id"] = default_status_id
        if not wp.get("author_id") and default_author_id:
            wp["author_id"] = default_author_id
        if not wp.get("priority_id") and default_priority_id:
            wp["priority_id"] = default_priority_id


