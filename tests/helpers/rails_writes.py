# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Rails answers to the guarded writes of ADR-013, for test doubles."""

from __future__ import annotations

from typing import Any

GUARD = "_j2o_guard"
DIGEST = "d" * 64


def answer(rows: list[dict[str, Any]], status: str = "updated") -> dict[str, Any]:
    """Rails answering each guarded row of a batch with ``status`` (and a digest unless refused)."""
    results = [
        {"id": row.get("id", row.get("work_package_id")), "attr": row[GUARD]["attr"], "status": status}
        | ({} if status == "target_changed" else {"digest": DIGEST})
        for row in rows
        if GUARD in row
    ]
    plain = sum(GUARD not in row for row in rows)
    return {"updated": plain + (len(results) if status == "updated" else 0), "failed": 0, "results": results}


def unguarded(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows as the component built them, without the guard."""
    return [{key: value for key, value in row.items() if key != GUARD} for row in rows]


def plain_calls(method_calls: list[Any]) -> list[tuple[str, tuple, dict]]:
    """Mock calls with the guard taken out of every row list, for comparing two runs.

    The guard of a repeated run carries the digest the first run recorded.
    """

    def plain(value: Any) -> Any:
        if isinstance(value, list) and all(isinstance(row, dict) for row in value):
            return unguarded(value)
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key not in {GUARD, "guard"}}
        return value

    return [(name, tuple(plain(arg) for arg in args), kwargs) for name, args, kwargs in method_calls]


def sent(mock_method: Any) -> list[list[dict[str, Any]]]:
    """The unguarded rows of every call of a mocked writer."""
    return [unguarded(call.args[0]) for call in mock_method.call_args_list]
