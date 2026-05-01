"""Jira user account queries.

Phase 3d of ADR-002 continues the jira_client.py decomposition. The
user-related methods (user listing, user detail lookup, avatar
download, batch user lookup by key) move into a focused service.

The service is exposed on ``JiraClient`` as ``self.users`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like the other Phase 3 services this is HTTP-only — calls
go through the ``jira`` SDK or its session — so there is no
Ruby-script escaping to worry about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.infrastructure.jira.jira_client import (
    JiraApiError,
    JiraConnectionError,
)

if TYPE_CHECKING:
    from src.infrastructure.jira.jira_client import JiraClient


class JiraUserService:
    """User-domain queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_users(self) -> list[dict[str, Any]]:
        """Get all users from Jira.

        Returns:
            List of user dictionaries with key, name, display name, email, and active status

        Raises:
            JiraApiError: If the API request fails

        """
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Paginate through all users — the pre-extraction code did
            # a single ``search_users(..., maxResults=1000)`` call,
            # which silently truncated to the first 1000 on
            # instances with more users. Same pattern the agile
            # service uses for boards/sprints.
            users: list[Any] = []
            start_at = 0
            page_size = 1000
            while True:
                page = self._client.jira.search_users(
                    user=".",
                    includeInactive=True,
                    startAt=start_at,
                    maxResults=page_size,
                )
                if not page:
                    break
                users.extend(page)
                if len(page) < page_size:
                    break
                start_at += page_size

            self._logger.info("Retrieved %s users from Jira API", len(users))

            # Convert user objects to dictionaries with provenance metadata
            enriched_users = []
            for user in users:
                avatar_urls = getattr(user, "avatarUrls", None)
                if avatar_urls:
                    try:
                        avatar_urls = {str(k): str(v) for k, v in dict(avatar_urls).items() if v}
                    except Exception:
                        avatar_urls = None
                jira_user = {
                    "key": getattr(user, "key", None),
                    "name": getattr(user, "name", None),
                    "displayName": getattr(user, "displayName", None),
                    "emailAddress": getattr(user, "emailAddress", ""),
                    "active": getattr(user, "active", True),
                    "accountId": getattr(user, "accountId", None),
                    "timeZone": getattr(user, "timeZone", None) or getattr(user, "timezone", None),
                    "locale": getattr(user, "locale", None),
                    "self": getattr(user, "self", None),
                    "avatarUrls": avatar_urls,
                }
                enriched_users.append(jira_user)

            return enriched_users

        except Exception as e:
            error_msg = f"Failed to get users: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_user_info(self, user_key: str) -> dict[str, Any] | None:
        """Get information for a specific user by key.

        Args:
            user_key: The user key (account ID or username) to look up

        Returns:
            User dictionary with account ID, display name, email, and active status,
            or None if user not found

        Raises:
            JiraApiError: If the API request fails

        """
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            # Try to get the user by account ID first
            user = self._client.jira.user(user_key)

            if user:
                avatar_urls = getattr(user, "avatarUrls", None)
                if avatar_urls:
                    try:
                        avatar_urls = {str(k): str(v) for k, v in dict(avatar_urls).items() if v}
                    except Exception:
                        avatar_urls = None

                return {
                    "accountId": getattr(user, "accountId", None),
                    "displayName": getattr(user, "displayName", None),
                    "emailAddress": getattr(user, "emailAddress", ""),
                    "active": getattr(user, "active", True),
                    "key": getattr(user, "key", None),
                    "name": getattr(user, "name", None),
                    "timeZone": getattr(user, "timeZone", None) or getattr(user, "timezone", None),
                    "locale": getattr(user, "locale", None),
                    "avatarUrls": avatar_urls,
                }

            return None

        except Exception as e:
            # Log at debug level for not found cases, exception level for others
            if "404" in str(e) or "not found" in str(e).lower():
                self._logger.debug("User not found: %s", user_key)
                return None
            error_msg = f"Failed to get user info for {user_key}: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def download_user_avatar(self, avatar_url: str) -> tuple[bytes, str] | None:
        """Download a Jira user avatar and return (bytes, content_type)."""
        if not avatar_url:
            return None

        # Distinguish "no client" from "no session on the client" so
        # callers see consistent error messages with the rest of this
        # service. Pre-extraction code conflated the two via
        # ``getattr(None, '_session', None)``.
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        session = getattr(self._client.jira, "_session", None)
        if session is None:
            msg = "Jira session not initialized"
            raise JiraConnectionError(msg)

        try:
            response = session.get(avatar_url, stream=True, timeout=30)
            response.raise_for_status()
        except Exception as exc:
            self._logger.debug("Failed to download avatar %s: %s", avatar_url, exc)
            return None

        content_type = response.headers.get("Content-Type", "image/png")
        try:
            data = response.content
        finally:
            response.close()

        if not data:
            return None

        return data, content_type

    def batch_get_users_by_keys(self, user_keys: list[str]) -> dict[str, dict]:
        """Retrieve multiple users in batches."""
        if not user_keys:
            return {}

        # Get all users and filter to requested keys. Build the lookup
        # by walking the candidate identifiers in priority order
        # (``key`` → ``accountId`` → ``name``) and taking the first
        # *truthy* one. The previous shape
        # ``user.get("key", user.get("accountId", ""))`` returned
        # ``None`` whenever ``"key"`` was present with a ``None``
        # value, breaking lookups by accountId for accounts that
        # only carried an accountId.
        all_users = self.get_users()
        user_dict: dict[str, dict] = {}
        for user in all_users:
            identifier = user.get("key") or user.get("accountId") or user.get("name")
            if identifier:
                user_dict[str(identifier)] = user

        return {key: user_dict[key] for key in user_keys if key in user_dict}
