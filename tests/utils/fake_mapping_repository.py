"""In-memory :class:`MappingRepository` for unit tests (ADR-002 phase 4a).

PR 4a introduces the :class:`src.domain.repositories.MappingRepository`
Protocol; PR 4b will rewire consumers to depend on it explicitly. To
make that migration tractable, tests need a lightweight implementation
they can pass into migration constructors instead of monkey-patching
``cfg.mappings``.

:class:`FakeMappingRepository` is that implementation: a single
``dict[str, dict[str, Any]]`` backing store with no I/O, no caching
quirks, and no atomicity story to worry about. It is intentionally
chatty about its semantics — :meth:`set` and :meth:`set_all` defensively
copy their inputs so test fixtures cannot accidentally share state with
the system under test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


class FakeMappingRepository:
    """In-memory ``MappingRepository`` for unit tests.

    The fake satisfies :class:`src.domain.repositories.MappingRepository`
    structurally — Protocol conformance can be asserted with
    ``isinstance(fake, MappingRepository)`` because the Protocol is
    declared ``@runtime_checkable``.
    """

    def __init__(
        self,
        initial: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Create a fake repository, optionally pre-populated.

        Args:
            initial: Optional name → payload mapping to seed the store.
                The initial mappings are deep-enough copied (one level
                of dict) so callers can mutate ``initial`` after the
                constructor without affecting the fake.

        """
        self._store: dict[str, dict[str, Any]] = {}
        if initial is not None:
            for name, payload in initial.items():
                self._store[name] = dict(payload)

    # ── Protocol surface ─────────────────────────────────────────────

    def get(self, name: str) -> dict[str, Any]:
        """Return the named mapping, or an empty dict if missing.

        Returns the live dict from the store (no defensive copy on
        read). Tests that need an isolated copy should invoke
        :func:`dict` on the result.
        """
        return self._store.get(name, {})

    def set(self, name: str, data: Mapping[str, Any]) -> None:
        """Store ``data`` under ``name``, replacing any existing payload.

        The payload is shallow-copied so the test fixture cannot mutate
        the stored value via the original reference.
        """
        self._store[name] = dict(data)

    def has(self, name: str) -> bool:
        """Whether a non-empty mapping is stored under ``name``."""
        return bool(self._store.get(name))

    def all_names(self) -> list[str]:
        """Sorted list of every name stored in the fake."""
        return sorted(self._store)

    # ── Test convenience ─────────────────────────────────────────────

    def set_all(self, mappings: Mapping[str, Mapping[str, Any]]) -> None:
        """Bulk-replace the entire store with ``mappings``.

        Existing entries not present in ``mappings`` are dropped; this
        is "replace", not "merge". Each payload is shallow-copied for
        the same isolation reason as :meth:`set`.
        """
        self._store = {name: dict(payload) for name, payload in mappings.items()}


__all__ = ["FakeMappingRepository"]
