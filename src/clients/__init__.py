"""API clients package for the Jira to OpenProject migration.

Lazily expose common client classes to avoid importing heavy dependencies at
package import time (helps tests run without optional libs like `jira`).
"""

__all__ = ["JiraClient", "OpenProjectClient"]


def __getattr__(name: str) -> object:  # pragma: no cover - simple lazy import shim
    if name == "JiraClient":
        from .jira_client import JiraClient as _JiraClient  # noqa: PLC0415

        return _JiraClient
    if name == "OpenProjectClient":
        from .openproject_client import OpenProjectClient as _OpenProjectClient  # noqa: PLC0415

        return _OpenProjectClient
    raise AttributeError(name)
