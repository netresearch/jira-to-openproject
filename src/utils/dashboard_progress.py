# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The single producer's durable dashboard snapshot, containing aggregate observations only."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator

RunStatus = Literal["idle", "running", "pending", "paused", "completed", "failed"]
PhaseStatus = Literal["pending", "running", "paused", "completed", "failed", "skipped"]
Label = Annotated[str, Field(min_length=1, max_length=256)]
Count = Annotated[StrictInt, Field(ge=0)]


class ProgressPhase(BaseModel):
    """One public phase label and its observed state."""

    model_config = ConfigDict(extra="forbid")
    name: Label
    status: PhaseStatus
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


class MigrationProgress(BaseModel):
    """The values a CLI or pilot explicitly published for its current or latest run."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    migration_id: Label
    status: RunStatus
    start_time: AwareDatetime | None = None
    last_update: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    current_component: Label | None = None
    phases: list[ProgressPhase] = Field(default_factory=list)
    verified_counts: dict[Label, Count] = Field(default_factory=dict)
    pending_checks: list[Label] = Field(default_factory=list)
    memory_usage_mb: float | None = Field(default=None, ge=0)
    total_entities: Count | None = None
    processed_entities: Count | None = None
    failed_entities: Count | None = None
    error_count: Count | None = None

    @model_validator(mode="after")
    def check_times(self) -> Self:
        """Require timestamped evidence for active and finished runs."""
        if self.status != "idle" and (self.start_time is None or self.last_update is None):
            msg = "A recorded run needs start_time and last_update"
            raise ValueError(msg)
        if self.start_time and self.last_update and self.last_update < self.start_time:
            msg = "last_update precedes start_time"
            raise ValueError(msg)
        if self.finished_at and self.start_time and self.finished_at < self.start_time:
            msg = "finished_at precedes start_time"
            raise ValueError(msg)
        return self

    def public_data(self) -> dict[str, Any]:
        """Return JSON-compatible observations, with elapsed time measured through the latest report."""
        end = self.finished_at or self.last_update
        elapsed = (end - self.start_time).total_seconds() if end and self.start_time else None
        return {
            **self.model_dump(mode="json"),
            "elapsed_seconds": elapsed,
            "success_rate": None,
            "estimated_time_remaining": None,
            "current_entity": None,
            "current_entity_type": None,
        }


def progress_file() -> Path | None:
    """Return the operator-selected file shared by the dashboard and one producer."""
    configured = os.getenv("J2O_DASHBOARD_PROGRESS_FILE")
    return Path(configured) if configured else None


def write_progress(snapshot: dict[str, Any], *, path: Path | str | None = None) -> None:
    """Atomically replace the latest complete snapshot. Without configuration publication is disabled."""
    target = Path(path) if path is not None else progress_file()
    if target is None:
        return
    payload = MigrationProgress.model_validate(snapshot).model_dump_json()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as staged:
            temporary_path = Path(staged.name)
            staged.write(payload)
            staged.flush()
            os.fsync(staged.fileno())
        temporary_path.replace(target)
        if os.name == "posix":
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_progress(target: Path) -> MigrationProgress:
    """Read one complete generation, or report idle before the producer's first publication."""
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return MigrationProgress(migration_id="none", status="idle")
    return MigrationProgress.model_validate_json(data)
