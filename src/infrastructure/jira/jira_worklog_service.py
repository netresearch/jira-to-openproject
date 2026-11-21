# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Jira worklog and issue-link-type queries.

Phase 3e of ADR-002 continues the jira_client.py decomposition. The
worklog-related methods (single-issue worklog read, project-wide
worklog walk, individual worklog detail fetch) and the adjacent issue
link type lookup move into a focused service.

The service is exposed on ``JiraClient`` as ``self.worklogs`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like the other Phase 3 services this is HTTP-only — calls
go through the ``jira`` SDK — so there is no Ruby-script escaping to
worry about.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from src.infrastructure.jira.comment_audience import has_comment_audience
from src.infrastructure.jira.jira_client import (
    HTTP_NOT_FOUND,
    HTTP_OK,
    JiraApiError,
    JiraClient,
    JiraResourceNotFoundError,
)


def _worklog_user(user: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "key": "key",
        "name": "name",
        "display_name": "displayName",
        "email": "emailAddress",
        "account_id": "accountId",
        "time_zone": "timeZone",
    }
    return {key: user.get(attr) for key, attr in fields.items()}


def _worklog_record(issue_key: str, raw: dict[str, Any]) -> dict[str, Any]:
    row = {
        "id": raw["id"],
        "issue_key": issue_key,
        "author": _worklog_user(raw["author"]),
        "started": raw["started"],
        "time_spent": raw.get("timeSpent"),
        "time_spent_seconds": raw["timeSpentSeconds"],
        "comment": raw.get("comment"),
        "created": raw.get("created"),
        "updated": raw.get("updated"),
    }
    if raw.get("updateAuthor") is not None:
        row["update_author"] = _worklog_user(raw["updateAuthor"])
    return row


class JiraWorklogService:
    """Worklog-domain queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        self._author_zones: dict[tuple[str, str], str] = {}
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_issue_link_types(self) -> list[dict[str, Any]]:
        """Get all issue link types from Jira.

        Returns:
            List of issue link type dictionaries with id, name, inward, and outward

        Raises:
            JiraApiError: If the API request fails

        """
        try:
            link_types = self._client.jira.issue_link_types()
            result = [
                {
                    "id": link_type.id,
                    "name": link_type.name,
                    "inward": link_type.inward,
                    "outward": link_type.outward,
                }
                for link_type in link_types
            ]

            if not link_types:
                self._logger.warning("No issue link types found in Jira")

            return result
        except Exception as e:
            error_msg = f"Failed to get issue link types: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def _record_with_author_zone(self, issue_key: str, row: dict[str, Any]) -> dict[str, Any]:
        record = _worklog_record(issue_key, row)
        author = record["author"]
        if author.get("time_zone"):
            return record
        field = "accountId" if author.get("account_id") else "key"
        value = row["author"].get(field)
        if not value:
            msg = "Work log author has no stable identity for retrieving the source time zone."
            raise JiraApiError(msg)
        cache_key = (field, value)
        if cache_key not in self._author_zones:
            profile = self._client.jira._get_json("user", params={field: value})
            if profile.get(field) != value or not isinstance(profile.get("timeZone"), str) or not profile["timeZone"]:
                msg = "Jira did not confirm the work log author's identity and time zone."
                raise JiraApiError(msg)
            self._author_zones[cache_key] = profile["timeZone"]
        author["time_zone"] = self._author_zones[cache_key]
        return record

    def _check_issue_audience(self, issue_key: str, level_id: str | None = None) -> None:
        """Require an issue without a security level, or exactly the selected one.

        A level is allowed only when the caller names it: the level work package of a selected
        issue lies in that level's private sub-project (ADR-009), whose readers the time entries
        then have. A missing or other level is refused.
        """
        path = f"issue/{quote(issue_key, safe='')}"
        issue = self._client.jira._get_json(path, params={"fields": "project,security"})
        if level_id is None:
            allowed = issue.get("key") == issue_key and self._has_no_security_level(issue)
        else:
            security = (issue.get("fields") or {}).get("security")
            found = security.get("id") if isinstance(security, dict) else None
            allowed = issue.get("key") == issue_key and type(found) in (str, int) and str(found) == level_id
        if not allowed:
            msg = "Issue security cannot be represented on a native time entry."
            raise JiraApiError(msg)

    def _has_no_security_level(self, issue: dict[str, Any]) -> bool:
        fields = issue.get("fields") or {}
        if "security" in fields:
            return fields["security"] is None
        # Jira Server may omit a null field, even when explicitly requested.
        # Positively match the same immutable issue under the empty-level condition.
        issue_id = int(issue["id"])
        matches = self._client.jira.search_issues(f"id = {issue_id} AND level IS EMPTY", fields="id", maxResults=1)
        return len(matches) == 1 and str(matches[0].id) == str(issue_id)

    def _worklog_pages(self, issue_key: str, level_id: str | None = None) -> list[dict[str, Any]]:
        path = f"issue/{quote(issue_key, safe='')}/worklog"
        self._check_issue_audience(issue_key, level_id)
        start = 0
        total = None
        seen = set()
        results = []
        while True:
            page = self._client.jira._get_json(
                path, params={"startAt": start, "maxResults": 100, "expand": "properties"}
            )
            rows = page["worklogs"]
            current_total = page["total"]
            if page["startAt"] != start or (total is not None and total != current_total):
                msg = "Jira returned an inconsistent work log page."
                raise JiraApiError(msg)
            total = current_total
            ids = {str(row["id"]) for row in rows}
            if len(ids) != len(rows) or seen & ids:
                msg = "Jira returned duplicate work log IDs."
                raise JiraApiError(msg)
            seen.update(ids)
            if any(has_comment_audience(row) for row in rows):
                msg = "Work log audience cannot be represented on a native time entry."
                raise JiraApiError(msg)
            results.extend(self._record_with_author_zone(issue_key, row) for row in rows)
            start += len(rows)
            if start == total:
                return results
            if not rows or start > total:
                msg = "Jira returned an incomplete work log page."
                raise JiraApiError(msg)

    def get_work_logs_for_issue(self, issue_key: str, level_id: str | None = None) -> list[dict[str, Any]]:
        """Get all work logs for a specific issue.

        Args:
            issue_key: The key of the issue to get work logs for
            level_id: The security level the issue must have; None requires no level

        Returns:
            List of work log dictionaries with complete metadata

        Raises:
            JiraResourceNotFoundError: If the issue is not found
            JiraApiError: If the API request fails

        """
        try:
            result = self._worklog_pages(issue_key, level_id)
            self._logger.debug(
                "Retrieved %s work logs for issue %s",
                len(result),
                issue_key,
            )
            return result

        except Exception as e:
            error_msg = f"Failed to get work logs for issue {issue_key}: {e!s}"
            self._logger.exception(error_msg)
            msg_lower = str(e).lower()
            if "issue does not exist" in msg_lower or "issue not found" in msg_lower:
                msg = f"Issue {issue_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def _read_project_work_logs(self, issue_key: str, project_key: str) -> list[dict[str, Any]] | None:
        # Apply adaptive rate limiting before request
        self._client.rate_limiter.wait_if_needed(f"get_work_logs_{project_key}")

        try:
            request_start = time.time()
            work_logs = self.get_work_logs_for_issue(issue_key)
            response_time = time.time() - request_start

            # Record successful response for rate limiting adaptation
            self._client.rate_limiter.record_response(response_time, HTTP_OK)

            return work_logs
        except JiraResourceNotFoundError:
            # Issue was deleted between listing and fetching work logs
            self._logger.warning(
                "Issue %s not found when fetching work logs",
                issue_key,
            )
            self._client.rate_limiter.record_response(
                time.time() - request_start,
                HTTP_NOT_FOUND,
            )
            return None
        except JiraApiError as e:
            self._logger.warning(
                "Failed to get work logs for issue %s: %s",
                issue_key,
                e,
            )
            # Record an actual 5xx so the rate-limiter's
            # server-error backoff kicks in. The
            # pre-extraction code wrote
            # ``HTTP_BAD_REQUEST_MIN + 5`` (= 405) with
            # an inline ``# 500`` comment, but the
            # arithmetic was wrong (400 + 5 = 405) and
            # ``RateLimiter.record_response`` only flags
            # ``status_code >= 500`` as a server error,
            # so the backoff path never fired.
            self._client.rate_limiter.record_response(
                time.time() - request_start,
                500,
            )
            return None

    def get_all_work_logs_for_project(
        self,
        project_key: str,
        *,
        include_empty: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Get all work logs for all issues in a project."""
        try:
            self._logger.info(
                "Fetching work logs for all issues in project '%s'...",
                project_key,
            )

            # Get all issues for the project. ``get_all_issues_for_project``
            # uses ``fields=None`` (full payload) and only expands
            # ``renderedFields`` (and ``changelog`` when requested) — there's
            # no explicit worklog expansion. We rely on the ``worklog``
            # field being present in the standard issue payload, then
            # call ``get_work_logs_for_issue`` for the full per-issue
            # detail. ``get_all_issues_for_project`` still lives on the
            # client; it will move to a future JiraIssueService
            # extraction.
            all_issues = self._client.get_all_issues_for_project(
                project_key,
                expand_changelog=False,
            )

            work_logs_by_issue = {}

            for issue in all_issues:
                issue_key = issue.key

                # Check if issue has work logs in the basic fields first
                has_work_logs = (
                    hasattr(issue.fields, "worklog") and issue.fields.worklog and issue.fields.worklog.total > 0
                )

                if has_work_logs or include_empty:
                    work_logs = self._read_project_work_logs(issue_key, project_key)
                    if work_logs is not None and (work_logs or include_empty):
                        work_logs_by_issue[issue_key] = work_logs
            self._logger.info(
                "Work log extraction complete for project '%s': %s issues with work logs, %s total work logs",
                project_key,
                sum(bool(logs) for logs in work_logs_by_issue.values()),
                sum(len(logs) for logs in work_logs_by_issue.values()),
            )

            return work_logs_by_issue

        except Exception as e:
            error_msg = f"Failed to get work logs for project {project_key}: {e!s}"
            self._logger.exception(error_msg)
            if "project does not exist" in str(e).lower() or "project not found" in str(e).lower():
                msg = f"Project {project_key} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e

    def get_work_log_details(self, issue_key: str, work_log_id: str) -> dict[str, Any]:
        """Get detailed information for a specific work log.

        Args:
            issue_key: The key of the issue containing the work log
            work_log_id: The ID of the work log to get details for

        Returns:
            Dictionary containing detailed work log information

        Raises:
            JiraResourceNotFoundError: If the issue or work log is not found
            JiraApiError: If the API request fails

        """
        try:
            path = f"issue/{quote(issue_key, safe='')}/worklog/{quote(work_log_id, safe='')}"
            self._check_issue_audience(issue_key)
            row = self._client.jira._get_json(path, params={"expand": "properties"})
            if has_comment_audience(row):
                msg = "This work log has restricted or unrecognized audience metadata."
                raise JiraApiError(msg)
            return self._record_with_author_zone(issue_key, row)

        except Exception as e:
            error_msg = f"Failed to get work log {work_log_id} for issue {issue_key}: {e!s}"
            self._logger.exception(error_msg)
            if (
                "issue does not exist" in str(e).lower()
                or "issue not found" in str(e).lower()
                or "worklog does not exist" in str(e).lower()
                or "worklog not found" in str(e).lower()
            ):
                msg = f"Issue {issue_key} or work log {work_log_id} not found"
                raise JiraResourceNotFoundError(msg) from e
            raise JiraApiError(error_msg) from e
