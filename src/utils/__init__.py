"""Utility modules for Jira to OpenProject migration tool.

Delay heavy imports to avoid test-time dependency on optional libraries.
"""

__all__ = ["ErrorRecoverySystem", "error_recovery"]


def __getattr__(name: str):  # pragma: no cover - lazy shim
    if name in {"ErrorRecoverySystem", "error_recovery"}:
        from .error_recovery import ErrorRecoverySystem as _ERS, error_recovery as _er

        return _ERS if name == "ErrorRecoverySystem" else _er
    raise AttributeError(name)
