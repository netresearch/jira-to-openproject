# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Which list field options the content step needs before it writes (ADR-017).

The Rails writer in ``batch_update_work_packages`` resolves each value of a list field
to an option by label: a Hash gives ``item['value'] || item['name']``, any other value
gives itself, ``nil`` is skipped. The functions here derive the same labels, so the
content step can create the options that the rows will name. The writer stays strict.
"""

from __future__ import annotations

from typing import Any

# Sprint data belongs to the sprint and epic component, which writes it as text.
GH_SPRINT_CUSTOM_TYPE = "com.pyxis.greenhopper.jira:gh-sprint"
SPRINT_OWNER = "sprint_epic"

# The writer refuses these values on purpose; they need a mapping, not an option.
_REFUSED_CUSTOM_TYPES = frozenset(
    {"cascadingselect", "project", "version", "multiversion", "greenhopper-releasedmultiversionhistory"},
)
_SCALARS = (str, int, float, bool)


def is_sprint_field(entry: Any) -> bool:
    """Whether a custom field mapping entry describes the Jira Software sprint field."""
    return isinstance(entry, dict) and entry.get("jira_custom_type") == GH_SPRINT_CUSTOM_TYPE


def _refused_type(entry: dict[str, Any]) -> bool:
    custom = str(entry.get("jira_custom_type") or "")
    return custom.rpartition(":")[2] in _REFUSED_CUSTOM_TYPES


def option_labels(value: Any) -> list[Any]:
    """The labels the writer looks up for one row value, in order, without repeats.

    Labels stay raw scalars; the Ruby side converts them with ``to_s`` as the writer does.
    A cascading selection (a Hash with ``child``) and values without a usable label are skipped.
    """
    labels: list[Any] = []
    seen: set[tuple[type, Any]] = set()
    for item in value if isinstance(value, list) else [value]:
        label = item
        if isinstance(item, dict):
            # Ruby truthiness: only nil and false are false.
            if item.get("child") not in (None, False):
                continue
            label = item.get("value")
            if label is None or label is False:
                label = item.get("name")
        if label is None or not isinstance(label, _SCALARS) or label == "":
            continue
        # True == 1 in Python, but Ruby writes "true" and "1".
        if (type(label), label) not in seen:
            seen.add((type(label), label))
            labels.append(label)
    return labels


def list_fields(mapping: dict[str, Any]) -> dict[int, bool]:
    """OpenProject list field ids that take part, each with its "multi" flag.

    A target shared by several Jira fields takes part only when none of them is refused by
    the writer, and is "multi" when any of them is a Jira array.
    """
    fields: dict[int, bool] = {}
    refused: set[int] = set()
    for entry in mapping.values():
        if not isinstance(entry, dict) or entry.get("openproject_type") != "list" or is_sprint_field(entry):
            continue
        try:
            field_id = int(entry.get("openproject_id") or 0)
        except TypeError, ValueError:
            continue
        if field_id <= 0:
            continue
        if _refused_type(entry):
            refused.add(field_id)
        fields[field_id] = fields.get(field_id, False) or entry.get("jira_type") == "array"
    return {field_id: multi for field_id, multi in fields.items() if field_id not in refused}


def ensure_requests(rows: list[dict[str, Any]], fields: dict[int, bool]) -> dict[int, dict[str, Any]]:
    """One request per list field the rows write: its id, the labels and the "multi" flag."""
    requests: dict[int, dict[str, Any]] = {}
    for row in rows:
        for key, value in row.items():
            field_id = field_id_of(key)
            if field_id is None or field_id not in fields:
                continue
            request = requests.setdefault(field_id, {"id": field_id, "labels": [], "multi": fields[field_id]})
            known = {(type(label), label) for label in request["labels"]}
            request["labels"].extend(label for label in option_labels(value) if (type(label), label) not in known)
    return requests


def field_id_of(key: str) -> int | None:
    """The field id of a ``customFieldN`` row key, else None."""
    suffix = key.removeprefix("customField")
    return int(suffix) if suffix != key and suffix.isdigit() else None
