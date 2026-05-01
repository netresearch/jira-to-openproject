"""Persistence adapters for j2o (ADR-002 phase 4a).

Concrete implementations of the domain repository Protocols. Today this
package only ships :class:`JsonFileMappingRepository`; PR 4b will rewire
consumers to depend on the Protocol instead of the legacy
``cfg.mappings`` proxy, and a future phase may add a SQLite-backed
implementation alongside the JSON one.
"""

from __future__ import annotations

from src.infrastructure.persistence.mapping_repo import JsonFileMappingRepository

__all__ = ["JsonFileMappingRepository"]
