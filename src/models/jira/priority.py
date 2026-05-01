"""Pydantic v2 model for Jira priority payloads.

The migration consumes Jira priorities at one well-defined boundary:
:meth:`src.clients.jira_search_service.JiraSearchService.get_priorities`,
which currently returns a ``list[dict[str, Any]]`` of the form::

    [{"id": "1", "name": "Critical", "status": "Active"}, …]

This module gives that boundary a typed shape so the migration can stop
hand-rolling ``p.get("name")`` lookups and rely on attribute access.

The model also tolerates ``jira.Priority`` SDK instances via
:meth:`JiraPriority.from_jira_obj` to leave the door open for future
callers that bypass the dict-shaped service layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class JiraPriority(BaseModel):
    """Canonical representation of a Jira priority."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    """Numeric priority id (returned by Jira as a string)."""

    name: str | None = None
    """Display name, e.g. ``"Critical"``, ``"Normal"``."""

    status: str | None = None
    """Optional ``status`` flag returned by the Jira REST API.

    Some Jira deployments expose an ``ACTIVE``/``INACTIVE`` flag; we keep
    it around so the field is preserved through the typed pipeline.
    """

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JiraPriority:
        """Build a :class:`JiraPriority` from a Jira REST/JSON dict shape."""
        return cls.model_validate(raw)

    @classmethod
    def from_jira_obj(cls, obj: Any) -> JiraPriority:
        """Build a :class:`JiraPriority` from a ``jira.Priority`` SDK instance.

        The SDK exposes ``id``/``name``/``status`` via attribute access. We
        coerce ``id`` to ``str`` to match the dict-shape contract above.
        """
        raw_id = getattr(obj, "id", None)
        return cls.model_validate(
            {
                "id": None if raw_id is None else str(raw_id),
                "name": getattr(obj, "name", None),
                "status": getattr(obj, "status", None),
            },
        )


__all__ = ["JiraPriority"]
