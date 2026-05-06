#!/usr/bin/env python3
"""User migration module for Jira to OpenProject migration.

Handles the migration of users and their accounts from Jira to OpenProject.

Phase 7i notes
--------------
This migration is the **canonical write side of the** ``user_mapping``
**namespace**. Every other migration in the suite consumes
``user_mapping`` (via ``config.mappings.get_mapping("user")``) and
probes it through the canonical
``account_id -> name -> key -> email_address -> display_name`` order
(see ``work_package_skeleton_migration._map_user``,
``work_package_content_migration._resolve_watcher_user_id``,
``attachment_provenance_migration._author_identifiers``,
``category_defaults_migration._resolve_user_id`` and
``watcher_migration``). The on-disk storage shape -- a ``dict[str, dict]``
keyed by Jira user key with the documented payload (``jira_key``,
``jira_name``, ``jira_email``, ``jira_display_name``, ``openproject_id``,
``openproject_login``, ``openproject_email``, ``matched_by``,
``j2o_origin_system``, ``j2o_user_id``, ``j2o_user_key``,
``j2o_external_url``, ``time_zone``, ``locale``, ``avatar_url``) -- is
a contract with all of those readers and is **deliberately preserved
unchanged** here.

What Phase 7i does change is the **boundary parse** of incoming Jira
user payloads in :meth:`_build_user_origin_metadata`. The raw camelCase
Jira REST shape is now validated through :class:`JiraUser.from_dict`
(populate_by_name + alias handling for ``accountId`` / ``displayName`` /
``emailAddress`` / ``timeZone`` / ``avatarUrls``) and the typed fields
drive the metadata derivation. The original dict fallbacks for
non-canonical key variants -- lowercase ``timezone`` and capitalised
``Locale`` -- are kept side-by-side, because :class:`JiraUser` only
recognises the canonical Jira aliases and we want to preserve observable
behaviour for unusual upstream sources. The
:meth:`_ensure_jira_user_details` enrichment still mutates the passed-in
dict in place so cached entries pick up ``accountId`` / ``timeZone`` /
``locale`` / ``avatarUrls`` from a follow-up ``GET /user`` round-trip.

This migration carries no ``wp_map`` ladder hits (it does not consume
the work_package mapping at all), so the
:class:`WorkPackageMappingEntry.from_legacy` / bare-int-skip pattern
from earlier Phase 7 batches does not apply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.display import ProgressTracker
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, JiraUser, MigrationError


def _backfill_unmapped_users_from_op(
    user_mapping: dict[str, dict[str, Any]],
    op_client: Any,
    logger: Any,
) -> int:
    """Reconcile ``matched_by="none"`` mapping entries with live OP users.

    The initial probe in ``create_user_mapping`` can leave entries with
    ``matched_by="none"`` and ``openproject_id: None`` even when the user
    actually exists in OP (custom-field provenance not populated, login
    case mismatch, the disk file came from a partial earlier run, etc.).
    Walk those entries and try a final ``op_client.get_user(login)`` /
    ``...(email)`` lookup; back-fill the mapping when found.

    Returns the count of mappings updated.
    """
    n = 0
    for entry in user_mapping.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("openproject_id"):
            continue
        if entry.get("matched_by") != "none":
            continue
        login = entry.get("jira_name")
        email = entry.get("jira_email")

        existing: dict[str, Any] | None = None
        for probe in (login, email):
            if not probe:
                continue
            try:
                existing = op_client.get_user(probe)
            except Exception:
                existing = None
            if isinstance(existing, dict) and existing.get("id"):
                break
            existing = None
        if not existing:
            continue
        entry["openproject_id"] = int(existing["id"])
        if existing.get("login"):
            entry["openproject_login"] = existing.get("login")
        if existing.get("mail") or existing.get("email"):
            entry["openproject_email"] = existing.get("mail") or existing.get("email")
        entry["matched_by"] = "backfill_op_lookup"
        n += 1
    if n:
        logger.info("Backfilled %d previously-unmapped users from OP lookup", n)
    return n


def _apply_created_user_ids_to_mapping(
    batch: list[dict[str, Any]],
    meta: list[dict[str, Any]],
    created_list: list[dict[str, Any]],
) -> int:
    """Write fresh-created OP user ids back to their mapping entries.

    ``bulk_create_records("User", ...)`` returns ``[{index: N, id: OP_ID}]``
    for each successfully-created user. The caller knows ``batch[N]`` is
    the corresponding mapping entry and ``meta[N]`` carries login/mail.
    Without this back-fill, downstream WP migration's ``_map_user`` treats
    the mapping entry as un-mapped (``openproject_id is None``) and
    silently drops the assignee for every WP authored by that user.

    Returns the count of mappings updated.
    """
    n = 0
    for item in created_list:
        idx = item.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(batch)) or not (0 <= idx < len(meta)):
            continue
        new_op_id = item.get("id")
        if not new_op_id:
            continue
        target = batch[idx]
        # Don't trample an existing mapping (e.g. from duplicate resolution
        # earlier in the same run) — that one was deliberate.
        if target.get("openproject_id"):
            continue
        target["openproject_id"] = int(new_op_id)
        target["openproject_login"] = meta[idx].get("login")
        target["openproject_email"] = meta[idx].get("mail")
        target["matched_by"] = "created"
        n += 1
    return n


@register_entity_types("users", "user_accounts")
class UserMigration(BaseMigration):
    """Handles the migration of users from Jira to OpenProject.

    Since both systems use LDAP/AD for authentication, the focus is on:
    1. Identifying the users in Jira
    2. Ensuring they exist in OpenProject (via LDAP sync)
    3. Creating a mapping between Jira and OpenProject user IDs for later use
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
    ) -> None:
        """Initialize the user migration tools.

        Args:
            jira_client: Initialized Jira client instance
            op_client: Initialized OpenProject client instance

        """
        # Initialize base migration with client dependencies
        super().__init__(
            jira_client=jira_client,
            op_client=op_client,
        )

        # Data storage
        self.jira_users: list[dict[str, Any]] = []
        self.op_users: list[dict[str, Any]] = []
        self.user_mapping: dict[str, Any] = {}

        # Setup file paths
        self.jira_users_file = self.data_dir / "jira_users.json"
        self.op_users_file = self.data_dir / "op_users.json"
        self.user_mapping_file = self.data_dir / "user_mapping.json"

        # Logging
        self.logger.debug("UserMigration initialized with data dir: %s", self.data_dir)

        # Load existing data if available
        self.jira_users = self._load_from_json(Path("jira_users.json")) or []
        self.op_users = self._load_from_json(Path("op_users.json")) or []

        self.user_mapping = config.mappings.get_mapping("user") or {}

        # If mapping is empty but OP might have provenance data, try to restore
        if not self.user_mapping:
            self.logger.info(
                "User mapping is empty - will attempt restoration from OP on first use",
            )

        # Caches for provenance helpers
        self._origin_cf_id_map: dict[str, int] | None = None
        self._origin_system_label_cache: str | None = None
        self._jira_base_url_cache: str | None = None
        self._jira_user_index: dict[str, dict[str, Any]] | None = None
        self._supported_languages: set[str] | None = None
        self.avatar_cache_file = self.data_dir / "user_avatar_cache.json"
        self._avatar_cache: dict[str, Any] = self._load_from_json(self.avatar_cache_file) or {}

    def extract_jira_users(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Extract users from Jira.

        Returns:
            List of Jira users

        Raises:
            MigrationError: If users cannot be extracted from Jira

        """
        if self.jira_users and not force:
            self.logger.info("Using cached Jira users (%s entries)", len(self.jira_users))
            return self.jira_users

        self.logger.info("Extracting users from Jira...")

        self.jira_users = self.jira_client.get_users()

        if not self.jira_users:
            msg = "Failed to extract users from Jira"
            raise MigrationError(msg)

        self.logger.info("Extracted %s users from Jira", len(self.jira_users))

        self._save_to_json(self.jira_users, Path("jira_users.json"))

        return self.jira_users

    def extract_openproject_users(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Extract users from OpenProject.

        Returns:
            List of OpenProject users

        Raises:
            MigrationError: If users cannot be extracted from OpenProject

        """
        if self.op_users and not force:
            self.logger.info("Using cached OpenProject users (%s entries)", len(self.op_users))
            return self.op_users

        self.logger.info("Extracting users from OpenProject...")

        # Get users from OpenProject - no fallbacks or mocks
        self.op_users = self.op_client.get_users()

        if not self.op_users:
            # Instead of failing completely, log a warning and continue with empty list
            # This allows the migration to proceed even if user extraction has issues
            self.logger.warning(
                "Failed to extract users from OpenProject - continuing with empty user list",
            )
            self.logger.warning(
                "This may be due to JSON parsing issues with large user datasets",
            )
            self.op_users = []

        self.logger.info(
            "Extracted %s users from OpenProject",
            len(self.op_users),
        )

        self._save_to_json(self.op_users, Path("op_users.json"))

        return self.op_users

    def create_user_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira and OpenProject users.

        Returns:
            Dictionary keyed by Jira user key with OpenProject IDs and J2O provenance metadata

        Raises:
            MigrationError: If required user data is missing

        """
        self.logger.info("Creating user mapping...")

        if not self.jira_users:
            self.extract_jira_users()

        if not self.op_users:
            self.extract_openproject_users()

        # Debug: Check what type of data we're getting
        self.logger.debug("OpenProject users data type: %s", type(self.op_users))
        if self.op_users:
            self.logger.debug("First user data type: %s", type(self.op_users[0]))
            self.logger.debug("First user content: %s", str(self.op_users[0])[:200])

        # Ensure we have a list of dictionaries
        if not isinstance(self.op_users, list):
            msg = f"Expected list of users, got {type(self.op_users)}"
            raise MigrationError(msg)

        # Filter out any non-dictionary items
        valid_users = []
        for i, user in enumerate(self.op_users):
            if isinstance(user, dict):
                valid_users.append(user)
            else:
                self.logger.warning(
                    "Skipping invalid user data at index %d: %s (type: %s)",
                    i,
                    str(user)[:100],
                    type(user),
                )

        self.op_users = valid_users
        self.logger.info("Filtered to %d valid user records", len(self.op_users))

        op_users_by_username = {user.get("login", "").lower(): user for user in self.op_users if user.get("login")}
        # Prefer deterministic mapping via J2O provenance custom fields
        op_users_by_origin_key: dict[str, dict[str, Any]] = {}
        op_users_by_origin_id: dict[str, dict[str, Any]] = {}
        # OpenProject user email field is 'mail'; fall back to 'email' if present
        op_users_by_email: dict[str, dict[str, Any]] = {}
        for user in self.op_users:
            op_mail = (user.get("mail") or user.get("email") or "").lower()
            if op_mail:
                op_users_by_email[op_mail] = user

            origin_key = (user.get("j2o_user_key") or user.get("jira_user_key") or "").lower()
            if origin_key:
                op_users_by_origin_key[origin_key] = user

            origin_id = (user.get("j2o_user_id") or "").lower()
            if origin_id:
                op_users_by_origin_id[origin_id] = user

        mapping: dict[str, Any] = {}

        with ProgressTracker(
            "Mapping users",
            len(self.jira_users),
            "Recent User Mappings",
        ) as tracker:
            for jira_user in self.jira_users:
                jira_key = jira_user.get("key", "")  # Ensure non-None value
                if not jira_key:
                    self.logger.warning("Found Jira user without key, skipping")
                    continue

                # Enrich with up-to-date Jira metadata (accountId, timezone, etc.)
                origin_meta = self._build_user_origin_metadata(jira_user)

                jira_name = (jira_user.get("name") or "").lower()
                jira_email = (jira_user.get("emailAddress") or "").lower()
                jira_display_name = jira_user.get("displayName", "")
                jira_account_id = (origin_meta.get("user_id") or "").lower()
                origin_key_lower = (origin_meta.get("user_key") or "").lower()

                tracker.update_description(f"Mapping user: {jira_display_name}")

                matched_by = "none"
                op_user: dict[str, Any] | None = None

                # 1) Provenance key match (strongest)
                if origin_key_lower and origin_key_lower in op_users_by_origin_key:
                    op_user = op_users_by_origin_key[origin_key_lower]
                    matched_by = "j2o_user_key_cf"
                # 2) Provenance ID (Jira accountId) match
                elif jira_account_id and jira_account_id in op_users_by_origin_key:
                    op_user = op_users_by_origin_key[jira_account_id]
                    matched_by = "j2o_user_key_cf"
                elif jira_account_id and jira_account_id in op_users_by_origin_id:
                    op_user = op_users_by_origin_id[jira_account_id]
                    matched_by = "j2o_user_id_cf"
                # 3) Username match
                elif jira_name in op_users_by_username:
                    op_user = op_users_by_username[jira_name]
                    matched_by = "username"
                # 4) Email match
                elif jira_email and jira_email in op_users_by_email:
                    op_user = op_users_by_email[jira_email]
                    matched_by = "email"

                if op_user:
                    mapping[jira_key] = {
                        "jira_key": jira_key,
                        "jira_name": jira_name,
                        "jira_email": jira_email,
                        "jira_display_name": jira_display_name,
                        "openproject_id": op_user.get("id"),
                        "openproject_login": op_user.get("login"),
                        "openproject_email": op_user.get("mail") or op_user.get("email"),
                        "matched_by": matched_by,
                        "j2o_origin_system": origin_meta.get("origin_system"),
                        "j2o_user_id": origin_meta.get("user_id"),
                        "j2o_user_key": origin_meta.get("user_key"),
                        "j2o_external_url": origin_meta.get("external_url"),
                        "time_zone": origin_meta.get("time_zone"),
                        "locale": origin_meta.get("locale"),
                        "avatar_url": origin_meta.get("avatar_url"),
                    }
                    tracker.add_log_item(
                        f"Matched by {matched_by}: {jira_display_name} → {op_user.get('login')}",
                    )
                    tracker.increment()
                    continue
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": jira_name,
                    "jira_email": jira_email,
                    "jira_display_name": jira_display_name,
                    "openproject_id": None,
                    "openproject_login": None,
                    "openproject_email": None,
                    "matched_by": "none",
                    "j2o_origin_system": origin_meta.get("origin_system"),
                    "j2o_user_id": origin_meta.get("user_id"),
                    "j2o_user_key": origin_meta.get("user_key"),
                    "j2o_external_url": origin_meta.get("external_url"),
                    "time_zone": origin_meta.get("time_zone"),
                    "locale": origin_meta.get("locale"),
                    "avatar_url": origin_meta.get("avatar_url"),
                }
                tracker.add_log_item(f"No match found: {jira_display_name}")
                tracker.increment()

        # Save the mapping via controller only
        config.mappings.set_mapping("user", mapping)
        self.user_mapping = mapping

        # Persist the refreshed OpenProject user snapshot with provenance values
        for user in self.op_users:
            if isinstance(user, dict):
                user.pop("jira_user_key", None)
        self._save_to_json(self.op_users, Path("op_users.json"))

        return mapping

    def ensure_j2o_custom_fields(self) -> dict[str, int]:
        """Ensure J2O user provenance custom fields exist in OpenProject.

        Creates the following UserCustomField entries if they don't exist:
        - J2O Origin System: Source system identifier
        - J2O User ID: Original Jira user ID/accountId
        - J2O User Key: Original Jira user key
        - J2O External URL: Link back to Jira user profile

        Returns:
            Dictionary mapping field names to their OP custom field IDs

        """
        return self._get_user_origin_cf_ids()

    def restore_mapping_from_openproject(self) -> dict[str, Any]:
        """Restore user mapping from OpenProject provenance data alone.

        This method rebuilds the user mapping by querying OP for users that have
        J2O provenance custom fields populated. It does NOT require Jira data,
        making it suitable for recovery scenarios where local mapping files are
        missing but OP contains provenance data from previous migrations.

        Prioritizes J2O provenance fields, falls back to username/email matching
        if provenance data is not available.

        Returns:
            Dictionary keyed by Jira user key with OpenProject mapping data

        """
        self.logger.info("Restoring user mapping from OpenProject provenance data...")

        # Ensure J2O custom fields exist (creates them if missing)
        cf_ids = self.ensure_j2o_custom_fields()
        if cf_ids:
            self.logger.info("J2O user custom fields available: %s", list(cf_ids.keys()))

        # Extract fresh OP users (force refresh to get latest provenance data)
        self.op_users = self.op_client.get_users()
        if not self.op_users:
            self.logger.warning("No users found in OpenProject")
            return {}

        self.logger.info("Found %d users in OpenProject", len(self.op_users))

        mapping: dict[str, Any] = {}
        restored_from_provenance = 0
        restored_from_username = 0

        for op_user in self.op_users:
            if not isinstance(op_user, dict):
                continue

            op_id = op_user.get("id")
            op_login = op_user.get("login", "")
            op_email = op_user.get("mail") or op_user.get("email") or ""

            # Try to get J2O provenance data from the user
            j2o_user_key = op_user.get("j2o_user_key") or ""
            j2o_user_id = op_user.get("j2o_user_id") or ""
            j2o_origin_system = op_user.get("j2o_origin_system") or ""
            j2o_external_url = op_user.get("j2o_external_url") or ""

            # If provenance data exists, use it as the mapping key
            if j2o_user_key:
                mapping[j2o_user_key] = {
                    "jira_key": j2o_user_key,
                    "jira_name": op_login,  # Best guess from OP data
                    "jira_email": op_email,  # Best guess from OP data
                    "jira_display_name": f"{op_user.get('firstname', '')} {op_user.get('lastname', '')}".strip(),
                    "openproject_id": op_id,
                    "openproject_login": op_login,
                    "openproject_email": op_email,
                    "matched_by": "j2o_provenance",
                    "j2o_origin_system": j2o_origin_system,
                    "j2o_user_id": j2o_user_id,
                    "j2o_user_key": j2o_user_key,
                    "j2o_external_url": j2o_external_url,
                    "restored_from_op": True,
                }
                restored_from_provenance += 1
            elif op_login:
                # Fall back to using OP login as key (assumes login matches Jira username)
                # This handles users that existed in OP before J2O custom fields were created
                mapping[op_login] = {
                    "jira_key": op_login,  # Best guess
                    "jira_name": op_login,
                    "jira_email": op_email,
                    "jira_display_name": f"{op_user.get('firstname', '')} {op_user.get('lastname', '')}".strip(),
                    "openproject_id": op_id,
                    "openproject_login": op_login,
                    "openproject_email": op_email,
                    "matched_by": "username_fallback",
                    "j2o_origin_system": j2o_origin_system or "unknown",
                    "j2o_user_id": j2o_user_id,
                    "j2o_user_key": j2o_user_key,
                    "j2o_external_url": j2o_external_url,
                    "restored_from_op": True,
                }
                restored_from_username += 1

        self.logger.info(
            "Restored %d users from OP: %d from J2O provenance, %d from username fallback",
            len(mapping),
            restored_from_provenance,
            restored_from_username,
        )

        # Persist the restored mapping
        if mapping:
            config.mappings.set_mapping("user", mapping)
            self.user_mapping = mapping
            self._save_to_json(self.op_users, Path("op_users.json"))

        return mapping

    def _build_fallback_email(
        self,
        login: str,
        existing_emails: set[str] | None = None,
    ) -> str:
        """Build a safe, unique fallback email address for a user.

        Args:
            login: The user's login name from JIRA
            existing_emails: Set of existing email addresses to avoid collisions

        Returns:
            A valid, unique email address

        """
        if existing_emails is None:
            existing_emails = set()

        # Sanitize login to RFC-5322 compliant format
        # Keep only letters, digits, dots, underscores, and hyphens
        sanitized_login = re.sub(r"[^a-zA-Z0-9._-]", "", login.lower())

        # If sanitization results in empty string, use UUID
        if not sanitized_login:
            sanitized_login = str(uuid.uuid4())[:8]

        # Build base email
        base_email = f"{sanitized_login}@{config.FALLBACK_MAIL_DOMAIN}"

        # Check for uniqueness
        if base_email not in existing_emails:
            return base_email

        # Handle collisions by appending counter
        counter = 1
        while True:
            candidate_email = f"{sanitized_login}.{counter}@{config.FALLBACK_MAIL_DOMAIN}"
            if candidate_email not in existing_emails:
                return candidate_email
            counter += 1

    def _get_user_origin_cf_ids(self) -> dict[str, int]:
        """Ensure and cache user-level origin custom field IDs."""
        if self._origin_cf_id_map is not None:
            return self._origin_cf_id_map

        for legacy_cf in ("Jira user key", "Tempo Account"):
            try:
                removal_result = self.op_client.remove_custom_field(
                    legacy_cf,
                    cf_type="UserCustomField",
                )
                removed_count = removal_result.get("removed", 0)
                if removed_count:
                    self.logger.info(
                        "Removed legacy user custom field '%s' (%s entry/entries)",
                        legacy_cf,
                        removed_count,
                    )
            except Exception as exc:
                self.logger.debug(
                    "Failed to remove legacy user custom field '%s': %s",
                    legacy_cf,
                    exc,
                )

        cf_names = (
            ("J2O Origin System", "string"),
            ("J2O User ID", "string"),
            ("J2O User Key", "string"),
            ("J2O External URL", "string"),
        )
        ids: dict[str, int] = {}
        for name, field_format in cf_names:
            try:
                cf = self.op_client.ensure_custom_field(
                    name,
                    field_format=field_format,
                    cf_type="UserCustomField",
                )
                if isinstance(cf, dict) and cf.get("id"):
                    ids[name] = int(cf["id"])
            except Exception as exc:
                self.logger.warning("Unable to ensure user custom field %s: %s", name, exc)

        self._origin_cf_id_map = ids
        return ids

    def _get_origin_system_label(self) -> str:
        if self._origin_system_label_cache:
            return self._origin_system_label_cache

        label = "Jira"
        try:
            jira_client = getattr(self, "jira_client", None)
            server_info = None
            if jira_client and getattr(jira_client, "jira", None):
                server_info = jira_client.jira.server_info()

            deployment = None
            version = None
            if isinstance(server_info, dict):
                deployment = server_info.get("deploymentType") or server_info.get("deploymenttype")
                version = server_info.get("version") or server_info.get("serverVersion")

            parts: list[str] = ["Jira"]
            if deployment:
                parts.append(str(deployment).title())
            elif config.jira_config.get("deployment"):
                parts.append(str(config.jira_config.get("deployment")).title())
            if version:
                parts.append(str(version))

            label = " ".join(part for part in parts if part).strip()
        except Exception as exc:
            self.logger.debug("Failed to derive Jira origin label: %s", exc)

        self._origin_system_label_cache = label or "Jira"
        return self._origin_system_label_cache

    def _get_jira_base_url(self) -> str:
        if self._jira_base_url_cache:
            return self._jira_base_url_cache

        base_url = ""
        try:
            jira_client = getattr(self, "jira_client", None)
            server_info = None
            if jira_client and getattr(jira_client, "jira", None):
                server_info = jira_client.jira.server_info()
            if isinstance(server_info, dict):
                base_url = str(server_info.get("baseUrl") or "").strip()
        except Exception:
            base_url = ""

        if not base_url:
            base_url = str(config.jira_config.get("url", "")).strip()

        self._jira_base_url_cache = base_url.rstrip("/") if base_url else ""
        return self._jira_base_url_cache

    def _get_supported_languages(self) -> set[str]:
        if self._supported_languages is not None:
            return self._supported_languages

        try:
            locales = self.op_client.execute_query_to_json_file(
                "I18n.available_locales.map(&:to_s)",
            )
            if isinstance(locales, list):
                normalized = {str(locale).strip().lower().replace("-", "_") for locale in locales if locale}
                self._supported_languages = normalized
            else:
                self._supported_languages = set()
        except Exception as exc:
            self.logger.debug("Failed to fetch supported languages: %s", exc)
            self._supported_languages = set()

        return self._supported_languages

    def _map_locale_to_language(self, locale: str | None) -> str:
        if not locale:
            return ""

        supported = self._get_supported_languages()
        if not supported:
            return ""

        normalized = str(locale).strip().lower().replace("-", "_")
        candidates = [normalized]
        if "_" in normalized:
            candidates.append(normalized.split("_", 1)[0])

        for candidate in candidates:
            if candidate in supported:
                return candidate
        return ""

    def _prepare_avatar_job(
        self,
        *,
        jira_user: dict[str, Any],
        op_user: dict[str, Any] | None,
        mapping: dict[str, Any],
        meta: dict[str, Any],
    ) -> dict[str, Any] | None:
        avatar_url = str(meta.get("avatar_url") or "").strip()
        if not avatar_url:
            return None

        op_id = mapping.get("openproject_id") or mapping.get("openproject_user_id")
        if not op_id:
            return None

        try:
            op_id_int = int(op_id)
        except Exception:
            return None

        jira_key = mapping.get("jira_key") or meta.get("user_key")
        if not jira_key:
            return None

        cache_entry = self._avatar_cache.get(str(jira_key)) or {}
        return {
            "jira_key": str(jira_key),
            "openproject_id": op_id_int,
            "avatar_url": avatar_url,
            "cache": cache_entry,
        }

    def _sync_user_avatars(self, jobs: list[dict[str, Any]]) -> dict[str, int]:
        if not jobs:
            return {"uploaded": 0, "skipped": 0}

        try:
            self.op_client.ensure_local_avatars_enabled()
        except Exception as exc:
            self.logger.warning("Failed to ensure local avatars are enabled: %s", exc)
            return {"uploaded": 0, "skipped": len(jobs)}

        avatar_dir = self.data_dir / "avatars"
        avatar_dir.mkdir(parents=True, exist_ok=True)

        uploaded = 0
        skipped = 0

        for job in jobs:
            jira_key = job["jira_key"]
            avatar_url = job["avatar_url"]
            cache_entry = job.get("cache") or {}

            download = self.jira_client.download_user_avatar(avatar_url)
            if not download:
                skipped += 1
                continue

            data, content_type = download
            digest = hashlib.sha256(data).hexdigest()

            cached_digest = str(cache_entry.get("digest")) if cache_entry else ""
            cached_url = str(cache_entry.get("url")) if cache_entry else ""
            if digest == cached_digest and avatar_url == cached_url:
                self._avatar_cache[jira_key] = cache_entry
                self.logger.debug(
                    "Skipping avatar upload for %s (digest match)",
                    jira_key,
                )
                skipped += 1
                continue

            ext = self._guess_avatar_extension(content_type, avatar_url)
            filename = f"{jira_key}.{ext}"
            local_path = avatar_dir / filename
            try:
                with local_path.open("wb") as handle:
                    handle.write(data)
            except Exception as exc:
                self.logger.warning("Failed to persist avatar for %s: %s", jira_key, exc)
                skipped += 1
                continue

            container_name = f"j2o_avatar_{job['openproject_id']}_{uuid.uuid4().hex}.{ext}"
            container_path = Path("/tmp") / container_name
            try:
                self.op_client.transfer_file_to_container(local_path, container_path)
            except Exception as exc:
                self.logger.warning("Failed to copy avatar for %s to container: %s", jira_key, exc)
                skipped += 1
                with suppress(OSError):
                    local_path.unlink()
                continue

            try:
                result = self.op_client.set_user_avatar(
                    user_id=job["openproject_id"],
                    container_path=container_path,
                    filename=filename,
                    content_type=content_type,
                )
            except Exception as exc:
                self.logger.warning("Failed to set avatar for %s: %s", jira_key, exc)
                skipped += 1
                result = {"success": False}
            finally:
                try:
                    self.op_client.docker_client.execute_command(
                        f"rm -f {container_path.as_posix()}",
                        timeout=10,
                    )
                except Exception:
                    pass

            with suppress(OSError):
                local_path.unlink()

            if result.get("success"):
                uploaded += 1
                self._avatar_cache[jira_key] = {
                    "digest": digest,
                    "url": avatar_url,
                }
            else:
                skipped += 1

        self._save_avatar_cache()
        return {"uploaded": uploaded, "skipped": skipped}

    def _guess_avatar_extension(self, content_type: str, avatar_url: str) -> str:
        candidate = ""
        if content_type:
            candidate = (mimetypes.guess_extension(content_type) or "").lstrip(".")
        if not candidate and avatar_url:
            path_ext = avatar_url.split("?", maxsplit=1)[0].rsplit(".", 1)
            if len(path_ext) == 2:
                candidate = path_ext[1]
        if not candidate:
            candidate = "png"
        return candidate.lower()

    def _save_avatar_cache(self) -> None:
        try:
            self._save_to_json(self._avatar_cache, self.avatar_cache_file)
        except Exception as exc:
            self.logger.debug("Failed to persist avatar cache: %s", exc)

    def _get_jira_user_index(self) -> dict[str, dict[str, Any]]:
        if self._jira_user_index is not None:
            return self._jira_user_index

        index: dict[str, dict[str, Any]] = {}
        for user in self.jira_users:
            for key in (user.get("key"), user.get("name"), user.get("accountId")):
                if key:
                    index[str(key)] = user
        self._jira_user_index = index
        return index

    def _ensure_jira_user_details(self, jira_user: dict[str, Any]) -> None:
        if not jira_user:
            return

        needs_enrichment = any(not jira_user.get(field) for field in ("accountId", "timeZone", "locale", "avatarUrls"))

        if not needs_enrichment:
            return

        key = jira_user.get("key") or jira_user.get("name")
        if not key:
            return

        try:
            details = self.jira_client.get_user_info(str(key))
        except Exception as exc:
            self.logger.debug("Failed to enrich Jira user %s: %s", key, exc)
            return

        if not details:
            return

        for attr in ("accountId", "timeZone", "emailAddress", "displayName", "locale", "avatarUrls"):
            if attr not in jira_user or not jira_user[attr]:
                value = details.get(attr)
                if value:
                    jira_user[attr] = value

    def _build_user_origin_metadata(self, jira_user: dict[str, Any]) -> dict[str, Any]:
        """Build the J2O origin-metadata payload for a Jira user dict.

        Phase 7i: parse the raw Jira user dict at the boundary via
        :class:`JiraUser.from_dict`. The pydantic model handles the
        canonical Jira REST aliases (``accountId``, ``displayName``,
        ``emailAddress``, ``timeZone``, ``avatarUrls``) and -- because
        ``populate_by_name`` is enabled -- also accepts the snake_case
        equivalents (``account_id``, ``display_name``, …). Non-canonical
        legacy fallbacks (lowercase ``timezone``, capital ``Locale``)
        are preserved as dict-level fallbacks because :class:`JiraUser`
        intentionally only knows the canonical Jira aliases.

        Note: :meth:`_ensure_jira_user_details` still mutates the
        passed-in ``jira_user`` dict in place to enrich missing
        ``accountId`` / ``timeZone`` / ``locale`` / ``avatarUrls`` from
        a follow-up ``GET /user`` round-trip; downstream callers (e.g.
        :meth:`backfill_user_origin_metadata`) rely on that side-effect.
        """
        self._ensure_jira_user_details(jira_user)

        try:
            user = JiraUser.from_dict(jira_user)
        except (ValidationError, TypeError) as exc:
            # Boundary parse must never bring down user mapping;
            # fall back to a permissive empty model and lean on the
            # explicit dict fallbacks below. Log at debug so malformed
            # upstream payloads are still diagnosable.
            self.logger.debug(
                "JiraUser.from_dict failed for upstream user payload: %s",
                exc,
            )
            user = JiraUser()

        origin_system = self._get_origin_system_label()
        # Probe canonical Jira REST keys first (covers Cloud + Server/DC),
        # then snake_case for parity with legacy in-process payloads. The
        # dict-side fallbacks are explicit so a JiraUser parse failure
        # doesn't silently lose any identifier.
        account_id = user.account_id or jira_user.get("accountId") or jira_user.get("account_id")
        name_candidate = user.name or jira_user.get("name")
        key_candidate = user.key or jira_user.get("key")
        display_name_candidate = user.display_name or jira_user.get("displayName") or jira_user.get("display_name")
        jira_key = key_candidate or name_candidate
        # Canonical user_id probe: account_id → name → key → email → display_name
        # (matches AttachmentProvenanceMigration / WatcherMigration probe order).
        user_id = (
            account_id
            or name_candidate
            or key_candidate
            or user.email_address
            or jira_user.get("emailAddress")
            or jira_user.get("email")
            or display_name_candidate
        )

        base_url = self._get_jira_base_url()
        external_url = ""
        if base_url and jira_key:
            if account_id:
                external_url = f"{base_url}/secure/ViewProfile.jspa?accountId={quote(str(account_id))}"
            elif name_candidate:
                external_url = f"{base_url}/secure/ViewProfile.jspa?name={quote(str(name_candidate))}"
            else:
                external_url = f"{base_url}/secure/ViewProfile.jspa?name={quote(str(jira_key))}"

        # Probe canonical Jira REST keys first (``timeZone``), then the
        # non-canonical lowercase/capital legacy fallbacks for parity.
        timezone = user.time_zone or jira_user.get("timeZone") or jira_user.get("timezone")
        locale = user.locale or jira_user.get("locale") or jira_user.get("Locale")
        avatar_urls = user.avatar_urls or jira_user.get("avatarUrls") or {}

        avatar_url = ""
        if isinstance(avatar_urls, dict):
            for size_key in ("128x128", "72x72", "48x48", "32x32", "16x16"):
                candidate = avatar_urls.get(size_key)
                if candidate:
                    avatar_url = str(candidate)
                    break
            if not avatar_url:
                # pick any value deterministically (sorted)
                for key in sorted(avatar_urls):
                    candidate = avatar_urls.get(key)
                    if candidate:
                        avatar_url = str(candidate)
                        break

        return {
            "origin_system": origin_system,
            "user_id": str(user_id) if user_id else "",
            "user_key": str(jira_key) if jira_key else "",
            "external_url": external_url,
            "time_zone": str(timezone) if timezone else "",
            "locale": str(locale) if locale else "",
            "avatar_url": avatar_url,
        }

    def create_missing_users(self, batch_size: int | None = None) -> dict[str, Any]:
        """Create missing users in OpenProject using the LDAP synchronization.

        Args:
            batch_size: Number of users to create in each batch (defaults to config value)

        Returns:
            Dictionary with results of user creation

        Raises:
            MigrationError: If user mapping is missing or if users cannot be created

        """
        self.logger.info("Creating missing users in OpenProject...")

        # Use config default if no batch_size provided
        if batch_size is None:
            batch_size = config.USER_CREATION_BATCH_SIZE

        if not self.user_mapping:
            self.create_user_mapping()

        # Bug A2 reconcile: the initial probe in ``create_user_mapping``
        # sometimes can't match a user that already exists in OP — e.g.
        # because the user-provenance CFs aren't populated yet, or the
        # disk file came from an older partial run. Look those rows up
        # by login / email before deciding who's "missing", so we don't
        # try to re-create users that exist (and avoid leaving the
        # mapping with ``openproject_id: None`` for users we know are
        # there — which silently drops their downstream WP/TimeEntry
        # author/assignee assignments).
        try:
            _backfill_unmapped_users_from_op(self.user_mapping, self.op_client, self.logger)
        except Exception as backfill_err:
            self.logger.warning("User-mapping back-fill skipped: %s", backfill_err)

        missing_users = [user for user in self.user_mapping.values() if user["matched_by"] == "none"]

        total = len(missing_users)
        created = 0
        failed = 0
        created_users: list[dict[str, Any]] = []

        if not missing_users:
            self.logger.info("No missing users to create")
        else:
            self.logger.info(
                "Found %s users missing in OpenProject",
                total,
            )

        # Collect existing emails to prevent collisions
        existing_emails = set()
        if self.op_users:
            for op_user in self.op_users:
                if isinstance(op_user, dict) and "mail" in op_user:
                    existing_emails.add(op_user["mail"])

        cf_ids = self._get_user_origin_cf_ids()
        jira_index = self._get_jira_user_index()

        if missing_users:
            with ProgressTracker(
                "Creating users",
                total,
                "Recent User Creations",
            ) as tracker:
                for i in range(0, len(missing_users), batch_size):
                    batch = missing_users[i : i + batch_size]

                    # Prepare data for user creation
                    users_to_create = []
                    for user in batch:
                        # Split display name into first and last name - handle empty display names
                        display_name = user["jira_display_name"].strip() if user["jira_display_name"] else ""
                        names = ["User", user["jira_name"]] if not display_name else display_name.split(" ", 1)

                        first_name = names[0].strip() or "User"
                        last_name = names[1].strip() if len(names) > 1 and names[1].strip() else user["jira_name"]

                        # Ensure first_name and last_name are valid (non-empty, alphanumeric start)
                        # OpenProject requires at least 1 character for names
                        if not first_name or not first_name.strip():
                            first_name = "User"
                        if not last_name or not last_name.strip():
                            # Use jira_name as fallback, or "Unknown" if that's also empty
                            last_name = (
                                user["jira_name"].strip()
                                if user["jira_name"] and user["jira_name"].strip()
                                else "Unknown"
                            )

                        # Remove organization suffixes in brackets (e.g., "[DMK]", "[Tiracon]")
                        # These are common in Jira display names but OpenProject rejects square brackets
                        import re as _re

                        last_name = _re.sub(r"\s*\[.*?\]\s*$", "", last_name).strip()
                        last_name = _re.sub(r"\s*\(.*?\)\s*$", "", last_name).strip()  # Also parentheses

                        # Remove any control characters or problematic unicode
                        first_name = "".join(c for c in first_name if c.isprintable()).strip() or "User"
                        last_name = "".join(c for c in last_name if c.isprintable()).strip() or "Unknown"

                        # Final validation: ensure names only contain allowed characters
                        # OpenProject allows letters, numbers, spaces, hyphens, apostrophes
                        first_name = _re.sub(r"[^\w\s\-']", "", first_name).strip() or "User"
                        last_name = _re.sub(r"[^\w\s\-']", "", last_name).strip() or "Unknown"

                        # Handle missing or empty email addresses with fallback
                        email = user["jira_email"]
                        if not email or email.strip() == "":
                            # Generate safe, unique fallback email
                            email = self._build_fallback_email(
                                user["jira_name"],
                                existing_emails,
                            )
                            existing_emails.add(email)  # Track newly generated email
                            self.logger.info(
                                f"Using fallback email for user {user['jira_name']}: {email}",
                            )

                        jira_payload = jira_index.get(str(user.get("jira_key")))
                        if not jira_payload:
                            jira_payload = {
                                "key": user.get("jira_key"),
                                "name": user.get("jira_name"),
                                "emailAddress": user.get("jira_email"),
                                "displayName": user.get("jira_display_name"),
                            }
                        origin_meta = self._build_user_origin_metadata(jira_payload)

                        cf_payload: list[dict[str, Any]] = []
                        if origin_meta.get("origin_system") and cf_ids.get("J2O Origin System"):
                            cf_payload.append(
                                {
                                    "id": cf_ids["J2O Origin System"],
                                    "value": origin_meta["origin_system"],
                                },
                            )
                        if origin_meta.get("user_id") and cf_ids.get("J2O User ID"):
                            cf_payload.append(
                                {
                                    "id": cf_ids["J2O User ID"],
                                    "value": origin_meta["user_id"],
                                },
                            )
                        if origin_meta.get("user_key") and cf_ids.get("J2O User Key"):
                            cf_payload.append(
                                {
                                    "id": cf_ids["J2O User Key"],
                                    "value": origin_meta["user_key"],
                                },
                            )
                        if origin_meta.get("external_url") and cf_ids.get("J2O External URL"):
                            cf_payload.append(
                                {
                                    "id": cf_ids["J2O External URL"],
                                    "value": origin_meta["external_url"],
                                },
                            )

                        user_record: dict[str, Any] = {
                            "login": user["jira_name"],
                            "firstname": first_name,
                            "lastname": last_name,
                            "mail": email,
                            "admin": False,
                            "status": "active",
                        }
                        if cf_payload:
                            user_record["custom_fields"] = cf_payload
                        if origin_meta.get("time_zone"):
                            user_record["pref_attributes"] = {"time_zone": origin_meta["time_zone"]}

                        users_to_create.append(user_record)

                    batch_users = [user["jira_name"] for user in batch]
                    tracker.update_description(f"Creating users: {', '.join(batch_users)}")

                    # Use generic bulk create helper
                    try:
                        self.logger.info(f"Creating {len(users_to_create)} users via bulk_create_records")
                        # Build records for User model
                        records: list[dict[str, Any]] = []
                        meta: list[dict[str, Any]] = []
                        for u in users_to_create:
                            meta.append({"login": u.get("login"), "mail": u.get("mail")})
                            record: dict[str, Any] = {
                                "login": u.get("login"),
                                "firstname": u.get("firstname"),
                                "lastname": u.get("lastname"),
                                "mail": u.get("mail"),
                                "admin": bool(u.get("admin", False)),
                                "status": (u.get("status") or "active"),
                            }
                            if u.get("custom_fields"):
                                record["custom_fields"] = u["custom_fields"]
                            if u.get("pref_attributes"):
                                record["pref_attributes"] = u["pref_attributes"]
                            records.append(record)

                        result = self.op_client.bulk_create_records(
                            model="User",
                            records=records,
                            timeout=getattr(config, "USER_CREATION_TIMEOUT", 120),
                            result_basename="j2o_user_bulk_result.json",
                        )

                        if not isinstance(result, dict) or result.get("status") != "success":
                            raise MigrationError(result.get("message", "Bulk user creation failed"))

                        created_list = result.get("created", []) or []
                        error_list = result.get("errors", []) or []

                        batch_created = len(created_list)
                        created += batch_created

                        resolved_errors = 0
                        unresolved_indices: set[int] = set()
                        for err in error_list:
                            try:
                                idx = int(err.get("index"))
                            except Exception:
                                idx = -1
                            if idx < 0 or idx >= len(batch):
                                continue
                            messages = [str(m).lower() for m in err.get("errors", [])]
                            duplicate_violation = any(
                                "username has already been taken" in msg or "email has already been taken" in msg
                                for msg in messages
                            )
                            if not duplicate_violation:
                                unresolved_indices.add(idx)
                                continue

                            target_mapping = batch[idx]
                            jira_key = target_mapping.get("jira_key")
                            jira_user = jira_index.get(str(jira_key)) if jira_key else None
                            origin_meta = self._build_user_origin_metadata(jira_user) if jira_user else {}

                            existing_op_user: dict[str, Any] | None = None
                            login_candidate = target_mapping.get("jira_name") or target_mapping.get("jira_display_name")
                            email_candidate = target_mapping.get("jira_email")
                            try:
                                if login_candidate:
                                    existing_op_user = self.op_client.get_user(login_candidate)
                            except Exception:
                                existing_op_user = None
                            if (not existing_op_user) and email_candidate:
                                try:
                                    existing_op_user = self.op_client.get_user(email_candidate)
                                except Exception:
                                    existing_op_user = None

                            if (not existing_op_user) and login_candidate:
                                try:
                                    ruby_expr = f"user = User.find_by(login: {json.dumps(login_candidate)}); user && user.as_json.merge({{ 'mail' => user.mail }})"
                                    existing_op_user = self.op_client.execute_json_query(ruby_expr)
                                except Exception:
                                    existing_op_user = None

                            if existing_op_user and existing_op_user.get("id"):
                                resolved_errors += 1
                                op_id = int(existing_op_user["id"])  # type: ignore[arg-type]
                                target_mapping["openproject_id"] = op_id
                                target_mapping["openproject_login"] = existing_op_user.get("login")
                                target_mapping["openproject_email"] = existing_op_user.get(
                                    "mail",
                                ) or existing_op_user.get(
                                    "email",
                                )
                                target_mapping["matched_by"] = "username_existing"

                                # Merge provenance data into cached OpenProject users
                                def _apply_provenance(
                                    op_user: dict[str, Any],
                                    meta: dict[str, Any] = origin_meta,
                                ) -> None:
                                    if meta.get("origin_system"):
                                        op_user["j2o_origin_system"] = meta.get("origin_system")
                                    if meta.get("user_id"):
                                        op_user["j2o_user_id"] = meta.get("user_id")
                                    if meta.get("user_key"):
                                        op_user["j2o_user_key"] = meta.get("user_key")
                                    if meta.get("external_url"):
                                        op_user["j2o_external_url"] = meta.get("external_url")
                                    if meta.get("time_zone"):
                                        op_user["time_zone"] = meta.get("time_zone")
                                    op_user.pop("jira_user_key", None)

                                existing_entry = next(
                                    (u for u in self.op_users if isinstance(u, dict) and u.get("id") == op_id),
                                    None,
                                )
                                if isinstance(existing_entry, dict):
                                    _apply_provenance(existing_entry)
                                elif isinstance(existing_op_user, dict):
                                    record = dict(existing_op_user)
                                    _apply_provenance(record)
                                    self.op_users.append(record)

                                self.logger.info(
                                    "Resolved duplicate for Jira user %s by linking to existing OpenProject account %s",
                                    jira_key,
                                    existing_op_user.get("login"),
                                )
                            else:
                                unresolved_indices.add(idx)

                        failed += len(unresolved_indices)

                        # Write the new OP id back to the mapping for each
                        # freshly-created user. Without this, downstream
                        # ``_map_user`` lookups in the WP migration treat
                        # these users as un-mapped (the file still has
                        # ``openproject_id: None`` from the initial probe)
                        # and silently drop ~half of all assignees.
                        _apply_created_user_ids_to_mapping(batch, meta, created_list)

                        # Build created_users payload to retain for summary (limited fields, no PII beyond login/mail)
                        for item in created_list:
                            idx = item.get("index")
                            if isinstance(idx, int) and 0 <= idx < len(meta):
                                m = meta[idx]
                                created_users.append(
                                    {
                                        "status": "success",
                                        "login": m.get("login"),
                                        "mail": m.get("mail"),
                                        "id": item.get("id"),
                                    },
                                )

                        # Log a few errors safely
                        if error_list and self.logger.isEnabledFor(logging.DEBUG):
                            for err in error_list[:3]:
                                idx = err.get("index")
                                safe_login = (
                                    meta[idx]["login"] if isinstance(idx, int) and 0 <= idx < len(meta) else "unknown"
                                )
                                self.logger.debug("User creation error: %s -> %s", safe_login, err.get("errors", []))

                        tracker.add_log_item(
                            f"Created {batch_created}/{len(batch)} users in batch",
                        )
                    except Exception as e:
                        error_msg = f"Exception during bulk user creation: {e!s}"
                        self.logger.exception(error_msg)
                        failed += len(batch)
                        tracker.add_log_item(
                            f"Exception during creation: {', '.join(batch_users)}",
                        )
                        raise MigrationError(error_msg) from e

                    tracker.increment(len(batch))

        # Summarize creation results clearly
        self.logger.info(
            "User creation summary: created=%s failed=%s total=%s",
            created,
            failed,
            total,
        )

        try:
            provenance_stats = self.backfill_user_origin_metadata()
            self.logger.info(
                "Backfilled user origin metadata: updated=%s errors=%s avatars_uploaded=%s",
                provenance_stats.get("updated", 0),
                provenance_stats.get("errors", 0),
                provenance_stats.get("avatars_uploaded", 0),
            )
        except Exception as e:
            self.logger.warning("Failed to backfill user origin metadata: %s", e)

        # Refresh OpenProject cache and mapping now that provenance has been updated
        self.extract_openproject_users(force=True)
        self.create_user_mapping()

        return {
            "created": created,
            "failed": failed,
            "total": total,
            "created_count": created,  # Add for test compatibility
            "created_users": created_users,
        }

    def backfill_user_origin_metadata(self) -> dict[str, int]:
        """Update origin custom fields and timezone for existing OpenProject users."""
        if not self.op_users:
            self.extract_openproject_users()
        if not self.user_mapping:
            self.create_user_mapping()

        cf_ids = self._get_user_origin_cf_ids()
        if not cf_ids:
            self.logger.warning("User origin custom fields missing; skipping backfill")
            return {"updated": 0, "errors": 1}

        op_users_by_id: dict[int, dict[str, Any]] = {}
        for op_user in self.op_users:
            try:
                uid = int(op_user.get("id"))
            except Exception:
                continue
            op_users_by_id[uid] = op_user

        jira_index = self._get_jira_user_index()

        rows: list[dict[str, Any]] = []
        avatar_jobs: list[dict[str, Any]] = []
        for mapping in self.user_mapping.values():
            op_id = mapping.get("openproject_id")
            jira_key = mapping.get("jira_key")
            if not op_id or not jira_key:
                continue

            try:
                op_id_int = int(op_id)
            except Exception:
                continue

            op_user = op_users_by_id.get(op_id_int)
            jira_user = jira_index.get(str(jira_key))
            if not jira_user:
                continue

            meta = self._build_user_origin_metadata(jira_user)
            if not any(meta.values()):
                continue

            custom_fields: list[dict[str, Any]] = []
            existing_origin_system = (op_user or {}).get("j2o_origin_system") if op_user else None
            if (
                meta["origin_system"]
                and meta["origin_system"] != existing_origin_system
                and cf_ids.get("J2O Origin System")
            ):
                custom_fields.append({"id": cf_ids["J2O Origin System"], "value": meta["origin_system"]})

            existing_user_id = (op_user or {}).get("j2o_user_id") if op_user else None
            if meta["user_id"] and meta["user_id"] != existing_user_id and cf_ids.get("J2O User ID"):
                custom_fields.append({"id": cf_ids["J2O User ID"], "value": meta["user_id"]})

            existing_user_key = (op_user or {}).get("j2o_user_key") if op_user else None
            if meta["user_key"] and meta["user_key"] != existing_user_key and cf_ids.get("J2O User Key"):
                custom_fields.append({"id": cf_ids["J2O User Key"], "value": meta["user_key"]})

            existing_url = (op_user or {}).get("j2o_external_url") if op_user else None
            if meta["external_url"] and meta["external_url"] != existing_url and cf_ids.get("J2O External URL"):
                custom_fields.append({"id": cf_ids["J2O External URL"], "value": meta["external_url"]})

            pref_payload = {}
            existing_tz = (op_user or {}).get("time_zone") if op_user else None
            user_type = str((op_user or {}).get("type", "")) if op_user else ""
            if meta["time_zone"] and meta["time_zone"] != existing_tz:
                if user_type not in {"DeletedUser", "SystemUser"}:
                    pref_payload = {"time_zone": meta["time_zone"]}

            language_code = self._map_locale_to_language(meta.get("locale"))
            existing_lang = (op_user or {}).get("language") if op_user else None
            if language_code and language_code != existing_lang:
                pref_payload = {**pref_payload, "language": language_code}

            avatar_job = self._prepare_avatar_job(
                jira_user=jira_user,
                op_user=op_user,
                mapping=mapping,
                meta=meta,
            )
            if avatar_job:
                avatar_jobs.append(avatar_job)

            if not custom_fields and not pref_payload:
                continue

            row: dict[str, Any] = {"id": op_id_int}
            if custom_fields:
                row["custom_fields"] = custom_fields
            if pref_payload:
                row["pref"] = pref_payload

            rows.append(row)

        if not rows:
            avatar_result = self._sync_user_avatars(avatar_jobs)
            return {
                "updated": 0,
                "errors": 0,
                "avatars_uploaded": avatar_result.get("uploaded", 0),
                "avatars_skipped": avatar_result.get("skipped", 0),
            }

        summary = self._persist_user_origin_updates(rows)
        avatar_result = self._sync_user_avatars(avatar_jobs)
        return {
            "updated": int(summary.get("updated", 0)),
            "errors": int(summary.get("errors", 0)),
            "avatars_uploaded": avatar_result.get("uploaded", 0),
            "avatars_skipped": avatar_result.get("skipped", 0),
        }

    def _persist_user_origin_updates(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        if not rows:
            return {"updated": 0, "errors": 0}

        batch_size = 50
        total_updated = 0
        total_errors = 0

        for i in range(0, len(rows), batch_size):
            chunk = rows[i : i + batch_size]
            payload_literal = json.dumps(chunk)
            ruby = (
                "require 'json'\n"
                f"rows = JSON.parse({json.dumps(payload_literal)})\n"
                "updated = 0\n"
                "errors = []\n"
                "rows.each do |row|\n"
                "  begin\n"
                "    user = User.find_by(id: row['id'])\n"
                "    next unless user && user.respond_to?(:custom_value_for)\n"
                "    touched = false\n"
                "    Array(row['custom_fields']).each do |cfh|\n"
                "      cf = CustomField.find_by(id: cfh['id'])\n"
                "      next unless cf\n"
                "      new_val = cfh['value']\n"
                "      current = user.custom_value_for(cf)&.value\n"
                "      next if (current || '').to_s == (new_val || '').to_s\n"
                "      cv = user.custom_value_for(cf)\n"
                "      if cv\n"
                "        cv.value = new_val\n"
                "        cv.save\n"
                "      else\n"
                "        values = user.custom_field_values || {}\n"
                "        values[cf.id] = new_val\n"
                "        user.custom_field_values = values\n"
                "        user.save\n"
                "      end\n"
                "      touched = true\n"
                "    end\n"
                "    pref_payload = row['pref'] || {}\n"
                "    tz = pref_payload['time_zone']\n"
                "    if tz && !tz.to_s.strip.empty? && user.respond_to?(:pref)\n"
                "      pref = user.pref\n"
                "      pref ||= user.respond_to?(:build_pref) ? user.build_pref : nil\n"
                "      if pref && pref.time_zone != tz\n"
                "        pref.time_zone = tz\n"
                "        pref.save\n"
                "        touched = true\n"
                "      end\n"
                "    end\n"
                "    updated += 1 if touched\n"
                "  rescue => e\n"
                "    errors << { id: row['id'], error: e.message }\n"
                "  end\n"
                "end\n"
                "{ updated: updated, errors: errors.length }\n"
            )

            summary = self.op_client.execute_query_to_json_file(ruby, timeout=180)
            total_updated += int(summary.get("updated", 0) or 0)
            total_errors += int(summary.get("errors", 0) or 0)

        return {"updated": total_updated, "errors": total_errors}

    def analyze_user_mapping(self) -> dict[str, Any]:
        """Analyze the user mapping for statistics and potential issues.

        Returns:
            Dictionary with analysis results

        Raises:
            MigrationError: If user mapping is missing

        """
        if not self.user_mapping:
            self.create_user_mapping()

        total_users = len(self.user_mapping)
        matched_by_username = len(
            [u for u in self.user_mapping.values() if u["matched_by"] == "username"],
        )
        matched_by_email = len(
            [u for u in self.user_mapping.values() if u["matched_by"] == "email"],
        )
        not_matched = len(
            [u for u in self.user_mapping.values() if u["matched_by"] == "none"],
        )

        analysis = {
            "total_users": total_users,
            "matched_by_username": matched_by_username,
            "matched_by_email": matched_by_email,
            "not_matched": not_matched,
            "username_match_percentage": ((matched_by_username / total_users) * 100 if total_users > 0 else 0),
            "email_match_percentage": ((matched_by_email / total_users) * 100 if total_users > 0 else 0),
            "total_match_percentage": (
                ((matched_by_username + matched_by_email) / total_users) * 100 if total_users > 0 else 0
            ),
            "not_matched_percentage": ((not_matched / total_users) * 100 if total_users > 0 else 0),
        }

        # Display the analysis
        self.logger.info("User mapping analysis:")
        self.logger.info("Total users: %s", total_users)
        self.logger.info(
            "Matched by username: %s (%s%%)",
            matched_by_username,
            analysis["username_match_percentage"],
        )
        self.logger.info(
            "Matched by email: %s (%s%%)",
            matched_by_email,
            analysis["email_match_percentage"],
        )
        self.logger.info(
            "Total matched: %s (%s%%)",
            matched_by_username + matched_by_email,
            analysis["total_match_percentage"],
        )
        self.logger.info(
            "Not matched: %s (%s%%)",
            not_matched,
            analysis["not_matched_percentage"],
        )

        return analysis

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        Args:
            entity_type: Type of entities to retrieve

        Returns:
            List of current entities from Jira

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        if entity_type == "users":
            return self.jira_client.get_users()
        msg = f"UserMigration does not support entity type: {entity_type}. Supported types: ['users']"
        raise ValueError(
            msg,
        )

    def run(self) -> ComponentResult:
        """Execute the complete user migration process."""
        self.logger.info("Starting user migration")

        try:
            # Always refresh upstream/downstream snapshots before mapping
            self.extract_jira_users(force=True)
            self.extract_openproject_users()
            self._get_user_origin_cf_ids()

            result = self.create_missing_users()
            # Consider success if we have results (even if 0 users needed creation)
            created = result.get("created", 0)
            total = result.get("total", 0)
            failed = result.get("failed", 0)

            # Backfill provenance metadata for all mapped users (existing + newly created)
            try:
                provenance_stats = self.backfill_user_origin_metadata()
                self.logger.info(
                    "Backfilled user origin metadata: updated=%s errors=%s avatars_uploaded=%s",
                    provenance_stats.get("updated", 0),
                    provenance_stats.get("errors", 0),
                    provenance_stats.get("avatars_uploaded", 0),
                )
            except Exception as e:
                self.logger.warning("Failed to backfill user origin metadata: %s", e)

            # Success if failure rate is below threshold (allow partial success for data quality issues)
            # Users with invalid last names in Jira cannot be migrated - this is a source data issue
            success_rate = (created / total * 100) if total > 0 else 100
            failure_threshold_pct = 15  # Allow up to 15% failure rate for data quality issues
            is_success = (100 - success_rate) <= failure_threshold_pct

            if failed > 0 and is_success:
                self.logger.warning(
                    "User migration completed with %d failures (%.1f%% success rate) - "
                    "failures likely due to invalid data in Jira (e.g., invalid last names)",
                    failed,
                    success_rate,
                )

            message = (
                f"User migration completed: {created}/{total} users created ({success_rate:.1f}%), {failed} failed"
            )

            return ComponentResult(
                success=is_success,
                message=message,
                data=result,
                success_count=created,
                failed_count=failed,
                total_count=total,
            )
        except Exception as e:
            self.logger.exception("User migration failed")
            return ComponentResult(
                component="users",
                status="failed",
                message=f"User migration failed: {e}",
                data={"error": str(e)},
            )
