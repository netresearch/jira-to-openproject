"""Backfill ``user_mapping`` for Jira identities not in ``/rest/api/2/users``.

The ``users`` component populates ``user_mapping.json`` by iterating
``/rest/api/2/users``, which excludes locked / inactive accounts.
Anyone who watched an issue / authored a comment / was assigned a
worklog under one of those locked accounts then drops out as
``user_unmapped`` in :class:`WatcherMigration`,
:class:`TimeEntryMigration`, etc. — silent loss on every run.

This is a **backfill** in the literal sense the operator described:
on a follow-up run, retroactively fold *new* identities (new in Jira
*or* newly-discovered via richer issue data) into the existing
mapping so the rest of the run can resolve them.

Sources of candidate identities, in priority order:

1. **Previous run's ``unmapped_users`` lists** — the watcher and TE
   migrations record every distinct identity they couldn't resolve
   in ``ComponentResult.details["unmapped_users"]``. The latest
   ``migration_results_*.json`` carries that breadcrumb forward.
2. **Cached Jira issues** — when ``jira_issues_cache.json`` is
   present, discover identities from ``fields.assignee``,
   ``fields.reporter``, ``fields.watches.watchers`` and
   ``fields.comment.comments[*].author``. (This is the same
   discovery PR #196 added to ``user_migration``; running it in a
   dedicated component ensures it executes regardless of whether
   ``users`` was skipped by change-detection.)

For each candidate:

* Skip if already in the mapping under any identifier.
* Query Jira for the full profile.
* Probe OP via ``get_user(login)`` → ``get_user_by_email(email)``.
* If found → write to ``user_mapping`` under every identifier
  (name / key / accountId / emailAddress) so the multi-probe
  resolver in the consumers can reach it from any of them.
* If not found in OP → record under ``not_found_in_op`` for
  operator triage.

Idempotent: re-runs leave already-mapped names untouched and never
clobber operator-set entries on an alternate identifier.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.exceptions import RecordNotFoundError
from src.infrastructure.jira.jira_client import JiraClient, JiraResourceNotFoundError
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult


@register_entity_types("user_mapping_backfill")
class UserMappingBackfillMigration(BaseMigration):
    """Phase: pull missing Jira identities into ``user_mapping``."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        msg = (
            "UserMappingBackfillMigration is a transformation-only migration"
            " and does not support idempotent workflow. It operates on the"
            " persisted user_mapping + previous migration_results / cached issues."
        )
        raise ValueError(msg)

    @staticmethod
    def _names_from_previous_results(results_dir: Path) -> set[str]:
        """Walk every ``migration_results_*.json`` in ``results_dir`` and
        union the ``unmapped_users`` from every component's details.

        Reading every file (not just the latest) is intentional: a
        backfill might span several runs in which different sets of
        users showed up unmapped, and there's no guarantee the
        latest file has the union — only the most recent run's
        breadcrumb. Each run's file is small (≪1 MB), so the cost is
        negligible.
        """
        out: set[str] = set()
        if not results_dir.exists():
            return out
        for path in sorted(results_dir.glob("migration_results_*.json")):
            try:
                with path.open() as f:
                    d = json.load(f)
            except OSError, json.JSONDecodeError:
                continue
            comps = d.get("components") or d.get("component_results") or {}
            if not isinstance(comps, dict):
                continue
            for comp in comps.values():
                if not isinstance(comp, dict):
                    continue
                details = comp.get("details") or {}
                if not isinstance(details, dict):
                    continue
                names = details.get("unmapped_users") or []
                if isinstance(names, list):
                    for name in names:
                        if isinstance(name, str) and name.strip():
                            out.add(name.strip())
        return out

    @staticmethod
    def _names_from_issue_cache(data_dir: Path) -> set[str]:
        """Discover identities from cached Jira issues.

        Same shape :meth:`UserMigration._discover_users_from_cached_issues`
        consumes — we read from any of these locations:

        * ``jira_issues_cache.json`` (unified)
        * ``jira_issues_*.json`` (per-project dumps from
          ``work_package_migration._extract_jira_issues``)

        Per-issue probes: ``fields.assignee``, ``fields.reporter``,
        ``fields.watches.watchers[*]``,
        ``fields.comment.comments[*].author``. Each user object's
        ``name`` / ``key`` / ``accountId`` is candidate-keyed (whichever
        identifier the issue carries).
        """
        out: set[str] = set()
        candidates: list[Path] = []
        unified = data_dir / "jira_issues_cache.json"
        if unified.exists():
            candidates.append(unified)
        for hit in data_dir.glob("jira_issues_*.json"):
            if hit.name == "jira_issues_cache.json":
                continue
            candidates.append(hit)
        if not candidates:
            return out

        for path in candidates:
            try:
                with path.open() as f:
                    raw = json.load(f)
            except OSError, json.JSONDecodeError:
                continue
            # Iterate the parsed object directly — materialising
            # ``list(raw.values())`` would copy every issue dict in
            # memory on large per-project dumps (4082 NRS issues ≈
            # tens of MB). Streaming over the underlying iterable
            # halves peak memory.
            if isinstance(raw, dict):
                for issue in raw.values():
                    _harvest_users_from_issue(issue, out)
            elif isinstance(raw, list):
                for issue in raw:
                    _harvest_users_from_issue(issue, out)
        return out

    @staticmethod
    def _resolve_existing_op_id(
        user_map: dict[str, Any],
        cand_keys: list[str],
    ) -> tuple[int | None, bool]:
        """Walk ``cand_keys``, collecting every distinct ``openproject_id``
        already present in ``user_map``.

        Returns ``(op_id, conflict)`` where:

        * ``op_id`` is the unique OP user id when all populated entries
          agree (or ``None`` when no cand key is mapped).
        * ``conflict`` is ``True`` when two cand keys point to
          DIFFERENT OP users — caller treats as
          ``alias_op_id_conflict`` and refuses to write aliases that
          would pick one silently. If any conflicting entry has
          ``matched_by="manual"``, that one wins (operator's
          manual mapping target is the source of truth).

        Defensive on the int parse: malformed historical entries
        (string ids, etc.) get coerced via ``int()``; on
        ``ValueError`` / ``TypeError`` the entry is skipped instead
        of crashing the run. Per PR #207 review.
        """
        seen_ids: set[int] = set()
        manual_op_id: int | None = None
        for k in cand_keys:
            rec = user_map.get(k)
            if rec is None:
                continue
            raw_op_id: Any = rec.get("openproject_id") if isinstance(rec, dict) else rec
            if raw_op_id is None:
                continue
            try:
                op_id = int(raw_op_id)
            except TypeError, ValueError:
                continue
            seen_ids.add(op_id)
            if isinstance(rec, dict) and rec.get("matched_by") == "manual":
                manual_op_id = op_id

        if not seen_ids:
            return None, False
        if len(seen_ids) == 1:
            return next(iter(seen_ids)), False
        # Multiple distinct ids → conflict. If one is operator-set,
        # honour the "manual target wins" contract and use it.
        if manual_op_id is not None:
            return manual_op_id, False
        return None, True

    def _candidate_keys(self, jira_user: dict[str, Any]) -> list[str]:
        """Identifier ladder mirroring the consumer migrations' resolvers.

        Probe order matches :meth:`AttachmentProvenanceMigration._resolve_user_id`
        and :meth:`WatcherMigration._resolve_user_id`:
        ``accountId`` → ``name`` → ``key`` → ``emailAddress`` →
        ``displayName``. Cloud instances key on ``accountId``;
        Server/DC on ``name``. Yielding the most stable identifier
        first means a backfilled mapping is reachable by the
        consumers' first probe whenever possible.
        """
        out: list[str] = []
        for k in ("accountId", "name", "key", "emailAddress", "displayName"):
            v = jira_user.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        return out

    def _find_op_user(self, jira_user: dict[str, Any]) -> dict[str, Any] | None:
        """Best-effort: locate an OP user matching the Jira user.

        Suppresses *only* the not-found path
        (:class:`RecordNotFoundError`) — a real client failure
        (``QueryExecutionError``, auth, network) is allowed to
        propagate so the operator sees the incident instead of a
        silent ``not_found_in_op``. Per PR #205 review.
        """
        name = jira_user.get("name") or jira_user.get("key")
        if isinstance(name, str) and name:
            try:
                user = self.op_client.get_user(name)
            except RecordNotFoundError:
                user = None
            if user:
                return user
        email = jira_user.get("emailAddress")
        if isinstance(email, str) and email:
            try:
                user = self.op_client.get_user_by_email(email)
            except RecordNotFoundError:
                user = None
            if user:
                return user
        return None

    def _build_entry(self, op_user: dict[str, Any], jira_user: dict[str, Any]) -> dict[str, Any] | None:
        """Build the user_mapping entry, returning ``None`` if the OP user
        has no usable id.
        """
        op_id = op_user.get("id")
        if not isinstance(op_id, int):
            try:
                op_id = int(op_id) if op_id is not None else None
            except TypeError, ValueError:
                op_id = None
        if op_id is None:
            return None
        return {
            "openproject_id": op_id,
            "jira_name": jira_user.get("name"),
            "jira_email": jira_user.get("emailAddress"),
            "jira_display_name": jira_user.get("displayName"),
            "jira_key": jira_user.get("key"),
            "matched_by": "user_mapping_backfill",
        }

    def run(self) -> ComponentResult:  # type: ignore[override]
        self.logger.info("Starting user_mapping backfill")

        user_map: dict[str, Any] = dict(self.mappings.get_mapping("user") or {})
        before = len(user_map)

        results_dir = self.data_dir.parent / "results"
        from_results = self._names_from_previous_results(results_dir)
        from_cache = self._names_from_issue_cache(self.data_dir)
        all_names = from_results | from_cache
        self.logger.info(
            "Backfill candidates: %d from previous migration_results, %d from issue cache (%d unique)",
            len(from_results),
            len(from_cache),
            len(all_names),
        )

        if not all_names:
            # Same details schema as the main return path so downstream
            # consumers (audit, dashboards) read a uniform shape
            # regardless of which branch the component took.
            return ComponentResult(
                success=True,
                updated=0,
                message="No backfill candidates from previous results / issue cache.",
                details={
                    "from_previous_results": 0,
                    "from_issue_cache": 0,
                    "added": 0,
                    "already_mapped": 0,
                    "not_found_in_jira": 0,
                    "not_found_in_op_count": 0,
                    "not_found_in_op_sample": [],
                    "jira_api_errors": 0,
                    "added_sample": [],
                },
            )

        # Filter out identities already reachable by any probe key in
        # the existing mapping. We don't know yet which identifier
        # form each name takes (login vs accountId vs email), so the
        # cheap path is "is this string already a key?". A second
        # check after the Jira lookup catches the case where Jira
        # reports an alternate identifier we already have.
        unresolved: list[str] = []
        already_mapped: list[str] = []
        for name in sorted(all_names):
            if name in user_map:
                already_mapped.append(name)
            else:
                unresolved.append(name)

        added: list[dict[str, Any]] = []
        not_found_in_jira: list[str] = []
        not_found_in_op: list[dict[str, Any]] = []
        # Tracks Jira API failures distinct from "user doesn't exist"
        # (auth, network, rate limit). Misclassifying an outage as
        # missing users would cause silent data loss on the next
        # consumer (PR #205 review).
        jira_api_errors = 0

        for name in unresolved:
            try:
                jira_user = self.jira_client.get_user_info(name)
            except JiraResourceNotFoundError:
                # Genuine 404 — record under not_found_in_jira.
                jira_user = None
            except Exception as exc:
                # Any other JiraError (auth, rate-limit, connection)
                # is an outage, not a missing user. Log + count;
                # don't pretend the user doesn't exist.
                self.logger.warning(
                    "Jira API error looking up %r: %s — counted under jira_api_errors",
                    name,
                    exc,
                )
                jira_api_errors += 1
                continue
            if not isinstance(jira_user, dict):
                not_found_in_jira.append(name)
                continue

            # Build the candidate identifier ladder for this Jira user
            # (``accountId`` → ``name`` → ``key`` → ``emailAddress`` →
            # ``displayName``). The probe orders are NOT identical
            # across consumer migrations:
            #
            #   - ``WatcherMigration._resolve_user_id``:
            #     account_id → name → email_address → display_name (no ``key``).
            #   - ``AttachmentProvenanceMigration._author_identifiers``:
            #     accountId → name → key → emailAddress → email → displayName.
            #   - ``WpMetadataBackfillMigration._resolve_user_id``:
            #     accountId → name → key → emailAddress → displayName.
            #
            # Writing the alias under EVERY identifier (including
            # ``key`` even though watcher skips it) ensures the same
            # user is reachable from any probe path the consumers use
            # now — or might use after a future code change.
            cand_keys = self._candidate_keys(jira_user)

            # Reuse path: an alternate identifier already maps this
            # Jira user (e.g. ``user_map["JIRAUSER18400"]`` exists but
            # ``user_map["anne.geissler"]`` doesn't). Reuse the
            # existing entry's ``openproject_id`` instead of querying
            # OP — extra API calls aren't needed and the existing
            # entry may carry operator-set metadata we shouldn't
            # overwrite.
            #
            # Caught by the live 2026-05-07 NRS run: 18 watcher
            # ``unmapped_users`` were already mapped under their
            # ``JIRAUSER<id>`` keys but the watcher resolver probes
            # ``name`` first and never reached the ``key`` field. The
            # earlier "skip if any candidate present" logic was too
            # aggressive — it left the mapping reachable only via
            # ``key`` and the consumer kept dropping watchers.
            existing_op_id, conflict = self._resolve_existing_op_id(user_map, cand_keys)
            if conflict:
                # Multiple cand_keys point to DIFFERENT OP users.
                # Don't silently pick one — that would write alias
                # entries pointing to whichever id we happened to see
                # first and could redirect the consumer to the wrong
                # OP user. Surface as ``not_found_in_op`` so the
                # operator triages (the conflict could be a stale
                # auto-mapped entry vs an operator-fixed one).
                not_found_in_op.append(
                    {
                        "jira_name": name,
                        "reason": "alias_op_id_conflict",
                        "details": "multiple cand_keys map to different OP users; refusing to silently pick one",
                    },
                )
                continue

            if existing_op_id is not None:
                missing_keys = [k for k in cand_keys if k not in user_map]
                if not missing_keys:
                    already_mapped.append(name)
                    continue
                # Emit a thin alias entry that points at the same OP
                # user under each missing identifier. ``matched_by``
                # marks these as alias-only writes so an audit can
                # tell them apart from primary backfills.
                alias_entry: dict[str, Any] = {
                    "openproject_id": existing_op_id,
                    "jira_name": jira_user.get("name"),
                    "jira_email": jira_user.get("emailAddress"),
                    "jira_display_name": jira_user.get("displayName"),
                    "jira_key": jira_user.get("key"),
                    "matched_by": "user_mapping_backfill_alias",
                }
                for k in missing_keys:
                    user_map[k] = alias_entry
                added.append(
                    {
                        "name": name,
                        "openproject_id": existing_op_id,
                        "alias_for_existing": True,
                        "added_keys": missing_keys,
                    },
                )
                continue

            # No existing alias — probe OP normally.
            op_user = self._find_op_user(jira_user)
            if op_user is None:
                not_found_in_op.append(
                    {
                        "jira_name": name,
                        "jira_email": jira_user.get("emailAddress"),
                        "jira_display": jira_user.get("displayName"),
                        "active": jira_user.get("active"),
                    },
                )
                continue

            entry = self._build_entry(op_user, jira_user)
            if entry is None:
                not_found_in_op.append(
                    {
                        "jira_name": name,
                        "reason": "op_user_id_missing_or_non_integer",
                    },
                )
                continue
            for k in cand_keys:
                if k not in user_map:
                    user_map[k] = entry
            added.append({"name": name, "openproject_id": entry["openproject_id"]})

        # Persist only when something actually changed. Avoids re-writing
        # the file every run when no candidates were resolved.
        if added:
            self.mappings.set_mapping("user", user_map)
            self.logger.info(
                "Backfilled %d Jira identities into user_mapping (was %d → %d entries).",
                len(added),
                before,
                len(user_map),
            )
        if not_found_in_op:
            sample = [u.get("jira_name") for u in not_found_in_op[:20]]
            self.logger.warning(
                "%d Jira identities have no matching OP user (sample: %s)."
                " Operator must create these in OP or accept the loss as"
                " expected for fully-deleted accounts.",
                len(not_found_in_op),
                sample,
            )

        return ComponentResult(
            success=True,
            updated=len(added),
            message=(
                f"User mapping backfill: added={len(added)},"
                f" already_mapped={len(already_mapped)},"
                f" not_found_in_jira={len(not_found_in_jira)},"
                f" not_found_in_op={len(not_found_in_op)},"
                f" jira_api_errors={jira_api_errors}"
            ),
            details={
                "from_previous_results": len(from_results),
                "from_issue_cache": len(from_cache),
                "added": len(added),
                "already_mapped": len(already_mapped),
                "not_found_in_jira": len(not_found_in_jira),
                "not_found_in_op_count": len(not_found_in_op),
                "not_found_in_op_sample": not_found_in_op[:20],
                "jira_api_errors": jira_api_errors,
                "added_sample": added[:20],
            },
        )


