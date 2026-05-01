"""Pydantic v2 model for Jira project payloads.

The shape modelled here matches the dict produced by
``JiraProjectService.get_projects`` (see :mod:`src.infrastructure.jira.jira_project_service`).
That dict is the one persisted to the JSON cache *and* the one consumed by
the migration layer, so it is the right boundary for our Pydantic model.

For SDK ingestion (a ``jira.Project`` instance) the relevant attributes
mirror the dict keys, with the addition that ``project.raw`` exposes the
underlying REST payload from which we can read ``projectCategory``,
``lead`` and ``avatarUrls`` directly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.domain.ids import JiraProjectKey


class JiraComponentLead(BaseModel):
    """Reference to the user who leads a Jira project component.

    The Jira REST shape is ``{"name": str, "key": str, "displayName": str,
    "emailAddress": str, "accountId": str}``. We model only the identifier
    fields the migration uses for user-mapping lookup.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str | None = None
    """Server/DC login (``name``)."""

    key: str | None = None
    """Server/DC user key — sometimes used in place of ``name``."""

    account_id: str | None = Field(default=None, alias="accountId")
    """Cloud ``accountId`` for the lead, when present."""

    email_address: str | None = Field(default=None, alias="emailAddress")
    """Lead's email — used as a last-resort key for user-mapping lookup."""

    display_name: str | None = Field(default=None, alias="displayName")

    @classmethod
    def from_any(cls, raw: Any) -> JiraComponentLead | None:
        """Build from a dict, SDK-like object, plain string, or ``None``.

        Returns ``None`` when ``raw`` is ``None`` or carries no usable
        identifier — callers iterate components and skip those.

        Plain strings are treated as a ``name`` hint to preserve
        compatibility with legacy lead values produced by older
        migration cache formats.
        """
        if raw is None:
            return None
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None
            return cls.model_validate({"name": stripped})
        if isinstance(raw, dict):
            data = raw
        else:
            data = {
                "name": getattr(raw, "name", None),
                "key": getattr(raw, "key", None),
                "accountId": getattr(raw, "accountId", None),
                "emailAddress": getattr(raw, "emailAddress", None),
                "displayName": getattr(raw, "displayName", None),
            }
        instance = cls.model_validate(data)
        # Honor the docstring's "no usable identifier" contract — return
        # None rather than an empty lead so callers can skip cleanly.
        if not (
            instance.name or instance.key or instance.account_id or instance.email_address or instance.display_name
        ):
            return None
        return instance


class JiraProjectComponent(BaseModel):
    """A Jira project component as returned by ``get_project_components``.

    The migration uses this only to look up the component's display
    ``name`` and its (optional) ``lead`` user. Everything else the REST
    shape carries (``id``, ``description``, assigneeType, …) is ignored
    by ``extra="ignore"``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    name: str | None = None
    lead: JiraComponentLead | None = None
    """Direct ``lead`` field — Cloud REST shape."""
    component_lead: JiraComponentLead | None = Field(default=None, alias="componentLead")
    """Server/DC alias for the same data."""

    @classmethod
    def from_any(cls, raw: Any) -> JiraProjectComponent:
        """Build from either a dict or an SDK-like object."""
        if isinstance(raw, dict):
            payload = dict(raw)
            # The REST shape stores the lead block as a dict — coerce it.
            for lead_key in ("lead", "componentLead"):
                if lead_key in payload:
                    payload[lead_key] = JiraComponentLead.from_any(payload[lead_key])
            return cls.model_validate(payload)
        return cls.model_validate(
            {
                "id": getattr(raw, "id", None),
                "name": getattr(raw, "name", None),
                "lead": JiraComponentLead.from_any(getattr(raw, "lead", None)),
                "componentLead": JiraComponentLead.from_any(getattr(raw, "componentLead", None)),
            },
        )

    @property
    def effective_lead(self) -> JiraComponentLead | None:
        """Return ``lead`` if set, else ``componentLead``."""
        return self.lead or self.component_lead


class JiraProjectCategoryRef(BaseModel):
    """Lightweight reference for a project category (id + name)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    name: str | None = None


