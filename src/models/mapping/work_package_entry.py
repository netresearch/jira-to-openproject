"""Pydantic v2 model for the work-package mapping entry (ADR-002 phase 3c).

Historically the ``work_package`` mapping persisted by
:mod:`src.application.components.work_package_migration` is a flat ``dict`` whose
values are *polymorphic*:

* the modern shape is a ``dict`` with at least ``jira_key`` and
  ``openproject_id``, plus optional ``openproject_project_id``,
  ``jira_migration_date`` and ``updated_at`` fields; or
* the legacy shape is a bare ``int`` — the OpenProject work-package id —
  produced by older migrations that did not yet record metadata.

Phase 3c closes the loop on this polymorphism by introducing a single,
typed :class:`WorkPackageMappingEntry` and a :meth:`from_legacy`
constructor that coerces both shapes into the canonical typed form.
Consumers can opt into the typed flow one site at a time; broader
adoption (and the matching :class:`MappingRepository` work) is staged
for phase 4 and phase 7 respectively.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from src.domain.ids import JiraIssueKey, OpProjectId, OpWorkPackageId


class WorkPackageMappingEntry(BaseModel):
    """Canonical typed representation of a single ``work_package`` mapping row.

    The two required fields (``jira_key`` and ``openproject_id``) are
    always present after construction. Optional fields mirror what
    :class:`~src.application.components.work_package_migration.WorkPackageMigration`
    stores today; see :meth:`from_legacy` for the coercion rules used
    when normalising persisted user data.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    jira_key: JiraIssueKey
    """Jira issue key, e.g. ``"PROJ-123"`` — the dict-level mapping key."""

    openproject_id: OpWorkPackageId
    """OpenProject ``work_packages.id`` primary key for this issue."""

    openproject_project_id: OpProjectId | None = None
    """OpenProject ``projects.id`` the work package belongs to (if known)."""

    jira_migration_date: str | None = None
    """ISO-8601 timestamp the entry was last migrated. Kept as ``str``."""

    updated_at: str | None = None
    """ISO-8601 ``updated_at`` last seen on the OpenProject side."""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkPackageMappingEntry:
        """Build an entry from an already-dict-shaped legacy payload."""
        return cls.model_validate(raw)

    @classmethod
    def from_legacy(cls, key: str, value: Any) -> WorkPackageMappingEntry:
        """Coerce either the dict or int legacy shape into a typed entry.

        Args:
            key: The Jira issue key, i.e. the dict-level mapping key.
                The legacy ``int`` shape stores the work-package id only,
                so the Jira key has to come from outside the value.
            value: The raw mapping value — either an ``int`` (legacy) or
                a ``dict`` (modern).

        Returns:
            A fully-validated :class:`WorkPackageMappingEntry`.

        Raises:
            ValueError: If ``value`` is neither ``int`` nor ``dict``,
                or if the dict shape is missing the required
                ``openproject_id`` field. We fail fast at the boundary
                so corrupt entries surface during normalisation rather
                than at consumption time.

        """
        # ``bool`` is an ``int`` subclass in Python, so we explicitly reject
        # boolean values to avoid silently treating ``True``/``False`` as ids.
        if isinstance(value, bool):
            msg = f"Unsupported wp_map entry shape for key {key!r}: bool"
            raise ValueError(msg)
        if isinstance(value, int):
            return cls(
                jira_key=JiraIssueKey(key),
                openproject_id=OpWorkPackageId(value),
            )
        if isinstance(value, dict):
            # Allow callers to omit ``jira_key`` from the value because it's
            # already implicit in the surrounding map. The outer mapping key
            # is the source of truth — if the inner dict has a conflicting
            # ``jira_key``, the outer wins (silent overwrite would let
            # data-corrupted upstream entries flow through unflagged, but
            # raising here would brick a normalisation script users run on
            # arbitrary historical data; outer-wins fixes the row in place).
            payload: dict[str, Any] = {**value, "jira_key": key}
            return cls.model_validate(payload)
        msg = f"Unsupported wp_map entry shape for key {key!r}: {type(value).__name__}"
        raise ValueError(msg)


__all__ = ["WorkPackageMappingEntry"]
