"""Infrastructure layer for j2o (ADR-002 phases 4+).

This package will host adapters that bridge the domain layer (see
:mod:`src.domain`) to concrete I/O concerns — filesystems, databases,
HTTP clients, and so on. Phase 4a starts the tree with the JSON-backed
mapping repository under :mod:`src.infrastructure.persistence`; later
phases will move the existing client/persistence modules into their
target subpackages.

The split between :mod:`src.domain` and :mod:`src.infrastructure` lets
us keep the Protocols framework-agnostic while concentrating I/O code
where it can be swapped wholesale (JSON → SQLite, in-process →
distributed cache, …) without touching domain consumers.
"""

from __future__ import annotations
