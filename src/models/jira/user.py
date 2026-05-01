"""Pydantic v2 model for Jira user payloads.

The migration receives Jira users in two shapes:

1. As a plain ``dict`` loaded from a cached JSON file or a REST response —
   the canonical Jira REST shape with camelCase keys (``accountId``,
   ``displayName``, ``timeZone``, …).
2. As a ``jira.User`` SDK object exposed via attribute access. The SDK is
   inconsistent about a few fields (notably ``timeZone`` vs ``timezone``,
   and ``avatarUrls`` may be a custom dict-like object).

:meth:`JiraUser.from_dict` and :meth:`JiraUser.from_jira_obj` normalise
both inputs into the same Pydantic instance. Field aliases let callers
construct instances using either snake_case (``account_id=…``) or the
original Jira camelCase (``accountId=…``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.domain.ids import JiraAccountId, JiraUserKey


class JiraUser(BaseModel):
    """Canonical representation of a Jira user account."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    key: JiraUserKey | None = None
    """Server/DC ``key`` field (legacy username-based identifier)."""

    account_id: JiraAccountId | None = Field(default=None, alias="accountId")
    """Cloud ``accountId`` — opaque, GDPR-stable user identifier."""

    name: str | None = None
    """Login/username (Server/DC). May be absent on Cloud instances."""

    display_name: str | None = Field(default=None, alias="displayName")
    """Human-readable display name."""

    email_address: str | None = Field(default=None, alias="emailAddress")
    """Primary email address. May be redacted on Cloud."""

    active: bool = True
    """``True`` for active accounts, ``False`` for deactivated ones."""

    time_zone: str | None = Field(default=None, alias="timeZone")
    """IANA timezone, e.g. ``"Europe/Berlin"``."""

    locale: str | None = None
    """Locale tag, e.g. ``"en_US"``."""

    self_url: str | None = Field(default=None, alias="self")
    """Canonical REST URL for this user (``/rest/api/2/user?...``)."""

    avatar_urls: dict[str, str] | None = Field(default=None, alias="avatarUrls")
    """Avatar URLs keyed by size, e.g. ``"48x48"``. ``None`` if unavailable."""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JiraUser:
        """Build a :class:`JiraUser` from a Jira REST/JSON dict shape."""
        return cls.model_validate(raw)

    @classmethod
    def from_jira_obj(cls, obj: Any) -> JiraUser:
        """Build a :class:`JiraUser` from a ``jira.User`` SDK instance.

        The SDK exposes its fields via attribute access. We normalise the
        ``timeZone``/``timezone`` discrepancy here, and tolerate a missing
        ``active`` flag (defaulting to ``True`` to match the SDK behaviour
        we observed in :mod:`src.clients.jira_user_service`).
        """
        avatar_urls_attr = getattr(obj, "avatarUrls", None)
        avatar_urls: dict[str, str] | None
        if avatar_urls_attr is None:
            avatar_urls = None
        else:
            try:
                avatar_urls = {str(k): str(v) for k, v in dict(avatar_urls_attr).items() if v} or None
            except TypeError, ValueError:
                avatar_urls = None

        return cls.model_validate(
            {
                "key": getattr(obj, "key", None),
                "accountId": getattr(obj, "accountId", None),
                "name": getattr(obj, "name", None),
                "displayName": getattr(obj, "displayName", None),
                "emailAddress": getattr(obj, "emailAddress", None),
                "active": getattr(obj, "active", True),
                # SDK quirk: some versions expose ``timezone`` lowercase.
                "timeZone": getattr(obj, "timeZone", None) or getattr(obj, "timezone", None),
                "locale": getattr(obj, "locale", None),
                "self": getattr(obj, "self", None),
                "avatarUrls": avatar_urls,
            },
        )


__all__ = ["JiraUser"]
