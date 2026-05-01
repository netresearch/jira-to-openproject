"""Branded identifier types for the j2o domain layer.

Phase 3a of ADR-002 introduces a thin nominal type layer over the primitive
``str`` and ``int`` ids that flow between the Jira and OpenProject sides of
the migration. The goal is *zero runtime cost* combined with *type-checker
enforcement* — mypy will refuse to assign an ``OpProjectId`` where an
``OpUserId`` is expected, even though both are ``int`` at runtime.

All branded ids in this module are declared with :func:`typing.NewType`,
which gives us:

* No runtime overhead — ``OpUserId(42)`` is literally ``42`` after the
  call returns; the wrapper exists only for the type checker.
* Distinct types — mypy treats ``OpUserId`` and ``OpProjectId`` as
  different types even though they share the same underlying ``int``.
* Implicit upcast — a ``NewType`` value is still accepted everywhere
  the base type is accepted, so existing call sites that take ``int``
  continue to work without any changes.

Adoption is intentionally gradual: phase 3a only *introduces* these
aliases. Subsequent phases (3-gamma and beyond) will tighten signatures
one module at a time so reviewers can land each change in isolation.
"""

from __future__ import annotations

from typing import NewType

# ── Jira identifiers ─────────────────────────────────────────────────────
# Jira identifiers are strings. ``JiraIssueKey`` is the human-readable
# ``ABC-123`` form; ``JiraProjectKey`` is the project slug (``ABC``);
# ``JiraUserKey`` is the legacy username/key field; ``JiraAccountId`` is
# the cloud-only opaque GDPR-compatible id.

JiraIssueKey = NewType("JiraIssueKey", str)
"""Human-readable issue key, e.g. ``"ABC-123"``."""

JiraProjectKey = NewType("JiraProjectKey", str)
"""Project key (the prefix in an issue key), e.g. ``"ABC"``."""

JiraUserKey = NewType("JiraUserKey", str)
"""Server/Data-Center user ``key`` field (legacy username-based)."""

JiraAccountId = NewType("JiraAccountId", str)
"""Cloud ``accountId`` — opaque, GDPR-stable user identifier."""


# ── OpenProject identifiers ──────────────────────────────────────────────
# OpenProject ids are integers. We brand the most common ones so that the
# Jira→OP mapping layer cannot silently swap them at the call site.

OpUserId = NewType("OpUserId", int)
"""OpenProject ``users.id`` primary key."""

OpProjectId = NewType("OpProjectId", int)
"""OpenProject ``projects.id`` primary key."""

OpWorkPackageId = NewType("OpWorkPackageId", int)
"""OpenProject ``work_packages.id`` primary key."""

OpCustomFieldId = NewType("OpCustomFieldId", int)
"""OpenProject ``custom_fields.id`` primary key."""

OpStatusId = NewType("OpStatusId", int)
"""OpenProject ``statuses.id`` primary key (work-package status)."""

OpPriorityId = NewType("OpPriorityId", int)
"""OpenProject ``enumerations.id`` primary key for priorities."""

OpTypeId = NewType("OpTypeId", int)
"""OpenProject ``types.id`` primary key (work-package type)."""


__all__ = [
    "JiraAccountId",
    "JiraIssueKey",
    "JiraProjectKey",
    "JiraUserKey",
    "OpCustomFieldId",
    "OpPriorityId",
    "OpProjectId",
    "OpStatusId",
    "OpTypeId",
    "OpUserId",
    "OpWorkPackageId",
]
