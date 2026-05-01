"""OpenProject infrastructure layer for the Jira to OpenProject migration.

Lazily exposes the common client class to avoid importing heavy dependencies at
package import time.
"""

__all__ = ["OpenProjectClient"]


def __getattr__(name: str) -> object:  # pragma: no cover - simple lazy import shim
    if name == "OpenProjectClient":
        from .openproject_client import OpenProjectClient as _OpenProjectClient

        return _OpenProjectClient
    raise AttributeError(name)
