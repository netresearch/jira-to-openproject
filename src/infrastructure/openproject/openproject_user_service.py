"""User-related operations against the OpenProject Rails console.

Phase 2j of ADR-002 continues the openproject_client.py god-class
decomposition by collecting all user-facing helpers onto a focused
service. The service owns:

* **Reads**: ``get_users`` (cached list of all users with provenance
  CFs), ``get_user`` (id/email/login lookup), ``get_user_by_email``
  (email index lookup with cache fall-through), ``batch_get_users_by_ids``
  (filtered slice of the cached full list), and
  ``batch_get_users_by_emails`` (paged ActiveRecord query with the
  shared idempotency decorator).
* **Avatars**: ``ensure_local_avatars_enabled`` (toggles the
  ``openproject_avatars`` plugin setting) and ``set_user_avatar`` (uploads
  an avatar via ``Avatars::UpdateService``).

Caches (``_users_cache``, ``_users_cache_time``, ``_users_by_email_cache``)
deliberately stay on ``OpenProjectClient`` — other client paths
(``find_record``, project/role helpers) hit the same cache, so moving it
would require either circular references or duplicate state. The service
reads/writes through ``self._client.<cache>``.

``OpenProjectClient`` exposes the service via ``self.users`` and keeps
thin delegators for the same method names so existing call sites work
unchanged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from src.infrastructure.exceptions import (
    QueryExecutionError,
    RecordNotFoundError,
)
from src.utils.idempotency_decorators import batch_idempotent

if TYPE_CHECKING:
    from pathlib import Path

    from src.infrastructure.openproject.openproject_client import OpenProjectClient


# Cache TTL: 5 minutes. Single definition (was previously duplicated on
# the client until Phase 2j; the client copy was unused after the move).
USERS_CACHE_TTL_SECONDS = 300


class OpenProjectUserService:
    """User-related Rails-console helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_users(self) -> list[dict[str, Any]]:
        """Get all users from OpenProject.

        Uses caching to avoid repeated Rails console queries.

        Returns:
            List of OpenProject users

        Raises:
            QueryExecutionError: If unable to retrieve users

        """
        client = self._client
        # Check cache first (5 minutes validity)
        current_time = time.time()
        cache_valid = (
            hasattr(client, "_users_cache")
            and hasattr(client, "_users_cache_time")
            and client._users_cache is not None
            and client._users_cache_time is not None
            and current_time - client._users_cache_time < USERS_CACHE_TTL_SECONDS
        )

        if cache_valid:
            self._logger.debug("Using cached users data (%d users)", len(client._users_cache))
            return client._users_cache

        try:
            # Route through centralized helper for uniform behavior
            # Include the 'mail' attribute and the J2O provenance custom fields if present
            file_path = client._generate_unique_temp_filename("users")
            ruby_query = (
                "cf_origin_system = CustomField.find_by(type: 'UserCustomField', name: 'J2O Origin System'); "
                "cf_origin_id = CustomField.find_by(type: 'UserCustomField', name: 'J2O User ID'); "
                "cf_origin_key = CustomField.find_by(type: 'UserCustomField', name: 'J2O User Key'); "
                "cf_origin_url = CustomField.find_by(type: 'UserCustomField', name: 'J2O External URL'); "
                "User.all.map do |u|\n"
                "  next unless u.is_a?(::User)\n"
                "  data = u.as_json\n"
                "  data['mail'] = u.mail\n"
                "  data['j2o_origin_system'] = (cf_origin_system ? u.custom_value_for(cf_origin_system)&.value : nil)\n"
                "  data['j2o_user_id'] = (cf_origin_id ? u.custom_value_for(cf_origin_id)&.value : nil)\n"
                "  data['j2o_user_key'] = (cf_origin_key ? u.custom_value_for(cf_origin_key)&.value : nil)\n"
                "  data['j2o_external_url'] = (cf_origin_url ? u.custom_value_for(cf_origin_url)&.value : nil)\n"
                "  pref = (u.respond_to?(:pref) ? u.pref : nil)\n"
                "  data['time_zone'] = (pref ? pref.time_zone : nil)\n"
                "  if pref && pref.respond_to?(:language)\n"
                "    data['language'] = pref.language\n"
                "  end\n"
                "  data\n"
                "end.compact"
            )
            json_data = client.execute_large_query_to_json_file(ruby_query, container_file=file_path, timeout=180)
        except QueryExecutionError:
            # Propagate specific high-signal errors (tests assert exact messages)
            raise
        except Exception as e:
            msg = "Failed to retrieve users."
            raise QueryExecutionError(msg) from e
        else:
            # Validate that we got a list
            if not isinstance(json_data, list):
                self._logger.error(
                    "Expected list of users, got %s: %s",
                    type(json_data),
                    str(json_data)[:200],
                )
                msg = f"Invalid users data format - expected list, got {type(json_data)}"
                raise QueryExecutionError(msg)

            # Update cache
            client._users_cache = json_data or []
            client._users_cache_time = current_time

            self._logger.info("Retrieved %d users from OpenProject", len(client._users_cache))
            return client._users_cache

    def get_user(self, user_identifier: int | str) -> dict[str, Any]:
        """Get a single user by id, email, or login.

        This is a convenience wrapper over ``find_record`` and existing helpers,
        with light cache lookups to reduce Rails console round-trips.

        Args:
            user_identifier: An integer id, numeric string id, email, or login

        Returns:
            User data as a dictionary

        Raises:
            RecordNotFoundError: If the user cannot be found
            QueryExecutionError: If the lookup fails

        """
        client = self._client
        try:
            # Normalize identifier
            identifier: str | int
            if isinstance(user_identifier, str):
                identifier = user_identifier.strip()
                if not identifier:
                    msg = "Empty user identifier"
                    raise ValueError(msg)
            else:
                identifier = int(user_identifier)

            # If numeric string, treat as id
            if isinstance(identifier, str) and identifier.isdigit():
                identifier = int(identifier)

            # Try cache fast-paths when possible
            if isinstance(identifier, int):
                # Check cached users first
                if getattr(client, "_users_cache", None):
                    for user in client._users_cache or []:
                        try:
                            uid = user.get("id")
                            if isinstance(uid, int) and uid == identifier:
                                return user
                            if isinstance(uid, str) and uid.isdigit() and int(uid) == identifier:
                                return user
                        except Exception:
                            self._logger.debug("Malformed user cache entry encountered")
                            continue

                # Fallback to direct lookup by id
                return client.find_record("User", identifier)

            # Email lookup
            if isinstance(identifier, str) and "@" in identifier:
                return self.get_user_by_email(identifier)

            # Login lookup (try cache first)
            login = identifier  # type: ignore[assignment]
            if getattr(client, "_users_cache", None):
                for user in client._users_cache or []:
                    if user.get("login") == login:
                        # Opportunistically cache by email for future lookups
                        email = user.get("mail") or user.get("email")
                        if isinstance(email, str):
                            client._users_by_email_cache[email.lower()] = user
                        return user

            # Fallback to direct lookup by login
            user = client.find_record("User", {"login": login})
            # Opportunistically cache by email for future lookups
            email = user.get("mail") or user.get("email")
            if isinstance(email, str):
                client._users_by_email_cache[email.lower()] = user
            return user

        except RecordNotFoundError:
            raise
        except Exception as e:
            msg = "Error getting user."
            raise QueryExecutionError(msg) from e

    def get_user_by_email(self, email: str) -> dict[str, Any]:
        """Get a user by email address.

        Uses cached user data if available.

        Args:
            email: Email address of the user

        Returns:
            User data

        Raises:
            RecordNotFoundError: If user with given email is not found
            QueryExecutionError: If query fails

        """
        client = self._client
        # Normalize email to lowercase
        email_lower = email.lower()

        # Check cache first
        if hasattr(client, "_users_by_email_cache") and email_lower in client._users_by_email_cache:
            return client._users_by_email_cache[email_lower]

        # Try to load all users so we can serve subsequent lookups from cache.
        # ``get_users`` does NOT populate ``_users_by_email_cache`` directly,
        # so scan the returned list ourselves and warm the email index. This
        # avoids a wasted Rails round-trip on every email lookup that misses
        # the email cache but matches a user already in the full-list cache.
        try:
            all_users = self.get_users()

            for user in all_users or []:
                user_email = user.get("mail") or user.get("email")
                if isinstance(user_email, str):
                    client._users_by_email_cache[user_email.lower()] = user

            # Now check if the email landed in the populated cache
            if email_lower in client._users_by_email_cache:
                return client._users_by_email_cache[email_lower]

            # If still not found, try direct query
            user = client.find_record("User", {"email": email})
            if user:
                # Cache the result
                client._users_by_email_cache[email_lower] = user
                return user

            msg = f"User with email '{email}' not found"
            raise RecordNotFoundError(msg)

        except RecordNotFoundError:
            raise  # Re-raise RecordNotFoundError
        except Exception as e:
            msg = "Error finding user by email."
            raise QueryExecutionError(msg) from e

    def batch_get_users_by_ids(self, user_ids: list[int]) -> dict[int, dict]:
        """Retrieve multiple users in batches.

        Filters the cached full users list — assumes ``get_users`` has been
        primed once per migration run. Lookups use a ``set`` so this stays
        O(N + M) instead of O(N * M) for large id lists, and cached
        ``user['id']`` values are coerced to ``int`` (some Rails JSON
        responses surface ids as numeric strings) so callers don't miss
        legitimate matches.
        """
        if not user_ids:
            return {}

        wanted: set[int] = {int(uid) for uid in user_ids}
        result: dict[int, dict] = {}
        for user in self.get_users():
            raw_id = user.get("id")
            try:
                uid = int(raw_id) if raw_id is not None else None
            except TypeError, ValueError:
                continue
            if uid is not None and uid in wanted:
                result[uid] = user
        return result

    @batch_idempotent(ttl=3600)  # 1 hour TTL for user email lookups
    def batch_get_users_by_emails(
        self,
        emails: list[str],
        batch_size: int | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple users by email addresses in batches with idempotency support.

        Args:
            emails: List of email addresses to find
            batch_size: Size of each batch (defaults to configured batch_size)
            headers: Optional headers dict; when ``X-Idempotency-Key`` is
                present the ``@batch_idempotent`` decorator caches the
                result under that key for the configured TTL. Without a
                header the decorator's per-call UUID makes the cache a
                no-op (so callers that need real idempotency MUST pass
                a stable key). Keyword-only so it cannot be passed
                positionally — the decorator's ``extract_headers_from_kwargs``
                only sees real kwargs, so positional arguments would
                silently disable caching.

        Returns:
            Dictionary mapping email to user data for successfully fetched
            users. Missing emails are omitted. If one or more batches fail,
            those failures are logged and the method continues processing
            remaining batches, so partial results may be returned.

        Raises:
            QueryExecutionError: If a non-batch error occurs (e.g. the
                ``_validate_batch_size`` call rejects the input).

        """
        # ``headers`` is consumed by the ``@batch_idempotent`` decorator's
        # ``extract_headers_from_kwargs`` helper before the function body
        # runs; we accept-and-discard it here to keep the signature
        # compatible with that contract.
        del headers
        client = self._client
        if not emails:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion. Use
        # ``is not None`` so a caller-supplied ``batch_size=0`` is
        # respected literally rather than swapped for the default.
        effective_batch_size = batch_size if batch_size is not None else getattr(client, "batch_size", 100)
        effective_batch_size = client._validate_batch_size(effective_batch_size)

        results = {}

        # Process emails in batches
        for i in range(0, len(emails), effective_batch_size):
            batch_emails = emails[i : i + effective_batch_size]

            def batch_operation(batch_emails: list[str] = batch_emails) -> list[dict[str, Any]]:
                # Use safe query builder with ActiveRecord parameterization
                query = client._build_safe_batch_query("User", "mail", batch_emails)
                return client.execute_json_query(query)  # type: ignore[return-value]

            try:
                # Execute batch operation with retry logic (with idempotency key propagation)
                batch_results = client._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch users by email {batch_emails[:2]}{'...' if len(batch_emails) > 2 else ''}",
                )

                if batch_results:
                    # Ensure we have a list
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]

                    # Map results by email
                    for record in batch_results:
                        if isinstance(record, dict) and "mail" in record:
                            email = record["mail"]
                            if email in batch_emails:
                                results[email] = record

            except Exception as e:
                self._logger.warning(
                    "Failed to fetch batch of user emails %s after retries: %s",
                    batch_emails,
                    e,
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for email in batch_emails:
                    self._logger.debug("Failed to fetch user by email %s: %s", email, e)
                continue

        return results

    # ── avatars ──────────────────────────────────────────────────────────

    def ensure_local_avatars_enabled(self) -> bool:
        """Enable local avatar uploads if disabled."""
        ruby = (
            "settings = Setting.plugin_openproject_avatars || {}\n"
            "if ActiveModel::Type::Boolean.new.cast(settings['enable_local_avatars'])\n"
            "  { enabled: true }.to_json\n"
            "else\n"
            "  settings['enable_local_avatars'] = true\n"
            "  Setting.plugin_openproject_avatars = settings\n"
            "  { enabled: true }.to_json\n"
            "end\n"
        )
        result = self._client.execute_query_to_json_file(ruby)
        return bool(isinstance(result, dict) and result.get("enabled"))

    def set_user_avatar(
        self,
        *,
        user_id: int,
        container_path: Path,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Upload and assign a local avatar for a user."""
        # Lazy import: ``escape_ruby_single_quoted`` lives on
        # openproject_client; lazy keeps the service ↔ client cycle out
        # of module-load time.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        safe_content_type = escape_ruby_single_quoted(content_type or "image/png")
        safe_filename = escape_ruby_single_quoted(filename)
        head = (
            f"user_id = {int(user_id)}\n"
            f"file_path = '{container_path.as_posix()}'\n"
            f"filename = '{safe_filename}'\n"
            f"content_type = '{safe_content_type}'\n"
        )
        body = """require 'rack/test'
require 'avatars/update_service'

result = { success: false }
user = User.find_by(id: user_id)
if user.nil?
  result = { success: false, error: 'user not found' }
elsif !OpenProject::Avatars::AvatarManager.local_avatars_enabled?
  result = { success: false, error: 'local avatars disabled' }
else
  uploader = Rack::Test::UploadedFile.new(file_path, content_type, true)
  service = ::Avatars::UpdateService.new(user)
  outcome = service.replace(uploader)
  if outcome.success?
    result = { success: true }
  else
    result = { success: false, error: outcome.errors.full_messages.join(', ') }
  end
end
result.to_json
"""
        script = head + body
        response = self._client.execute_query_to_json_file(script, timeout=180)
        if isinstance(response, dict):
            return response
        return {"success": False, "error": "unexpected response"}
