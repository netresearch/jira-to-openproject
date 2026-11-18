# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Pure mapping logic extracted from :mod:`src.application.components.work_package_migration`.

This module is **Phase 1** of the ``WorkPackageMigration`` god-class
decomposition: pure Jira → OpenProject data shape transformations live
here so they can be tested in isolation, while the service module keeps
its orchestration role.

Sixteen methods that perform Jira → OpenProject data shape transformations
have been moved here verbatim. The original
:class:`~src.application.components.work_package_migration.WorkPackageMigration`
keeps thin delegate methods that forward to an :class:`IssueTransformer`
instance constructed at the end of its ``__init__`` so that **behaviour is
unchanged**.

Why an owner reference rather than constructor injection of every value?
=======================================================================
Several of the values these methods read are reassigned (not just mutated)
on the Service after construction — most notably
``WorkPackageMigration.markdown_converter`` is rebuilt by
``_update_markdown_converter_mappings``. To preserve the historical
behaviour where the live Service attribute is read at call time, the
transformer keeps a back-reference to its owner and reads
``self._owner.<attr>`` on demand.

The transformer remains independently testable: tests can construct an
:class:`IssueTransformer` directly with a lightweight stand-in owner
(``types.SimpleNamespace`` is sufficient — see
``tests/unit/test_issue_transformer.py``) without instantiating the full
``WorkPackageMigration``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.infrastructure.jira.comment_audience import may_copy_history_item

if TYPE_CHECKING:
    from src.domain.enums import JournalEntryType


def _dict_fields(value: Any) -> dict[str, Any]:
    """Read optional Jira fields from dictionary-shaped REST data."""
    return value if isinstance(value, dict) else {}


