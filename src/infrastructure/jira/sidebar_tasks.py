# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Match direct Jira Software sidebar tasks to the ordinary per-issue Simple Tasklists response."""

from typing import Any


def sidebar_tasks(raw: dict, descriptions: list[dict], rows: list[dict], total: int) -> list[dict]:
    """Read only direct issue tasks. Embedded tasks from comments or other fields stay at their origin."""
    project = raw["fields"]["project"]
    if project["projectTypeKey"] != "software":
        msg = "Sidebar task audience is unknown for this project type."
        raise ValueError(msg)
    if not isinstance(rows, list) or len(rows) != total:
        msg = "The Jira task list and the source task count disagree."
        raise ValueError(msg)
    known = {t["id"]: t for t in descriptions}
    seen: set[str] = set()
    sidebar = []
    for row in rows:
        _validate_identity(row, raw, project)
        identifier = str(row["id"])
        if identifier in seen:
            msg = "Duplicate Jira task ID in issue task list."
            raise ValueError(msg)
        seen.add(identifier)
        if identifier in known:
            expected = known[identifier]
            if row["manuallyCreated"] or expected["title"] != row["text"] or expected["checked"] != row["checked"]:
                msg = "Jira task list differs from the rendered description."
                raise ValueError(msg)
        elif row["manuallyCreated"]:
            sidebar.append({"id": identifier, "checked": row["checked"], "title": row["text"]})
    if not known.keys() <= seen:
        msg = "Description task missing from the source issue task list."
        raise ValueError(msg)
    return sidebar


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
