# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Match OpenProject query rows to their original caller-supplied keys."""

from typing import Any


def index_batch_rows[K: int | str](
    rows: Any, keys: list[K], field: str, *, stringify: bool = False
) -> dict[K, dict[str, Any]]:
    """Keep matching rows and preserve the key types used by the caller."""
    requested = {str(key) if stringify else key: key for key in keys}
    if isinstance(rows, dict):
        rows = [rows]
    result: dict[K, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict) or field not in row:
            continue
        key = str(row[field]) if stringify else row[field]
        if isinstance(key, (int, str)) and key in requested:
            result[requested[key]] = row
    return result