class IssueTransformer:
    """Pure mapping logic extracted from ``WorkPackageMigration``.

    Sixteen pure-mapping methods (see module docstring) live here so they
    can be unit-tested without instantiating the full migration component.
    The implementation is a verbatim move from the original class — only
    ``self.<attr>`` reads have been rewired to ``self._owner.<attr>`` for
    Service-owned mutable state.
    """

    # Default fallback user id used when a journal author cannot be resolved
    # from the user mapping. See ``resolve_journal_author_id`` (BUG #32).
    _BUG32_FALLBACK_USER_ID = 148941
    _FIX_VERSION_FIELD = "Fix Version"

    # Probe order for resolving Jira changelog/comment authors against the
    # owner's ``user_mapping``.  Canonical Cloud-first order mirrors
    # ``_resolve_jira_user_id`` and ``WorkPackageMigration._map_user``:
    # accountId (Cloud) → name (Server) → key (Server/DC) → emailAddress → displayName.
    # The user_mapping is augmented with secondary indices on all of these
    # fields so any present raw Jira field resolves to the same OP user.
    _JOURNAL_AUTHOR_PROBE_KEYS: tuple[str, ...] = (
        "accountId",
        "name",
        "key",
        "emailAddress",
        "displayName",
    )

    def __init__(self, owner: Any) -> None:
        """Bind the transformer to its owning migration service.

        Args:
            owner: The :class:`WorkPackageMigration` instance whose live
                attributes (``user_mapping``, ``status_mapping``,
                ``enhanced_timestamp_migrator``,
                ``markdown_converter``, ``logger``, etc.) the transformer
                reads on every call. Tests may pass a
                ``types.SimpleNamespace`` exposing the same attributes.

        """
        self._owner = owner

    # ------------------------------------------------------------------ #
    # Helpers / convenience accessors
    # ------------------------------------------------------------------ #

    @property
    def logger(self) -> Any:
        """Live logger from the owning service."""
        return self._owner.logger

    # ------------------------------------------------------------------ #
    # Journal author resolution (BUG #32)
    # ------------------------------------------------------------------ #

    def resolve_journal_author_id(
        self,
        author_data: dict[str, Any] | None,
        jira_key: str,
        kind: JournalEntryType,
    ) -> int:
        """Resolve a journal-entry author to an OpenProject user id.

        Walks ``author_data`` against the owner's ``user_mapping`` using
        the canonical probe order in
        :attr:`_JOURNAL_AUTHOR_PROBE_KEYS` and returns the first matching
        ``openproject_id``. If no probe key resolves, returns
        :attr:`_BUG32_FALLBACK_USER_ID` and logs a ``[BUG32]`` warning
        listing every probe field that was present on ``author_data`` so
        unresolved authors are diagnosable from production logs.

        Args:
            author_data: Raw Jira author payload from a comment or
                changelog entry. Tolerates ``None``/empty inputs by
                resolving to the fallback user.
            jira_key: Jira issue key, used purely for log context.
            kind: Discriminator for the journal entry (``COMMENT`` or
                ``CHANGELOG``) used to distinguish the warning message
                between callers.

        Returns:
            The resolved ``openproject_id`` (int) or the BUG #32
            fallback user id when the author cannot be resolved.

        """
        if not isinstance(author_data, dict):
            author_data = {}
        user_mapping = self._owner.user_mapping
        for key in self._JOURNAL_AUTHOR_PROBE_KEYS:
            value = author_data.get(key)
            if not value:
                continue
            user_dict = user_mapping.get(value)
            if not user_dict:
                continue
            op_id = user_dict.get("openproject_id")
            if op_id:
                self.logger.debug(
                    f"{jira_key}: Found user via {key}: {value} → {op_id}",
                )
                return int(op_id)

        # No probe key resolved — fall back to the BUG #32 admin user.
        attempted_fields = {k: author_data.get(k) for k in self._JOURNAL_AUTHOR_PROBE_KEYS if k in author_data}
        fallback_id = self._BUG32_FALLBACK_USER_ID
        self.logger.warning(
            f"[BUG32] {jira_key}: User not found in mapping for {kind} (tried: {attempted_fields}), "
            f"using fallback user {fallback_id}",
        )
        return fallback_id

    # ------------------------------------------------------------------ #
    # Mention tracking
    # ------------------------------------------------------------------ #

    def track_mentioned_users(self, text: str | None, project_id: int) -> None:
        """Extract mentioned user IDs from text and track them for membership assignment.

        Args:
            text: Jira markup text that may contain user mentions.
            project_id: OpenProject project ID where mentions were found.

        """
        if not text or not project_id:
            return

        owner = self._owner
        if not hasattr(owner, "markdown_converter") or not owner.markdown_converter:
            return

        try:
            mentioned_ids = owner.markdown_converter.extract_mentioned_user_ids(text)
            if mentioned_ids:
                if project_id not in owner._mentioned_users_by_project:
                    owner._mentioned_users_by_project[project_id] = set()
                owner._mentioned_users_by_project[project_id].update(mentioned_ids)
        except Exception as e:
            self.logger.debug(f"Error extracting mentioned users: {e}")

    # ------------------------------------------------------------------ #
    # Workflow extraction
    # ------------------------------------------------------------------ #

    def extract_final_workflow(self, jira_issue: Any) -> str | None:
        """Extract the final/current workflow scheme name from Jira changelog.

        The Jira "Workflow" field in changelog represents workflow scheme changes
        (not status changes). We extract the most recent "toString" value to get
        the current workflow scheme name.

        Args:
            jira_issue: The Jira issue object.

        Returns:
            Final workflow scheme name, or None if not found.

        """
        try:
            histories = getattr(getattr(jira_issue, "changelog", None), "histories", None) or []
            workflow_changes = [
                (getattr(history, "created", ""), name)
                for history in histories
                for item in self._copyable_history_items(history)
                if self.get_attr(item, "field") == "Workflow" and (name := self.get_attr(item, "toString"))
            ]
            if workflow_changes:
                # Sort by timestamp and get the most recent
                workflow_changes.sort(key=lambda x: x[0])
                final_workflow = workflow_changes[-1][1]
                self.logger.debug(f"[WORKFLOW] Extracted final workflow scheme: {final_workflow}")
                return str(final_workflow)

        except Exception as e:
            self.logger.debug(f"[WORKFLOW] Error extracting workflow: {e}")

        return None

    # ------------------------------------------------------------------ #
    # Datetime parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def parse_datetime(value: Any) -> datetime | None:
        """Best-effort parsing for Jira/ISO datetime payloads."""
        if isinstance(value, datetime):
            if value.tzinfo:
                return value.astimezone(UTC)
            return value.replace(tzinfo=UTC)

        if not isinstance(value, str):
            return None

        candidate = value.strip()
        if not candidate:
            return None

        # Normalize Z suffix to ISO compatible form
        candidate_iso = candidate.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate_iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
            return parsed
        except ValueError:
            pass

        patterns = [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M",
        ]
        for pattern in patterns:
            try:
                parsed = datetime.strptime(candidate, pattern)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                else:
                    parsed = parsed.astimezone(UTC)
                return parsed
            except ValueError:
                continue
        return None

    def derive_snapshot_timestamp(self, snapshot: list[dict[str, Any]] | None) -> datetime | None:
        """Derive the most recent migration timestamp from existing OpenProject rows."""
        latest: datetime | None = None
        for entry in snapshot or []:
            if not isinstance(entry, dict):
                continue
            for key in ("jira_migration_date", "updated_at", "updated_at_utc"):
                candidate = entry.get(key)
                parsed = self.parse_datetime(candidate)
                if parsed and (latest is None or parsed > latest):
                    latest = parsed
        return latest

    @staticmethod
    def build_key_exclusion_clause(existing_keys: set[str]) -> str | None:
        """Return a JQL fragment for excluding already-migrated issue keys."""
        if not existing_keys:
            return None
        limited = [key for key in sorted({k.strip() for k in existing_keys if k and k.strip()}) if key][
            :200
        ]  # Keep under 8KB URL limit (200 keys × ~10 chars = ~2KB)
        if not limited:
            return None
        return f"key NOT IN ({','.join(limited)})"

    # ------------------------------------------------------------------ #
    # Start date resolution
    # ------------------------------------------------------------------ #

    def resolve_start_date(self, issue: Any) -> str | None:
        """Resolve start date from configured Jira custom fields."""
        candidates = list(self._owner.start_date_fields)
        fields = getattr(issue, "fields", None)
        for field_id in candidates:
            date = self._start_date_value(getattr(fields, field_id, None))
            if date:
                return date

        raw_fields = (
            issue.get("fields", issue) if isinstance(issue, dict) else getattr(issue, "raw", {}).get("fields", {})
        )
        if isinstance(raw_fields, dict):
            for field_id in candidates:
                date = self._start_date_value(raw_fields.get(field_id))
                if date:
                    return date

        return self.resolve_start_date_from_history(issue)

    def _start_date_value(self, value: Any) -> str | None:
        normalized = self._owner.enhanced_timestamp_migrator._normalize_timestamp(str(value)) if value else None
        return normalized.split("T", 1)[0] if normalized else None

    def resolve_start_date_from_history(self, issue: Any) -> str | None:
        """Infer start date from the first transition into an 'In Progress' category."""
        owner = self._owner
        histories = self.extract_changelog_histories(issue)
        if not histories:
            return None

        # Sort histories chronologically (oldest first) using their created timestamp
        normalized_histories: list[tuple[str, Any]] = []
        for history in histories:
            created_raw = self.get_attr(history, "created")
            if not created_raw:
                continue
            normalized = owner.enhanced_timestamp_migrator._normalize_timestamp(str(created_raw))
            if not normalized:
                continue
            normalized_histories.append((normalized, history))

        normalized_histories.sort(key=lambda pair: pair[0])

        for normalized, history in normalized_histories:
            items = self._copyable_history_items(history)
            if any(self._enters_in_progress(item) for item in items):
                return normalized.split("T", 1)[0]
        return None

    def _enters_in_progress(self, item: Any) -> bool:
        if str(self.get_attr(item, "field") or "").lower() != "status":
            return False
        owner = self._owner
        status_id = str(self.get_attr(item, "to") or "").strip()
        status_name = str(self.get_attr(item, "toString") or "").strip().lower()
        category = owner.status_category_by_id.get(status_id) if status_id else None
        if not category and status_name:
            category = owner.status_category_by_name.get(status_name)
        return self.is_in_progress_category(category or {})

    def _copyable_history_items(self, history: Any) -> list[Any]:
        """Keep the original audience available when deriving historical values."""
        return [item for item in self.get_attr(history, "items") or [] if may_copy_history_item(item, history)]

    @staticmethod
    def get_attr(obj: Any, key: str) -> Any:
        """Safely fetch attribute/key from Jira objects or dicts."""
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    # ------------------------------------------------------------------ #
    # Changelog processing
    # ------------------------------------------------------------------ #

    def process_changelog_item(self, item: dict[str, Any]) -> dict[str, list[Any]] | None:
        """Process a single changelog item into OpenProject field changes.

        Bug #28 fix: Transform Jira changelog entries into structured field changes
        for journal.data instead of text-only comments.

        Args:
            item: Jira changelog item with 'field', 'fromString', 'toString'.

        Returns:
            Dictionary mapping OpenProject field names to [old_value, new_value] or None.

        """
        if not may_copy_history_item(item):
            return None
        field = item.get("field")
        # Field mapping from Jira to OpenProject
        # BUG #32 FIX (REGRESSION #3): Only map fields that exist in Journal::WorkPackageJournal
        # BUG #17 FIX: Added timeestimate and timeoriginalestimate mappings
        # fixVersion: Enabled with on-the-fly version creation
        field_mappings = {
            "summary": "subject",
            "description": "description",
            "status": "status_id",
            "assignee": "assigned_to_id",
            "priority": "priority_id",
            "issuetype": "type_id",
            # "resolution": "resolution",  # NOT a valid Journal::WorkPackageJournal attribute  # noqa: ERA001
            # "labels": "tags",  # NOT a valid Journal::WorkPackageJournal attribute  # noqa: ERA001
            self._FIX_VERSION_FIELD: "version_id",  # On-the-fly version creation enabled
            # "component": "category_id",  # Requires category_mapping - falls through to Bug #16 notes for now
            "reporter": "author_id",
            # BUG #17 FIX: Time estimate fields (Jira stores in seconds, OpenProject in hours)
            "timeestimate": "remaining_hours",
            "timeoriginalestimate": "estimated_hours",
        }

        # BUG #32 FIX (REGRESSION #3): Skip unmapped fields to prevent invalid Journal::WorkPackageJournal attributes
        if not field or field not in field_mappings:
            return None

        op_field = field_mappings[field]

        old = self._journal_value(field, item.get("from"), item.get("fromString"))
        new = self._journal_value(field, item.get("to"), item.get("toString"))
        if field in ("issuetype", "status", self._FIX_VERSION_FIELD, "timeestimate", "timeoriginalestimate"):
            self.logger.info("Mapped Jira %s change to %s: %r -> %r", field, op_field, old, new)
        if old == new:
            return None
        return {op_field: [old, new]}

    def _journal_value(self, field: str, raw: Any, display: Any) -> Any:
        """Choose the original Jira field representation appropriate for the journal column."""
        owner = self._owner
        if field in ("assignee", "reporter"):
            return self._journal_user_id(raw)
        if field == "issuetype":
            return owner.issue_type_id_mapping.get(str(raw)) if raw and owner.issue_type_id_mapping else None
        if field == "status":
            return self.mapped_status_id(raw)
        if field == self._FIX_VERSION_FIELD:
            if display and owner._current_project_id:
                return owner._get_or_create_version(display, owner._current_project_id)
            return None
        if field in ("timeestimate", "timeoriginalestimate"):
            return self._hours_from_seconds(raw)
        return display or raw

    def _journal_user_id(self, username: Any) -> Any:
        mapping = self._owner.user_mapping
        if not username or not mapping:
            return None
        user = mapping.get(username)
        if not user:
            self.logger.debug("[BUG20] User not found in mapping: %s", username)
            return None
        return user.get("openproject_id")

    @staticmethod
    def _hours_from_seconds(raw: Any) -> float | None:
        if not raw:
            return None
        try:
            return round(int(raw) / 3600, 2)
        except TypeError, ValueError:
            return None

    @staticmethod
    def extract_changelog_histories(issue: Any) -> list[Any]:
        """Return changelog histories from either jira.Issue or dict payloads."""
        if hasattr(issue, "changelog") and issue.changelog:
            histories = getattr(issue.changelog, "histories", None)
            if histories:
                return list(histories)

        for source in (getattr(issue, "raw", None), issue):
            page = _dict_fields(source).get("changelog")
            histories = _dict_fields(page).get("histories")
            if isinstance(histories, list):
                return histories

        return []

    @staticmethod
    def is_in_progress_category(category: dict[str, Any]) -> bool:
        """Return True when the status category represents 'In Progress'."""
        if not category:
            return False

        key = str(category.get("key", "")).lower()
        name = str(category.get("name", "")).lower()
        cat_id = str(category.get("id", "")).lower()

        in_progress_keys = {"indeterminate", "in_progress", "in-progress"}
        if key in in_progress_keys:
            return True
        if name == "in progress":
            return True
        if cat_id == "4":  # Jira default id for In Progress category
            return True
        return False

    # ------------------------------------------------------------------ #
    # Issue meta
    # ------------------------------------------------------------------ #

    def extract_issue_meta(self, issue: Any) -> dict[str, Any]:
        """Extract non-AR metadata from a Jira issue for reporting.

        Safe best-effort extraction from either jira.Issue or dict-like payloads.
        Does not mutate inputs and never raises.
        """
        meta: dict[str, Any] = {}
        try:
            start_date = self.resolve_start_date(issue)
            if start_date:
                meta["start_date"] = start_date
            if hasattr(issue, "key") and hasattr(issue, "fields"):
                self._object_issue_meta(issue, meta)
            else:
                self._mapping_issue_meta(issue, meta)
        except Exception:
            # Never fail migration because of meta extraction
            pass
        return meta

    @staticmethod
    def _object_issue_meta(issue: Any, meta: dict[str, Any]) -> None:
        f = getattr(issue, "fields", None)
        meta["jira_key"] = getattr(issue, "key", None)
        meta["jira_id"] = getattr(issue, "id", None)
        if f is None:
            return

        def _name(obj: Any) -> Any:
            try:
                return getattr(obj, "name", None)
            except Exception:
                return None

        meta["issuetype_id"] = getattr(getattr(f, "issuetype", None), "id", None)
        meta["issuetype_name"] = _name(getattr(f, "issuetype", None))
        meta["status_id"] = getattr(getattr(f, "status", None), "id", None)
        meta["status_name"] = _name(getattr(f, "status", None))
        meta["priority_id"] = getattr(getattr(f, "priority", None), "id", None)
        meta["priority_name"] = _name(getattr(f, "priority", None))
        meta["reporter"] = getattr(getattr(f, "reporter", None), "name", None)
        meta["assignee"] = getattr(getattr(f, "assignee", None), "name", None)
        meta["created"] = getattr(f, "created", None)
        meta["updated"] = getattr(f, "updated", None)
        meta["duedate"] = getattr(f, "duedate", None)
        try:
            labels = list(getattr(f, "labels", []) or [])
        except Exception:
            labels = []
        meta["labels"] = labels
        try:
            comps = getattr(f, "components", []) or []
            meta["components"] = [getattr(c, "name", None) for c in comps if c]
        except Exception:
            meta["components"] = []
        # Optional relations
        try:
            parent = getattr(f, "parent", None)
            if parent is not None:
                meta["parent_key"] = getattr(parent, "key", None)
        except Exception:
            pass

    @staticmethod
    def _mapping_issue_meta(issue: Any, meta: dict[str, Any]) -> None:
        # Dict-like payloads (tests / fallback)
        d = issue or {}
        record = _dict_fields(d)
        fields = _dict_fields(record.get("fields"))
        meta["jira_key"] = record.get("key")
        meta["jira_id"] = record.get("id")
        for name in ("issuetype", "status", "priority"):
            obj = _dict_fields(fields.get(name))
            meta[f"{name}_id"] = obj.get("id")
            meta[f"{name}_name"] = obj.get("name")
        for name in ("reporter", "assignee"):
            obj = _dict_fields(fields.get(name))
            meta[name] = obj.get("name") or obj.get("displayName")
        for name in ("created", "updated", "duedate"):
            meta[name] = fields.get(name) or d.get(name)
        labels = fields.get("labels") or []
        if not isinstance(labels, list):
            labels = []
        meta["labels"] = labels
        comps = fields.get("components") or []
        if isinstance(comps, list):
            meta["components"] = [c.get("name") for c in comps if isinstance(c, dict)]
        else:
            meta["components"] = []
        parent = fields.get("parent") or {}
        if isinstance(parent, dict):
            meta["parent_key"] = parent.get("key")

    # ------------------------------------------------------------------ #
    # Issue-type / status mapping
    # ------------------------------------------------------------------ #

    def map_issue_type(
        self,
        type_id: str | None = None,
        type_name: str | None = None,
    ) -> int:
        """Map Jira issue type to OpenProject type ID."""
        if not type_id and not type_name:
            msg = "Either type_id or type_name must be provided for issue type mapping"
            raise ValueError(
                msg,
            )

        owner = self._owner

        # Try to find in mapping by ID
        if type_id and owner.issue_type_id_mapping and str(type_id) in owner.issue_type_id_mapping:
            return owner.issue_type_id_mapping[str(type_id)]

        # Try to find in mapping by ID in issue_type_mapping
        if type_id and str(type_id) in owner.issue_type_mapping:
            mapped_id = owner.issue_type_mapping[str(type_id)].get("openproject_id")
            if mapped_id:
                return mapped_id

        # Default to Task (typically ID 1 in OpenProject)
        type_display = type_name or "Unknown"
        self.logger.warning(
            f"No mapping found for issue type {type_display} (ID: {type_id}), defaulting to Task",
        )
        return 1

    def mapped_status_id(self, source_id: Any) -> int | None:
        """Look up a target ID in scalar and record-shaped Jira status mappings."""
        if not source_id:
            return None
        entry = (self._owner.status_mapping or {}).get(str(source_id))
        if isinstance(entry, dict):
            entry = entry.get("openproject_id") or entry.get("id")
        if isinstance(entry, str) and entry.isdecimal():
            entry = int(entry)
        return entry if type(entry) is int and entry > 0 else None

    def map_status(
        self,
        status_id: str | None = None,
        status_name: str | None = None,
    ) -> int:
        """Map Jira status to OpenProject status ID."""
        if not status_id and not status_name:
            msg = "Either status_id or status_name must be provided for status mapping"
            raise ValueError(
                msg,
            )

        mapped = self.mapped_status_id(status_id)
        if mapped is not None:
            return mapped

        # Default to "New" status (typically ID 1 in OpenProject)
        status_display = status_name or "Unknown"
        self.logger.warning(
            f"No mapping found for status {status_display} (ID: {status_id}), defaulting to New",
        )
        return 1

    # ------------------------------------------------------------------ #
    # WP dict sanitisation
    # ------------------------------------------------------------------ #

    @staticmethod
    def sanitize_wp_dict(wp: dict[str, Any]) -> None:
        """Sanitize a prepared work package dict in-place for AR compatibility.

        - Extract type_id and status_id from API-style _links if provided.
        - Remove the _links key entirely to avoid unknown attribute errors.
        - Ensure string fields are properly escaped.
        """
        for field in ("subject", "description"):
            if field in wp:
                try:
                    wp[field] = str(wp[field]).replace('"', '\\"').replace("'", "\\'")
                except Exception:
                    wp[field] = str(wp.get(field, ""))

        links = wp.get("_links")
        if isinstance(links, dict):
            for relation in ("type", "status"):
                IssueTransformer._copy_link_id(wp, links, relation)

        # Remove _links entirely to avoid AR unknown attribute errors
        wp.pop("_links", None)
        # Remove non-AR/meta keys that must not reach Rails mass-assignment
        wp.pop("watcher_ids", None)
        wp.pop("jira_id", None)
        wp.pop("jira_key", None)
        wp.pop("type_name", None)

    @staticmethod
    def _copy_link_id(wp: dict[str, Any], links: dict, relation: str) -> None:
        key = f"{relation}_id"
        link = links.get(relation)
        if key in wp or not isinstance(link, dict):
            return
        href = link.get("href")
        if not isinstance(href, str):
            return
        identifier = href.rstrip("/").rsplit("/", 1)[-1]
        if identifier.isdigit():
            try:
                wp[key] = int(identifier)
            except ValueError:
                pass


__all__ = ["IssueTransformer"]
