# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Public interface for external processes that publish dashboard observations."""

from src.utils.dashboard_progress import MigrationProgress, progress_file, read_progress, write_progress

__all__ = ["MigrationProgress", "progress_file", "read_progress", "write_progress"]
