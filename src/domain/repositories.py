"""Domain-layer repository Protocols (ADR-002 phase 4a).

Phase 4a of ADR-002 introduces a storage abstraction for migration mappings.
Today every consumer of mapping state reaches into the global ``cfg.mappings``
proxy and calls instance methods on :class:`src.mappings.mappings.Mappings`.
That coupling makes:

* future storage swaps (JSON file → SQLite, S3, …) require touching every
  call site;
* unit tests rely on ``monkeypatch.setattr(cfg, "mappings", DummyMappings(),
  raising=False)`` instead of explicit dependency injection;
* the public surface of ``Mappings`` (13 methods, 256 LOC) impossible to
  reason about in isolation from the persistence concern.

The :class:`MappingRepository` Protocol pins down the *minimum* contract a
backing store must satisfy. Higher-level convenience helpers (``get_op_user_id``,
``get_op_project_id``, …) are deliberately **not** part of this Protocol —
they belong on a future ``MappingsService`` that *uses* a repository.
Keeping the Protocol thin makes the JSON-vs-SQLite swap a one-class
implementation, not a 13-method rewrite.

Adoption is gradual: this PR ships only the Protocol, a JSON-backed
adapter, and a Fake for tests. PR 4b rewires consumers and deprecates
``cfg.mappings``.

The Protocol is :func:`typing.runtime_checkable` so tests can assert
conformance with ``isinstance(repo, MappingRepository)`` without forcing
implementations to subclass an ABC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping


@runtime_checkable
class MappingRepository(Protocol):
    """Storage abstraction for migration mappings.

    A ``MappingRepository`` is keyed by a *mapping name* (a stable string
    identifier such as ``"user_mapping"`` or ``"project_mapping"``) and
    stores an opaque ``dict[str, Any]`` payload per name. The structure of
    each payload is defined by the migration that owns it, not by the
    repository — different mappings have different shapes (project mapping
    is keyed by Jira project key, work-package mapping by issue key, etc.)
    so the Protocol stays at the lowest common denominator: a JSON-style
    dict.

    Implementations are free to back the store with files, a database, or
    in-memory state. They MUST NOT raise on missing-name lookups; they
    return an empty dict instead so callers can branch on truthiness
    without try/except boilerplate.
    """

    def get(self, name: str) -> dict[str, Any]:
        """Return the named mapping, or an empty dict if it is missing.

        The returned dict is the live payload; callers that intend to
        mutate it locally without persisting should copy it explicitly.
        """

    def set(self, name: str, data: Mapping[str, Any]) -> None:
        """Persist ``data`` under ``name``, overwriting any existing payload.

        Implementations should treat the write as atomic relative to other
        readers — concurrent ``get`` calls must observe either the old or
        the new payload, never a partially-written one.
        """

    def has(self, name: str) -> bool:
        """Whether a non-empty mapping is stored under ``name``.

        Returns ``False`` for both "no record exists" and "record exists
        but is an empty dict". Callers wanting to distinguish those cases
        should use :meth:`all_names` instead.
        """

    def all_names(self) -> list[str]:
        """List every mapping name currently known to the repository."""


__all__ = ["MappingRepository"]
