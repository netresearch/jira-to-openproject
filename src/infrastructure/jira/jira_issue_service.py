"""Jira issue-domain queries.

Phase 3k of ADR-002 — the final big slice of the jira_client.py
decomposition. The five issue-domain methods (full-project pagination,
single-issue detail extraction, JQL-based batch fetch with its private
helper, and the streaming paginator) move into a focused service.

The service is exposed on ``JiraClient`` as ``self.issues`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. ``get_all_issues_for_project`` in particular is also called
from ``JiraTempoService`` and ``JiraWorklogService`` via
``self._client.get_all_issues_for_project(...)`` — those keep working
because the delegator stays in place.

Like the other Phase 3 services this is HTTP-only — calls go through
the ``jira`` SDK — so there is no Ruby-script escaping to worry about.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from src.infrastructure.jira.jira_client import (
    JiraApiError,
    JiraConnectionError,
    JiraResourceNotFoundError,
)
from src.utils.performance_optimizer import StreamingPaginator, rate_limited

if TYPE_CHECKING:
    from jira import Issue
    from src.infrastructure.jira.jira_client import JiraClient

# Maximum number of issue keys per ``search_issues`` call.
#
# URL-length limits vary by server: Apache Tomcat defaults to 8 KB
# (``maxHttpHeaderSize``); Traefik defaults to 64 KB; intermediate proxies may
# impose stricter limits.  Each URL-encoded, double-quoted Jira key —
# e.g. ``%22NRS-4311%22%2C`` — is roughly 25 bytes when percent-encoded.
# 25 keys therefore produce ≈ 625 bytes of JQL argument on top of the
# ~100-byte base URL, staying safely under the lowest common limit.
# 100 keys (the ``BatchProcessor`` default) would produce ≈ 2 500 bytes of
# JQL argument alone, risking rejection on servers with a tighter URL cap.
_FETCH_BATCH_CHUNK_SIZE: int = 25

# An unexpected 401/403 on a chunk (after earlier requests succeeded) is almost
# always a transient session/proxy/WAF blip rather than broken credentials, so
# retry the chunk once with a short backoff before giving up. A persistent auth
# failure simply costs one extra attempt per chunk.
_CHUNK_TRANSIENT_RETRIES: int = 1
_CHUNK_TRANSIENT_RETRY_BACKOFF_SECONDS: float = 2.0


class JiraIssueService:
    """Issue-domain queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_all_issues_for_project(
        self,
        project_key: str,
        *,
        expand_changelog: bool = True,
    ) -> list[Issue]:
        """Get all issues for a specific project, handling pagination."""
        all_issues: list[Issue] = []
        start_at = 0
        max_results = 100  # Fetch in batches of 100
        # Surround project key with quotes to handle reserved words
        jql = f'project = "{project_key}" ORDER BY created ASC'
        fields = None  # Get all fields
        # Include renderedFields to fetch comments, along with optional changelog
        expand_parts = []
        if expand_changelog:
            expand_parts.append("changelog")
        expand_parts.append("renderedFields")  # Includes comments
        expand = ",".join(expand_parts)

        self._logger.notice("Fetching all issues for project '%s'...", project_key)

        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        # Verify project exists
        try:
            # Simple way to check if project exists - will raise exception if not found
            self._client.jira.project(project_key)
        except Exception as e:
            msg = f"Project '{project_key}' not found: {e!s}"
            raise JiraResourceNotFoundError(msg) from e

        # Fetch all pages
        while True:
            try:
                self._logger.debug(
                    "Fetching issues for %s: startAt=%s, maxResults=%s",
                    project_key,
                    start_at,
                    max_results,
                )

                issues_page = self._client.jira.search_issues(
                    jql,
                    startAt=start_at,
                    maxResults=max_results,
                    fields=fields,
                    expand=expand,
                    json_result=False,  # Get jira.Issue objects
                )

                if not issues_page:
                    self._logger.debug(
                        "No more issues found for %s at startAt=%s",
                        project_key,
                        start_at,
                    )
                    break  # Exit loop if no more issues are returned

                all_issues.extend(issues_page)
                self._logger.debug(
                    "Fetched %s issues (total: %s) for %s",
                    len(issues_page),
                    len(all_issues),
                    project_key,
                )

                # Check if this was the last page
                if len(issues_page) < max_results:
                    break

                start_at += len(issues_page)

            except Exception as e:
                error_msg = f"Failed to get issues page for project {project_key} at startAt={start_at}: {e!s}"
                self._logger.exception(error_msg)
                raise JiraApiError(error_msg) from e

        self._logger.info(
            "Finished fetching %s issues for project '%s'.",
            len(all_issues),
            project_key,
        )
        return all_issues

    def get_issue_details(self, issue_key: str) -> dict[str, Any]:
        """Get detailed information about a specific issue.

        Args:
            issue_key: The key of the issue to get details for

        Returns:
            A dictionary containing detailed issue information

        Raises:
            JiraResourceNotFoundError: If the issue is not found
            JiraApiError: If the API request fails

        """
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            issue = self._client.jira.issue(issue_key)

            # Extract basic issue data
            issue_data = {
                "id": issue.id,
                "key": issue.key,
                "summary": issue.fields.summary,
                "description": issue.fields.description,
                "issue_type": {
                    "id": issue.fields.issuetype.id,
                    "name": issue.fields.issuetype.name,
                },
                "status": {
                    "id": issue.fields.status.id,
                    "name": issue.fields.status.name,
                },
                "created": issue.fields.created,
                "updated": issue.fields.updated,
                "assignee": None,
                "reporter": None,
                "comments": [],
                "attachments": [],
            }

            # Add assignee if exists
            if hasattr(issue.fields, "assignee") and issue.fields.assignee:
                issue_data["assignee"] = {
                    "name": issue.fields.assignee.name,
                    "display_name": issue.fields.assignee.displayName,
                }

            # Add reporter if exists
            if hasattr(issue.fields, "reporter") and issue.fields.reporter:
                issue_data["reporter"] = {
                    "name": issue.fields.reporter.name,
                    "display_name": issue.fields.reporter.displayName,
                }

            # Add comments
            if hasattr(issue.fields, "comment") and issue.fields.comment:
                issue_data["comments"] = [
                    {
                        "id": comment.id,
                        "body": comment.body,
                        "author": comment.author.displayName,
                        "created": comment.created,
                    }
                    for comment in issue.fields.comment.comments
                ]

            # Add attachments
            if hasattr(issue.fields, "attachment") and issue.fields.attachment:
                issue_data["attachments"] = [
                    {
                        "id": attachment.id,
                        "filename": attachment.filename,
                        "size": attachment.size,
                        "content": attachment.url,
                    }
                    for attachment in issue.fields.attachment
                ]

            return issue_data
        except Exception as e:
            error_msg = f"Failed to get issue details for {issue_key}: {e!s}"
            self._logger.exception(error_msg)
            if "issue does not exist" in str(e).lower() or "issue not found" in str(e).lower():
                msg = f"Issue {issue_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    # ── batch operations ─────────────────────────────────────────────────

    def batch_get_issues(self, issue_keys: list[str]) -> dict[str, Issue]:
        """Retrieve multiple issues in batches for optimal performance."""
        if not issue_keys:
            return {}
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        # ``BatchProcessor.process_batches`` returns a ``list`` of
        # results from each batch — and each batch result is a
        # ``dict[str, Issue]``. Flatten into a single dict so callers
        # see the documented return shape (the pre-extraction code
        # silently returned a ``list[dict]`` typed as ``dict[str,
        # Issue]``).
        batch_results: list[dict[str, Issue]] = self._client.performance_optimizer.batch_processor.process_batches(
            issue_keys,
            self._fetch_issues_batch,
        )
        merged: dict[str, Issue] = {}
        for batch_result in batch_results:
            if isinstance(batch_result, dict):
                merged.update(batch_result)
        return merged

    def _fetch_issues_batch(self, issue_keys: list[str], **kwargs: object) -> dict[str, Issue]:
        """Fetch a batch of issues from Jira API.

        Deduplicates ``issue_keys`` (preserving order), then splits them into
        sub-chunks of at most ``_FETCH_BATCH_CHUNK_SIZE`` keys before building
        the JQL query.  This keeps the ``key in (...)`` clause below the URL
        length limit enforced by Apache Tomcat and reverse proxies (which reject
        an over-long URI with HTTP 414, or 413/400 on some proxies).  A chunk
        that is still rejected as too long is recovered by ``_fetch_single_chunk``
        splitting it further; genuine auth failures (401/403) are reported as
        such rather than misattributed to URL length.
        """
        # Deduplicate while preserving insertion order so the caller's ordering
        # is respected and duplicate keys don't inflate chunk count or JQL size.
        unique_keys = list(dict.fromkeys(k for k in issue_keys if k))
        if not unique_keys:
            return {}

        # Split into URL-safe sub-chunks and merge results.  The loop handles
        # any list size uniformly — no need for a separate single-chunk branch.
        merged: dict[str, Issue] = {}
        for chunk_idx in range(0, len(unique_keys), _FETCH_BATCH_CHUNK_SIZE):
            chunk = unique_keys[chunk_idx : chunk_idx + _FETCH_BATCH_CHUNK_SIZE]
            chunk_result = self._fetch_single_chunk(
                chunk,
                chunk_idx // _FETCH_BATCH_CHUNK_SIZE,
                **kwargs,
            )
            merged.update(chunk_result)

        # Reconcile requested-vs-returned so a dropped chunk is never silent at
        # the batch level (#260).  A genuine 401/403 chunk drop is non-fatal by
        # design (per-chunk WARNING above), but a partial result previously
        # looked complete to callers.  Surface the gap with a single summary so
        # operators can see issues went unfetched.  Note: missing keys can also
        # be issues deleted in Jira (``key in (...)`` JQL omits them silently),
        # so the message names both causes rather than asserting an error.
        missing = [k for k in unique_keys if k not in merged]
        if missing:
            sample = ", ".join(missing[:10])
            self._logger.warning(
                "Issue fetch reconciliation (batch_num=%s): requested %d key(s), received %d; "
                "%d key(s) were not returned by Jira — either deleted in Jira, or dropped by a "
                "fetch error (see the per-chunk warnings above). Missing sample: [%s%s]",
                kwargs.get("batch_num"),
                len(unique_keys),
                len(merged),
                len(missing),
                sample,
                "" if len(missing) <= 10 else ", …",
            )
        return merged

    def _fetch_single_chunk(
        self,
        issue_keys: list[str],
        chunk_index: int,
        **kwargs: object,
    ) -> dict[str, Issue]:
        """Fetch one URL-safe chunk of issue keys from the Jira search API.

        Args:
            issue_keys:  The sub-list of keys to fetch (length ≤ ``_FETCH_BATCH_CHUNK_SIZE``).
            chunk_index: Zero-based index of this chunk within the enclosing
                         batch; included in the error log so operators can
                         identify which range of keys failed.
            **kwargs:    Forwarded from :meth:`_fetch_issues_batch`; may contain
                         ``batch_num`` supplied by ``BatchProcessor`` for
                         cross-batch log correlation.

        Returns:
            ``dict[key → Issue]`` for the keys that were found; empty dict on error.

        """
        # Quote each key so a key containing a JQL reserved word /
        # special char (e.g. issue-key collisions with Jira keywords)
        # doesn't break the query. The pre-extraction code joined
        # raw keys, which is fine for vanilla Jira issue keys but
        # fragile for any issue type that allows extended characters.
        quoted_keys = ",".join(f'"{key}"' for key in issue_keys)
        jql = f"key in ({quoted_keys})"
        batch_num = kwargs.get("batch_num")
        first_key = issue_keys[0] if issue_keys else "?"
        last_key = issue_keys[-1] if issue_keys else "?"

        auth_retries = 0
        while True:
            try:
                issues = self._client.jira.search_issues(
                    jql,
                    maxResults=len(issue_keys),
                    expand="changelog",
                )
                return {issue.key: issue for issue in issues}
            except Exception as exc:
                status = self._extract_http_status(exc)

                # HTTP 413/414: the request URI is genuinely too long.  Recover by
                # halving the chunk and retrying — the only failure mode where
                # "URL-length" is the honest cause and where data can be reclaimed.
                if status in {413, 414} and len(issue_keys) > 1:
                    mid = len(issue_keys) // 2
                    self._logger.warning(
                        "Chunk too large (HTTP %s): batch_num=%s, chunk_index=%s, keys=[%s..%s];"
                        " splitting %d keys into %d+%d and retrying",
                        status,
                        batch_num,
                        chunk_index,
                        first_key,
                        last_key,
                        len(issue_keys),
                        mid,
                        len(issue_keys) - mid,
                    )
                    merged: dict[str, Issue] = {}
                    merged.update(self._fetch_single_chunk(issue_keys[:mid], chunk_index, **kwargs))
                    merged.update(self._fetch_single_chunk(issue_keys[mid:], chunk_index, **kwargs))
                    return merged
                if status in {413, 414}:
                    # A single key whose URI is still rejected cannot be split further.
                    self._logger.warning(
                        "Single issue key %s rejected as too large (HTTP %s); skipping it"
                        " (batch_num=%s, chunk_index=%s)",
                        first_key,
                        status,
                        batch_num,
                        chunk_index,
                    )
                    return {}
                # HTTP 401/403: an unexpected auth failure on a pre-bounded chunk is
                # usually a transient session/proxy/WAF blip — retry once with a short
                # backoff before giving up. NOT a URL-length rejection.
                if status in {401, 403} and auth_retries < _CHUNK_TRANSIENT_RETRIES:
                    auth_retries += 1
                    self._logger.warning(
                        "Chunk fetch hit HTTP %s (auth) for batch_num=%s, chunk_index=%s, keys=[%s..%s];"
                        " retrying once (%d/%d) after %.1fs in case of a transient session/proxy blip",
                        status,
                        batch_num,
                        chunk_index,
                        first_key,
                        last_key,
                        auth_retries,
                        _CHUNK_TRANSIENT_RETRIES,
                        _CHUNK_TRANSIENT_RETRY_BACKOFF_SECONDS,
                    )
                    time.sleep(_CHUNK_TRANSIENT_RETRY_BACKOFF_SECONDS)
                    continue
                if status in {401, 403}:
                    # Still failing after the retry: a genuine authentication/
                    # authorization failure, NOT a URL-length rejection.  Name it
                    # honestly and make the dropped keys explicit.
                    self._logger.warning(
                        "Chunk fetch failed: batch_num=%s, chunk_index=%s, keys=[%s..%s]"
                        " — authentication/authorization failure (HTTP %s) after %d retr(y/ies);"
                        " these %d issue(s) were NOT fetched. Check Jira credentials/session and re-run.",
                        batch_num,
                        chunk_index,
                        first_key,
                        last_key,
                        status,
                        auth_retries,
                        len(issue_keys),
                    )
                    return {}
                if status is not None:
                    self._logger.warning(
                        "Chunk fetch failed: batch_num=%s, chunk_index=%s, keys=[%s..%s]"
                        " — Jira returned HTTP %s; these %d issue(s) were NOT fetched.",
                        batch_num,
                        chunk_index,
                        first_key,
                        last_key,
                        status,
                        len(issue_keys),
                    )
                    return {}
                # Unexpected error with no HTTP status — keep the full traceback.
                self._logger.exception(
                    "Chunk fetch failed: batch_num=%s, chunk_index=%s, keys=[%s..%s]",
                    batch_num,
                    chunk_index,
                    first_key,
                    last_key,
                )
                return {}

    @staticmethod
    def _extract_http_status(exc: BaseException) -> int | None:
        """Return the HTTP status code carried by an exception or its cause chain.

        Production surfaces the status in several shapes, all handled here:
        * a jira-lib ``JIRAError`` exposes ``.status_code`` (often on
          ``exc.__cause__`` after ``JiraClient`` re-wraps it ``from`` the cause);
        * a ``requests`` error exposes ``.response.status_code``;
        * ``JiraClient._handle_response`` raises ``JiraAuthenticationError`` /
          ``JiraApiError`` ``from None`` with the status only in the message
          (``"HTTP Error <code>: ..."``) — no attribute and no ``__cause__``.

        ``id()`` tracking guards against cyclic ``__cause__`` chains.
        """
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            code = getattr(current, "status_code", None)
            if isinstance(code, int):
                return code
            response = getattr(current, "response", None)
            resp_code = getattr(response, "status_code", None) if response is not None else None
            if isinstance(resp_code, int):
                return resp_code
            match = re.search(r"HTTP Error (\d{3})\b", str(current))
            if match:
                return int(match.group(1))
            current = current.__cause__
        return None

    # ── streaming ────────────────────────────────────────────────────────

    @rate_limited()
    def stream_all_issues_for_project(
        self,
        project_key: str,
        fields: str | None = None,
        batch_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream all issues for a project with memory-efficient pagination.

        The pre-extraction code referenced ``StreamingPaginator`` with
        kwargs and a method that don't exist on the actual
        ``src.utils.performance_optimizer.StreamingPaginator``
        (``__init__`` takes ``fetch_func, page_size, max_pages``;
        the streaming entry point is ``iter_items``, not
        ``paginate_jql_search``). This method was therefore broken
        in production. Rewritten to use the real API: build a
        ``fetch_func`` closure over the SDK's ``search_issues``
        and yield through ``iter_items``.
        """
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        # Use ``is not None`` so a caller-supplied ``batch_size=0`` is
        # respected literally (rather than swapped for the default).
        effective_batch_size = batch_size if batch_size is not None else self._client.batch_size

        # Quote ``project_key`` in JQL so reserved words / special
        # characters in project keys don't break the query — same
        # treatment ``get_all_issues_for_project`` uses.
        jql = f'project = "{project_key}"'

        def _fetch_page(start_at: int, max_results: int, **_kw: object) -> list[Issue]:
            return list(
                self._client.jira.search_issues(
                    jql,
                    startAt=start_at,
                    maxResults=max_results,
                    fields=fields,
                ),
            )

        paginator = StreamingPaginator(
            fetch_func=_fetch_page,
            page_size=effective_batch_size,
        )
        yield from paginator.iter_items()

    # ── watchers ─────────────────────────────────────────────────────────

    def get_issue_watchers(self, issue_key: str) -> list[dict[str, Any]]:
        """Get the watchers for a specific Jira issue.

        Args:
            issue_key: The key of the issue to get watchers for (e.g., 'PROJECT-123')

        Returns:
            List of watcher dictionaries

        Raises:
            JiraResourceNotFoundError: If the issue is not found
            JiraApiError: If the API request fails

        """
        if not self._client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        try:
            result = self._client.jira.watchers(issue_key)

            if not result:
                self._logger.debug("No watchers found for issue %s", issue_key)
                return []

            return [
                {
                    "name": getattr(watcher, "name", None),
                    # Server/DC internal user key (e.g. JIRAUSER18400); the user
                    # mapping is keyed by it, so it must be carried for watcher
                    # resolution (#260).
                    "key": getattr(watcher, "key", None),
                    "accountId": getattr(watcher, "accountId", None),
                    "displayName": getattr(watcher, "displayName", None),
                    "emailAddress": getattr(watcher, "emailAddress", None),
                    "active": getattr(watcher, "active", True),
                }
                for watcher in result.watchers
            ]
        except Exception as e:
            error_msg = f"Failed to get watchers for issue {issue_key}: {e!s}"
            self._logger.exception(error_msg)
            if "issue does not exist" in str(e).lower() or "issue not found" in str(e).lower():
                msg = f"Issue {issue_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e