class JiraProject(BaseModel):
    """Canonical representation of a Jira project."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    key: JiraProjectKey
    """Project key (the prefix in an issue key)."""

    name: str
    """Display name of the project."""

    id: str
    """Numeric project id (returned by Jira as a string)."""

    description: str = ""
    """Project description. Empty string when absent (matches the cache shape)."""

    lead: str | None = None
    """Lead user *login* (Server/DC ``name``/``key``). ``None`` if not set."""

    lead_display: str | None = None
    """Lead user display name. ``None`` if not set."""

    browse_url: str | None = None
    """``{base_url}/browse/{key}`` URL — convenient for issue links."""

    archived: bool = False
    """``True`` if the project has been archived in Jira."""

    project_type_key: str | None = None
    """Jira project type, e.g. ``"software"``, ``"business"``."""

    project_category: JiraProjectCategoryRef | None = None
    """Project category reference, if the project belongs to one."""

    avatar_url: str | None = None
    """Preferred avatar URL (largest available size)."""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JiraProject:
        """Build a :class:`JiraProject` from the cached JSON dict shape."""
        # The cache stores ``project_category`` as either a dict ``{"id":..,
        # "name":..}`` or an empty mapping. Normalise to ``None`` when empty
        # so consumers don't have to test both ``is None`` and ``== {}``.
        normalised = dict(raw)
        category = normalised.get("project_category")
        if isinstance(category, dict) and not category.get("id") and not category.get("name"):
            normalised["project_category"] = None
        return cls.model_validate(normalised)

    @classmethod
    def from_jira_obj(cls, obj: Any, *, browse_url: str | None = None) -> JiraProject:
        """Build a :class:`JiraProject` from a ``jira.Project`` SDK instance.

        ``browse_url`` is supplied by the caller because the SDK does not
        carry the base URL on the project object — that lives on the
        client. Pass ``None`` when the URL is not needed.
        """
        raw_detail: dict[str, Any] = getattr(obj, "raw", {}) or {}

        category_raw = raw_detail.get("projectCategory") or {}
        if not isinstance(category_raw, dict):
            category_raw = {}
        category: JiraProjectCategoryRef | None
        if category_raw.get("id") or category_raw.get("name"):
            category = JiraProjectCategoryRef.model_validate(
                {
                    "id": str(category_raw.get("id")) if category_raw.get("id") else None,
                    "name": str(category_raw.get("name")) if category_raw.get("name") else None,
                },
            )
        else:
            category = None

        lead_info = raw_detail.get("lead") or {}
        if not isinstance(lead_info, dict):
            lead_info = {}
        lead_login = lead_info.get("name") or lead_info.get("key")
        lead_display = lead_info.get("displayName")

        avatar_urls = raw_detail.get("avatarUrls") or {}
        if not isinstance(avatar_urls, dict):
            avatar_urls = {}
        preferred_avatar_url: str | None = None
        for size_key in ("128x128", "64x64", "48x48", "32x32", "24x24", "16x16"):
            candidate = avatar_urls.get(size_key)
            if candidate:
                preferred_avatar_url = str(candidate)
                break

        description = raw_detail.get("description") or ""

        return cls.model_validate(
            {
                "key": getattr(obj, "key", None),
                "name": getattr(obj, "name", None),
                "id": getattr(obj, "id", None),
                "description": description,
                "lead": lead_login,
                "lead_display": lead_display,
                "browse_url": browse_url,
                "archived": bool(raw_detail.get("archived", False)),
                "project_type_key": (raw_detail.get("projectTypeKey") or getattr(obj, "projectTypeKey", None)),
                "project_category": category,
                "avatar_url": preferred_avatar_url,
            },
        )


__all__ = [
    "JiraComponentLead",
    "JiraProject",
    "JiraProjectCategoryRef",
    "JiraProjectComponent",
]
