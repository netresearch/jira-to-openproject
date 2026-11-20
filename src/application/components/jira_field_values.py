# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Helpers for raw Jira custom field values."""

from __future__ import annotations

from typing import Any

# Jira Server's option id for a select list set to "None".
_NONE_OPTION_ID = "-1"


def without_empty_values(value: Any) -> Any:
    """Return ``value`` without Jira's "no value" placeholders, or ``None`` if nothing is left.

    Jira Server reports a select list set to "None" as the option
    ``{"id": "-1", "value": ""}``; an empty string or list carries no value
    either. A placeholder that has a cascading ``child`` is kept.
    """
    if isinstance(value, list):
        items = [item for item in value if without_empty_values(item) is not None]
        return items or None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict) and not value.get("child"):
        if str(value.get("id")) == _NONE_OPTION_ID or ("value" in value and value["value"] in (None, "")):
            return None
    return value
