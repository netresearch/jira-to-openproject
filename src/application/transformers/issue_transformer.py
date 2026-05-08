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

if TYPE_CHECKING:
    from src.domain.enums import JournalEntryType


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

    # Probe order for resolving Jira changelog/comment authors against the
    # owner's ``user_mapping``. Mirrors the multi-key fallback documented in
    # BUG #32 — the user_mapping is augmented with secondary indices on
    # ``name`` / ``displayName`` / ``emailAddress`` (and others) so any of
    # these raw Jira fields will resolve to the same OP user when present.
    _JOURNAL_AUTHOR_PROBE_KEYS: tuple[str, ...] = ("name", "displayName", "emailAddress")

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
            # Access changelog from issue
            changelog = getattr(jira_issue, "changelog", None)
            if not changelog:
                return None

            histories = getattr(changelog, "histories", None)
            if not histories:
                return None

            # Find all Workflow field changes, sorted by date (most recent last)
            workflow_changes = []
            for history in histories:
                items = getattr(history, "items", [])
                for item in items:
                    field = getattr(item, "field", None) or (item.get("field") if isinstance(item, dict) else None)
                    if field == "Workflow":
                        to_string = getattr(item, "toString", None) or (
                            item.get("toString") if isinstance(item, dict) else None
                        )
                        if to_string:
                            created = getattr(history, "created", "")
                            workflow_changes.append((created, to_string))

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
        owner = self._owner
        candidates: list[str] = list(owner.start_date_fields)

        # jira.Issue style access
        if hasattr(issue, "fields"):
            fields_obj = getattr(issue, "fields", None)
            for field_id in candidates:
                try:
                    value = getattr(fields_obj, field_id)
                except AttributeError:
                    value = None
                if value:
                    normalized = owner.enhanced_timestamp_migrator._normalize_timestamp(str(value))
                    if normalized:
                        return normalized.split("T", 1)[0]

        # Raw dict fields from jira.Issue
        raw_fields = {}
        if hasattr(issue, "raw"):
            raw_fields = getattr(issue, "raw", {}).get("fields", {})
        if isinstance(issue, dict):
            raw_fields = issue.get("fields", issue)

        if isinstance(raw_fields, dict):
            for field_id in candidates:
                value = raw_fields.get(field_id)
                if value:
                    normalized = owner.enhanced_timestamp_migrator._normalize_timestamp(str(value))
                    if normalized:
                        return normalized.split("T", 1)[0]

        # Fallback: derive start date from Jira status history
        history_start = self.resolve_start_date_from_history(issue)
        if history_start:
            return history_start

        return None

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
            items = self.get_attr(history, "items") or []
            for item in items:
                field_name = str(self.get_attr(item, "field") or "").lower()
                if field_name != "status":
                    continue

                status_id = str(self.get_attr(item, "to") or "").strip()
                status_name = str(self.get_attr(item, "toString") or "").strip().lower()

                category: dict[str, Any] = {}
                if status_id and status_id in owner.status_category_by_id:
                    category = owner.status_category_by_id[status_id] or {}
                elif status_name and status_name in owner.status_category_by_name:
                    category = owner.status_category_by_name[status_name] or {}

                if not category and status_name:
                    # Attempt loose lookup by name if exact match missing
                    category = next(
                        (val for key, val in owner.status_category_by_name.items() if key == status_name),
                        {},
                    )

                if self.is_in_progress_category(category):
                    return normalized.split("T", 1)[0]

        return None

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
        owner = self._owner
        field = item.get("field")
        if not field:
            return None

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
            "Fix Version": "version_id",  # On-the-fly version creation enabled
            # "component": "category_id",  # Requires category_mapping - falls through to Bug #16 notes for now
            "reporter": "author_id",
            # BUG #17 FIX: Time estimate fields (Jira stores in seconds, OpenProject in hours)
            "timeestimate": "remaining_hours",
            "timeoriginalestimate": "estimated_hours",
        }

        # BUG #32 FIX (REGRESSION #3): Skip unmapped fields to prevent invalid Journal::WorkPackageJournal attributes
        if field not in field_mappings:
            return None

        op_field = field_mappings[field]

        # Get values from changelog item
        from_value = item.get("fromString") or item.get("from")
        to_value = item.get("toString") or item.get("to")

        # Special handling for user fields (assignee, reporter)
        if field in ["assignee", "reporter"]:
            # BUG #20 FIX: Use 'from'/'to' (username) not 'fromString'/'toString' (display name)
            # Jira changelog provides:
            #   - from/to: username (e.g., "enrico.tischendorf")
            #   - fromString/toString: display name (e.g., "Enrico Tischendorf")
            # Our user_mapping is keyed by username, so we must use from/to
            from_username = item.get("from")
            to_username = item.get("to")

            # Map usernames to OpenProject IDs
            from_id = None
            to_id = None

            if from_username and owner.user_mapping:
                user_dict = owner.user_mapping.get(from_username)
                from_id = user_dict.get("openproject_id") if user_dict else None
                if not user_dict:
                    self.logger.debug(f"[BUG20] User not found in mapping: {from_username}")

            if to_username and owner.user_mapping:
                user_dict = owner.user_mapping.get(to_username)
                to_id = user_dict.get("openproject_id") if user_dict else None
                if not user_dict:
                    self.logger.debug(f"[BUG20] User not found in mapping: {to_username}")

            # BUG #19 FIX: Skip no-change mappings (from == to, including both None)
            # This prevents "retracted" phantom journals from operations with no actual change
            if from_id == to_id:
                return None

            return {op_field: [from_id, to_id]}

        # BUG #11 FIX: Map Jira IDs to OpenProject IDs for status, issuetype
        # The progressive state building needs OpenProject integer IDs, not Jira string values
        if field == "issuetype":
            # Get Jira type IDs (not string names)
            from_jira_id = item.get("from")  # e.g., "3" for Task
            to_jira_id = item.get("to")  # e.g., "10404" for Access

            # Map to OpenProject type IDs
            from_op_id = None
            to_op_id = None

            if from_jira_id and owner.issue_type_id_mapping:
                from_op_id = owner.issue_type_id_mapping.get(str(from_jira_id))
            if to_jira_id and owner.issue_type_id_mapping:
                to_op_id = owner.issue_type_id_mapping.get(str(to_jira_id))

            # BUG #11 DEBUG: Log type mapping results
            self.logger.info(
                f"[BUG11-TYPE] issuetype change: from_jira={from_jira_id} -> from_op={from_op_id}, to_jira={to_jira_id} -> to_op={to_op_id}",
            )

            # BUG #19 FIX: Skip no-change mappings
            if from_op_id == to_op_id:
                return None

            return {op_field: [from_op_id, to_op_id]}

        if field == "status":
            # Get Jira status IDs (not string names)
            from_jira_id = item.get("from")  # e.g., "1" for Open
            to_jira_id = item.get("to")  # e.g., "6" for Closed

            # Map to OpenProject status IDs
            from_op_id = None
            to_op_id = None

            if from_jira_id and owner.status_mapping:
                mapping = owner.status_mapping.get(str(from_jira_id))
                from_op_id = mapping.get("openproject_id") if mapping else None
            if to_jira_id and owner.status_mapping:
                mapping = owner.status_mapping.get(str(to_jira_id))
                to_op_id = mapping.get("openproject_id") if mapping else None

            # BUG #11 DEBUG: Log status mapping results
            self.logger.info(
                f"[BUG11-STATUS] status change: from_jira={from_jira_id} -> from_op={from_op_id}, to_jira={to_jira_id} -> to_op={to_op_id}",
            )

            # BUG #19 FIX: Skip no-change mappings
            if from_op_id == to_op_id:
                return None

            return {op_field: [from_op_id, to_op_id]}

        # Handle Fix Version -> version_id with on-the-fly version creation
        if field == "Fix Version":
            # Version names are in fromString/toString (like user display names)
            from_version_name = item.get("fromString")
            to_version_name = item.get("toString")

            # Map to OpenProject version IDs (create if needed)
            from_version_id = None
            to_version_id = None

            if owner._current_project_id:
                if from_version_name:
                    from_version_id = owner._get_or_create_version(from_version_name, owner._current_project_id)
                if to_version_name:
                    to_version_id = owner._get_or_create_version(to_version_name, owner._current_project_id)

            self.logger.info(
                f"[FIXVERSION] project_id={owner._current_project_id}, from='{from_version_name}' -> {from_version_id}, to='{to_version_name}' -> {to_version_id}",
            )

            # Skip no-change mappings
            if from_version_id == to_version_id:
                return None

            return {op_field: [from_version_id, to_version_id]}

        # For priority, keep string values for now (mapping not critical for journals)
        if field == "priority":
            # BUG #19 FIX: Skip no-change mappings
            if from_value == to_value:
                return None
            return {op_field: [from_value, to_value]}

        # BUG #17 FIX: Handle time estimate fields - convert Jira seconds to OpenProject hours
        if field in ["timeestimate", "timeoriginalestimate"]:
            from_seconds = item.get("from")
            to_seconds = item.get("to")

            def seconds_to_hours(seconds_str: str | None) -> float | None:
                """Convert Jira time (seconds) to OpenProject hours."""
                if not seconds_str:
                    return None
                try:
                    seconds = int(seconds_str)
                    return round(seconds / 3600, 2)  # Convert to hours with 2 decimal places
                except (
                    ValueError,
                    TypeError,
                ):
                    return None

            from_hours = seconds_to_hours(from_seconds)
            to_hours = seconds_to_hours(to_seconds)

            self.logger.info(
                f"[BUG17-TIME] {field} change: from={from_seconds}s ({from_hours}h) -> to={to_seconds}s ({to_hours}h)",
            )

            # BUG #19 FIX: Skip no-change mappings
            if from_hours == to_hours:
                return None

            return {op_field: [from_hours, to_hours]}

        # Generic field change (subject, description, etc.)
        # BUG #19 FIX: Skip no-change mappings
        if from_value == to_value:
            return None
        return {op_field: [from_value, to_value]}

    @staticmethod
    def extract_changelog_histories(issue: Any) -> list[Any]:
        """Return changelog histories from either jira.Issue or dict payloads."""
        if hasattr(issue, "changelog") and issue.changelog:
            histories = getattr(issue.changelog, "histories", None)
            if histories:
                return list(histories)

        raw = getattr(issue, "raw", None)
        if isinstance(raw, dict):
            histories = raw.get("changelog", {}).get("histories")
            if isinstance(histories, list):
                return histories

        if isinstance(issue, dict):
            histories = issue.get("changelog", {}).get("histories")
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
            # Handle jira.Issue style
            if hasattr(issue, "key") and hasattr(issue, "fields"):
                f = getattr(issue, "fields", None)
                meta["jira_key"] = getattr(issue, "key", None)
                meta["jira_id"] = getattr(issue, "id", None)
                if f is not None:

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
            else:
                # Dict-like payloads (tests / fallback)
                d = issue or {}
                fields = d.get("fields") if isinstance(d, dict) else {}
                meta["jira_key"] = d.get("key") if isinstance(d, dict) else None
                meta["jira_id"] = d.get("id") if isinstance(d, dict) else None

                def _get(path_keys: list[str]) -> Any:
                    cur: Any = fields if isinstance(fields, dict) else {}
                    for k in path_keys:
                        if not isinstance(cur, dict):
                            return None
                        cur = cur.get(k)
                    return cur

                meta["issuetype_id"] = _get(["issuetype", "id"])
                meta["issuetype_name"] = _get(["issuetype", "name"])
                meta["status_id"] = _get(["status", "id"])
                meta["status_name"] = _get(["status", "name"])
                meta["priority_id"] = _get(["priority", "id"])
                meta["priority_name"] = _get(["priority", "name"])
                meta["reporter"] = _get(["reporter", "name"]) or _get(["reporter", "displayName"])
                meta["assignee"] = _get(["assignee", "name"]) or _get(["assignee", "displayName"])
                meta["created"] = _get(["created"]) or d.get("created")
                meta["updated"] = _get(["updated"]) or d.get("updated")
                meta["duedate"] = _get(["duedate"]) or d.get("duedate")
                labels = _get(["labels"]) or []
                if not isinstance(labels, list):
                    labels = []
                meta["labels"] = labels
                comps = _get(["components"]) or []
                if isinstance(comps, list):
                    meta["components"] = [c.get("name") for c in comps if isinstance(c, dict)]
                else:
                    meta["components"] = []
                parent = _get(["parent"]) or {}
                if isinstance(parent, dict):
                    meta["parent_key"] = parent.get("key")
        except Exception:
            # Never fail migration because of meta extraction
            pass
        return meta

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

        owner = self._owner

        # Try to find in mapping by ID
        if status_id and owner.status_mapping and str(status_id) in owner.status_mapping:
            mapped_id = owner.status_mapping[str(status_id)].get("openproject_id")
            if mapped_id:
                return mapped_id

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
        # Ensure string values for certain fields
        if "subject" in wp:
            try:
                wp["subject"] = str(wp["subject"]).replace('"', '\\"').replace("'", "\\'")
            except Exception:
                wp["subject"] = str(wp.get("subject", ""))
        if "description" in wp:
            try:
                wp["description"] = str(wp["description"]).replace('"', '\\"').replace("'", "\\'")
            except Exception:
                wp["description"] = str(wp.get("description", ""))

        # Sanitize OpenProject API-style links that are not valid AR attributes
        links = wp.get("_links")
        if isinstance(links, dict):
            # Extract type_id from links if present and not already provided
            try:
                if "type_id" not in wp and isinstance(links.get("type"), dict):
                    href = links["type"].get("href")
                    if isinstance(href, str) and href.strip():
                        type_id_str = href.rstrip("/").split("/")[-1]
                        if type_id_str.isdigit():
                            wp["type_id"] = int(type_id_str)
            except Exception:
                pass

            # Extract status_id from links if present and not already provided
            try:
                if "status_id" not in wp and isinstance(links.get("status"), dict):
                    href = links["status"].get("href")
                    if isinstance(href, str) and href.strip():
                        status_id_str = href.rstrip("/").split("/")[-1]
                        if status_id_str.isdigit():
                            wp["status_id"] = int(status_id_str)
            except Exception:
                pass

        # Remove _links entirely to avoid AR unknown attribute errors
        wp.pop("_links", None)
        # Remove non-AR/meta keys that must not reach Rails mass-assignment
        wp.pop("watcher_ids", None)
        wp.pop("jira_id", None)
        wp.pop("jira_key", None)
        wp.pop("type_name", None)


__all__ = ["IssueTransformer"]
