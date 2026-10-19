# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Dashboard package for Jira to OpenProject migration tool."""

from .app import app, get_metrics, get_migration_status

__all__ = ["app", "get_metrics", "get_migration_status"]
