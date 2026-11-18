# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""OpenProject status and work-package type catalog queries."""

from __future__ import annotations

from typing import Any

from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.openproject_client import OpenProjectClient


class OpenProjectStatusTypeService:
    """Status + work-package-type read helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    def get_statuses(self) -> list[dict[str, Any]]:
        """Read all statuses as delivered by the OpenProject model."""
        try:
            rows = self._client.rails_runner.read_catalog("Status.all.as_json", "get_statuses")
        except Exception as exc:
            msg = "Failed to get statuses."
            raise QueryExecutionError(msg) from exc
        return rows if isinstance(rows, list) else []

    def get_work_package_types(self) -> list[dict[str, Any]]:
        """Read only the type columns used for work-package mapping."""
        try:
            rows = self._client.rails_runner.read_catalog(
                "Type.select(:id, :name).map { |t| { id: t.id, name: t.name } }",
                "get_work_package_types",
            )
        except Exception as exc:
            msg = "Failed to get work package types."
            raise QueryExecutionError(msg) from exc
        return rows if isinstance(rows, list) else []
