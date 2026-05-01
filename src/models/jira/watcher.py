"""Pydantic v2 model for Jira issue watchers (ADR-002 phase 7c).

The :meth:`src.infrastructure.jira.jira_issue_service.JiraIssueService.get_issue_watchers`
method (established in phase 3l, see #148) returns a flat list of
``dict`` payloads keyed by ``name``/``accountId``/``displayName``/
``emailAddress``/``active``. The Jira SDK ``Watcher`` instance carries
the same attributes via attribute access.

:class:`JiraWatcher` normalises both shapes so the watcher migration can
use a single typed view at the boundary instead of per-call
``isinstance(w, dict)`` ladders.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JiraWatcher(BaseModel):
    """Canonical typed representation of a Jira issue watcher row."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str | None = None
    """Server/DC login (``name``) — the legacy username-based identifier."""

    account_id: str | None = Field(default=None, alias="accountId")
    """Cloud ``accountId`` for the watcher, when present."""

    display_name: str | None = Field(default=None, alias="displayName")
    """Human-readable display name."""

    email_address: str | None = Field(default=None, alias="emailAddress")
    """Watcher's email — opaque/redacted on Cloud."""

    active: bool = True
    """``True`` for active accounts; absent flag defaults to ``True``."""

    @classmethod
    def from_any(cls, raw: Any) -> JiraWatcher | None:
        """Build a :class:`JiraWatcher` from a dict or SDK-like object.

        Returns ``None`` when ``raw`` is ``None`` or carries no usable
        identifier — the watcher migration iterates and skips those
        rows so a missing watcher row never aborts the per-issue loop.
        """
        if raw is None:
            return None
        if isinstance(raw, dict):
            data = raw
        else:
            data = {
                "name": getattr(raw, "name", None),
                "accountId": getattr(raw, "accountId", None),
                "displayName": getattr(raw, "displayName", None),
                "emailAddress": getattr(raw, "emailAddress", None),
                "active": getattr(raw, "active", True),
            }
        instance = cls.model_validate(data)
        if not (instance.name or instance.account_id or instance.email_address or instance.display_name):
            return None
        return instance


__all__ = ["JiraWatcher"]
