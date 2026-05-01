"""Mapping-side Pydantic models and legacy mapping helpers (ADR-002 phase 3).

``JiraToOPMapping`` is intentionally NOT re-exported here. Its module
(``src.models.mapping.jira_to_op``) calls ``configure_logging(...)`` at
import time, which uses ``logging.basicConfig(force=True)`` and would
clobber global logging configuration whenever ``src.models`` is
imported. Callers who need the legacy class can import it explicitly
via ``from src.models.mapping.jira_to_op import JiraToOPMapping``.
"""

from __future__ import annotations

from src.models.mapping.work_package_entry import WorkPackageMappingEntry

__all__ = [
    "WorkPackageMappingEntry",
]
