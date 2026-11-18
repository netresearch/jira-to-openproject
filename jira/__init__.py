# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT


from . import resources
from .resources import Issue

__all__ = ["JIRA", "Issue", "JIRAError", "resources"]


class JIRA:
    """SDK stand-in accepting constructor parameters used by offline tests."""

    def __init__(self, *args, **kwargs) -> None:
        """Accept the real client's constructor arguments without opening a connection."""


class JIRAError(Exception):
    """Base Jira error exposed by the offline SDK stand-in."""
