# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Select the Jira field changes whose readers match the issue's own audience.

Shared by the operator archive (``j2o-history``) and the
``work_package_field_history`` migration component, so both keep exactly the
same changes. Comment changes and any event or item that carries
audience/visibility metadata are dropped.
"""

from __future__ import annotations

import re
from collections import Counter

from src.infrastructure.jira.comment_audience import has_comment_audience, may_copy_history_item

_CORE_ADDITIONAL_FIELDS = {"WorklogId", "WorklogTimeSpent", "RemoteIssueLink"}
# A worklog can carry its own visibility; its history items stay in the operator archive only.
_WORKLOG_FIELDS = {"WorklogId", "WorklogTimeSpent"}
_SYSTEM_CUSTOM = "com.atlassian.jira.plugin.system.customfieldtypes:"
_AGILE_CUSTOM = "com.pyxis.greenhopper.jira:"
_SCHEMA_TYPES = {
    *(_SYSTEM_CUSTOM + kind for kind in ("textarea", "textfield", "select", "multicheckboxes", "multiselect", "float")),
    *(
        _AGILE_CUSTOM + kind
        for kind in ("gh-sprint", "gh-epic-link", "gh-epic-label", "gh-epic-status", "gh-epic-color", "gh-lexo-rank")
    ),
    "com.tempoplugin.tempo-accounts:accounts.customfield",
}
_FIELDS = ("field", "fieldId", "fieldtype", "from", "fromString", "to", "toString")
ISSUE_KEY = re.compile(r"[A-Z][A-Z\d_]*-\d+", re.ASCII)


def custom_fields(fields: list[dict], approved: set[str]) -> set[tuple[str, str | None]]:
    """Resolve unambiguous custom names and IDs from Jira's field catalogue."""
    names = Counter(field["name"] for field in fields)
    result: set[tuple[str, str | None]] = set()
    for field in fields:
        name, fid = field["name"], field["id"]
        schema = field.get("schema", {}).get("custom", "")
        if schema.startswith(_SYSTEM_CUSTOM) and fid not in approved:
            continue
        if re.fullmatch(r"customfield_\d+", fid, re.ASCII) and schema in _SCHEMA_TYPES:
            result.add((name, fid))
            if names[name] == 1:
                result.add((name, None))
    return result


def _synthetic_reference(item: dict) -> bool:
    if item.get("fieldId") or item["field"] not in {"Epic Child", "Parent Issue"}:
        return False
    for side in ("from", "to"):
        value, label = item.get(side), item.get(side + "String")
        if value is None and label is None:
            continue
        if not isinstance(label, str) or not ISSUE_KEY.fullmatch(label):
            return False
        if not isinstance(value, str) or not ((value.isascii() and value.isdecimal()) or ISSUE_KEY.fullmatch(value)):
            return False
    return item.get("from") is not None or item.get("to") is not None


def _keep(item: dict, history: dict, fields: set[tuple[str, str | None]], *, project_visible: bool = False) -> bool:
    if project_visible and item.get("field") in _WORKLOG_FIELDS:
        return False
    if "raw" in item or has_comment_audience(history) or has_comment_audience(item):
        return False
    if any(value is not None and type(value) not in (str, int) for value in (item.get(k) for k in _FIELDS)):
        return False
    name, fid = item.get("field"), item.get("fieldId")
    if not isinstance(name, str):
        return False
    if item.get("fieldtype") == "jira":
        if name.startswith("customfield_") or (fid and str(fid).casefold() != name.casefold()):
            return False
        return may_copy_history_item(item, history) or name in _CORE_ADDITIONAL_FIELDS
    if item.get("fieldtype") != "custom" or not may_copy_history_item(item, history):
        return False
    return (name, fid) in fields or _synthetic_reference(item)


def history_event(history: dict, fields: set[tuple[str, str | None]], *, project_visible: bool = False) -> dict:
    """Return one changelog event with only its kept items, each with its original position.

    ``project_visible`` selects for a value every project member reads: worklog items are dropped.
    """
    values = [
        {"position": index, **{k: item[k] for k in _FIELDS if k in item}}
        for index, item in enumerate(history["items"])
        if _keep(item, history, fields, project_visible=project_visible)
    ]
    return {
        "id": str(history["id"]),
        "created": history["created"],
        "author": (history.get("author") or {}).get("key"),
        "changes": values,
    }


def kept_events(
    histories: list[dict], fields: set[tuple[str, str | None]], *, project_visible: bool = False
) -> list[dict]:
    """Return the events of a changelog that keep at least one change."""
    events = [history_event(h, fields, project_visible=project_visible) for h in histories]
    return [event for event in events if event["changes"]]


def is_complete_page(page: dict) -> bool:
    """Accept only a first changelog page that holds every history exactly once.

    Raises ``KeyError`` for a page without ``startAt``, ``total`` or ``histories``.
    """
    histories = page["histories"]
    return (
        type(page["startAt"]) is int
        and type(page["total"]) is int
        and isinstance(histories, list)
        and page.get("isLast") is not False
        and page["startAt"] == 0
        and len(histories) == page["total"]
        and len({str(h["id"]) for h in histories}) == len(histories)
    )
