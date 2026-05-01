"""Jira infrastructure layer for the Jira to OpenProject migration.

Lazily exposes the common client class to avoid importing heavy dependencies at
package import time (helps tests run without optional libs like `jira`).
"""

__all__ = ["JiraClient"]


def __getattr__(name: str) -> object:  # pragma: no cover - simple lazy import shim
    if name == "JiraClient":
        from .jira_client import JiraClient as _JiraClient

        return _JiraClient
    raise AttributeError(name)
