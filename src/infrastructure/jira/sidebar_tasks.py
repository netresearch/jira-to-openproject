# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Reconcile ordinary tasks with the Jira Software issue list without exporting embedded comment content."""

from typing import Any

_METADATA = ("ordinal", "mandatory", "createdDate", "lastChangedDate", "lastChangedBy", "lastChangedByName", "overdue")


def issue_task_records(raw: dict, descriptions: list[dict], rows: list[dict], total: int) -> list[dict]:
    """Return source-ordered records only for positively identified descriptions and direct sidebar tasks."""
    project = raw["fields"]["project"]
    if project["projectTypeKey"] != "software" or not isinstance(rows, list) or len(rows) != total:
        msg = "The Jira task list does not match the ordinary issue scope or source count."
        raise ValueError(msg)
    known = {task["id"]: task for task in descriptions}
    seen: set[str] = set()
    result = []
    for row in rows:
        _validate_identity(row, raw, project)
        sid = str(row["id"])
        if sid in seen:
            msg = "Duplicate Jira task ID in issue task list."
            raise ValueError(msg)
        seen.add(sid)
        if record := _ordinary_record(row, known.get(sid)):
            result.append(record)
    if not known.keys() <= seen:
        msg = "Description task missing from the source issue task list."
        raise ValueError(msg)
    # Older add-on versions omit ordinals. Keep the response order in that case.
    if result and all("ordinal" in item for item in result):
        result.sort(key=lambda item: item["ordinal"])
    return result


def _ordinary_record(row: dict, description: dict | None) -> dict | None:
    if row.get("commentId") is not None or row.get("visibility") is not None or row.get("private"):
        if description is not None:
            msg = "Jira task has conflicting issue and restricted origins."
            raise ValueError(msg)
        return None
    if description is not None:
        if row["manuallyCreated"] or description["title"] != row["text"] or description["checked"] != row["checked"]:
            msg = "Jira task list differs from the rendered description."
            raise ValueError(msg)
    elif not row["manuallyCreated"]:
        return None
    if ("ordinal" in row and type(row["ordinal"]) is not int) or (
        "mandatory" in row and not isinstance(row["mandatory"], bool)
    ):
        msg = "Invalid Jira task ordinal or mandatory flag."
        raise ValueError(msg)
    return {
        "id": str(row["id"]),
        "title": row["text"],
        "checked": row["checked"],
        "origin": "description" if description is not None else "sidebar",
        **{k: row[k] for k in _METADATA if k in row},
    }


def _validate_identity(row: dict[str, Any], raw: dict, project: dict) -> None:
    if (
        row["issue"] != raw["key"]
        or str(row["issueId"]) != str(raw["id"])
        or str(row["projectId"]) != str(project["id"])
    ):
        msg = "Jira task belongs to a different issue or project."
        raise ValueError(msg)
    if not (isinstance(row["checked"], bool) and isinstance(row["manuallyCreated"], bool)):
        msg = "Invalid task state or origin in the Jira task list."
        raise ValueError(msg)
    if not str(row["id"]).isascii() or not str(row["id"]).isdigit() or not isinstance(row["text"], str):
        msg = "Invalid ID or title in the Jira task list."
        raise ValueError(msg)
