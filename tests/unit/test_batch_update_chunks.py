# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The batch writer sends large row sets in chunks, one Rails call each."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from src.infrastructure.openproject.openproject_work_package_service import (
    BATCH_UPDATE_CHUNK_ROWS,
    BATCH_UPDATE_TIMEOUT_SECONDS,
    OpenProjectWorkPackageService,
)


class _Rails:
    """Answer each Rails call from the rows it carries; fail the calls listed in ``failing``."""

    def __init__(self, failing: tuple[int, ...] = ()) -> None:
        self.logger = logging.getLogger("test")
        self.calls: list[tuple[list[int], Any]] = []
        self.failing = failing

    def execute_json_query(self, script: str, timeout: int | None = None) -> dict[str, Any]:
        rows = json.loads(script.split("<<-'J2O_UPDATES'\n", 1)[1].split("\nJ2O_UPDATES", 1)[0])
        ids = [row["id"] for row in rows]
        self.calls.append((ids, timeout))
        if len(self.calls) in self.failing:
            msg = "End marker not observed within 90s"
            raise RuntimeError(msg)
        return {
            "updated": len(ids),
            "unchanged": 0,
            "target_changed": 0,
            "failed": 0,
            "results": [{"id": i, "status": "updated"} for i in ids],
        }


def _rows(count: int) -> list[dict[str, Any]]:
    return [{"id": i, "description": f"text {i}"} for i in range(1, count + 1)]


def test_large_row_set_travels_in_chunks_with_the_explicit_timeout() -> None:
    rails = _Rails()
    answer = OpenProjectWorkPackageService(rails).batch_update_work_packages(_rows(250))  # type: ignore[arg-type]

    assert [len(ids) for ids, _ in rails.calls] == [100, 100, 50]
    assert [ids[0] for ids, _ in rails.calls] == [1, 101, 201]
    assert {timeout for _, timeout in rails.calls} == {BATCH_UPDATE_TIMEOUT_SECONDS}
    assert answer["updated"] == 250
    assert [row["id"] for row in answer["results"]] == list(range(1, 251))


def test_a_failed_chunk_reports_its_rows_as_failed_and_the_others_still_run() -> None:
    rails = _Rails(failing=(2,))
    answer = OpenProjectWorkPackageService(rails).batch_update_work_packages(_rows(250))  # type: ignore[arg-type]

    assert len(rails.calls) == 3
    assert answer["updated"] == 150
    assert answer["failed"] == 100
    failed = [row for row in answer["results"] if row["status"] == "failed"]
    assert [row["id"] for row in failed] == list(range(101, 201))
    assert all("End marker not observed" in row["error"] for row in failed)


def test_rows_up_to_one_chunk_take_one_call() -> None:
    rails = _Rails()
    OpenProjectWorkPackageService(rails).batch_update_work_packages(_rows(BATCH_UPDATE_CHUNK_ROWS))  # type: ignore[arg-type]

    assert len(rails.calls) == 1


def test_no_rows_take_no_call() -> None:
    rails = _Rails()
    answer = OpenProjectWorkPackageService(rails).batch_update_work_packages([])  # type: ignore[arg-type]

    assert rails.calls == []
    assert answer["updated"] == 0
    assert answer["results"] == []


def test_an_answer_that_is_no_mapping_fails_only_its_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    rails = _Rails()
    answer_rows = rails.execute_json_query
    calls = []

    def answer(script: str, timeout: int | None = None) -> Any:
        calls.append(script)
        return "garbage" if len(calls) == 2 else answer_rows(script, timeout)

    monkeypatch.setattr(rails, "execute_json_query", answer)
    result = OpenProjectWorkPackageService(rails).batch_update_work_packages(_rows(250))  # type: ignore[arg-type]

    assert result["updated"] == 150
    assert result["failed"] == 100
    failed = [row for row in result["results"] if row["status"] == "failed"]
    assert [row["id"] for row in failed] == list(range(101, 201))
    assert all("Unexpected batch update answer: str" in row["error"] for row in failed)
