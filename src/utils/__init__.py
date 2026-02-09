"""Utility modules for Jira to OpenProject migration tool.

Delay heavy imports to avoid test-time dependency on optional libraries.
"""

__all__ = ["error_recovery"]


def __getattr__(name: str) -> object:  # pragma: no cover - lazy shim
    if name == "error_recovery":
        from .error_recovery import error_recovery as _er

        return _er
    raise AttributeError(name)