def _harvest_users_from_issue(issue: Any, into: set[str]) -> None:
    """Walk an issue (dict or SDK object) and collect every user
    identifier it carries. Mutates ``into`` for streaming-friendliness.

    Probed locations match the consumers:

    * ``fields.assignee``
    * ``fields.reporter``
    * ``fields.watches.watchers[*]``
    * ``fields.comment.comments[*].author``
    """

    def _read(obj: Any, name: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def _add(user: Any) -> None:
        if user is None:
            return
        # ``accountId`` first to match the consumer resolvers' probe
        # order (``AttachmentProvenanceMigration._resolve_user_id`` /
        # ``WatcherMigration._resolve_user_id``). Cloud users have a
        # stable ``accountId`` but variable ``displayName``; preferring
        # the latter would harvest less-stable identifiers and miss
        # matches when the display-name in the cache differs from the
        # current Jira directory.
        for k in ("accountId", "name", "key", "emailAddress", "displayName"):
            v = _read(user, k)
            if isinstance(v, str) and v.strip():
                into.add(v.strip())
                return  # Prefer the first hit (matches the resolver probe order)

    fields = _read(issue, "fields")
    if fields is None:
        return
    _add(_read(fields, "assignee"))
    _add(_read(fields, "reporter"))
    watches = _read(fields, "watches")
    if watches is not None:
        for w in _read(watches, "watchers") or []:
            _add(w)
    comment = _read(fields, "comment")
    if comment is not None:
        for c in _read(comment, "comments") or []:
            _add(_read(c, "author"))
