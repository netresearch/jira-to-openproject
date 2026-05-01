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
from typing import TYPE_CHECKING, Any

from src.clients.jira_client import (
    HTTP_NOT_FOUND,
    HTTP_OK,
    JiraApiError,
    JiraResourceNotFoundError,
)

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient


class JiraWorklogService:
    """Worklog-domain queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.clients.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.clients.jira_client import logger

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

    def get_work_logs_for_issue(self, issue_key: str) -> list[dict[str, Any]]:
        """Get all work logs for a specific issue.

        Args:
            issue_key: The key of the issue to get work logs for

        Returns:
            List of work log dictionaries with complete metadata

        Raises:
            JiraResourceNotFoundError: If the issue is not found
            JiraApiError: If the API request fails

        """
        try:
            # Get work logs using the JIRA library's worklog method
            work_logs = self._client.jira.worklogs(issue_key)

            result = []
            for work_log in work_logs:
                work_log_data = {
                    "id": work_log.id,
                    "issue_key": issue_key,
                    "author": {
                        "name": getattr(work_log.author, "name", None),
                        "display_name": getattr(work_log.author, "displayName", None),
                        "email": getattr(work_log.author, "emailAddress", None),
                        "account_id": getattr(work_log.author, "accountId", None),
                    },
                    "started": work_log.started,
                    "time_spent": work_log.timeSpent,
                    "time_spent_seconds": work_log.timeSpentSeconds,
                    "comment": getattr(work_log, "comment", None),
                    "created": work_log.created,
                    "updated": work_log.updated,
                }

                # Add update author if different from original author
                if hasattr(work_log, "updateAuthor") and work_log.updateAuthor:
                    work_log_data["update_author"] = {
                        "name": getattr(work_log.updateAuthor, "name", None),
                        "display_name": getattr(
                            work_log.updateAuthor,
                            "displayName",
                            None,
                        ),
                        "email": getattr(work_log.updateAuthor, "emailAddress", None),
                        "account_id": getattr(work_log.updateAuthor, "accountId", None),
                    }

                result.append(work_log_data)

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
            issues_with_logs = 0
            total_work_logs = 0

            for issue in all_issues:
                issue_key = issue.key

                # Check if issue has work logs in the basic fields first
                has_work_logs = (
                    hasattr(issue.fields, "worklog") and issue.fields.worklog and issue.fields.worklog.total > 0
                )

                if has_work_logs or include_empty:
                    # Apply adaptive rate limiting before request
                    self._client.rate_limiter.wait_if_needed(f"get_work_logs_{project_key}")

                    try:
                        request_start = time.time()
                        work_logs = self.get_work_logs_for_issue(issue_key)
                        response_time = time.time() - request_start

                        # Record successful response for rate limiting adaptation
                        self._client.rate_limiter.record_response(response_time, HTTP_OK)

                        if work_logs or include_empty:
                            work_logs_by_issue[issue_key] = work_logs
                            if work_logs:
                                issues_with_logs += 1
                                total_work_logs += len(work_logs)

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
                        continue
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
                        continue

            self._logger.info(
                "Work log extraction complete for project '%s': %s issues with work logs, %s total work logs",
                project_key,
                issues_with_logs,
                total_work_logs,
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
            # Use the JIRA library's worklog method to get specific work log
            work_log = self._client.jira.worklog(issue_key, work_log_id)

            work_log_data = {
                "id": work_log.id,
                "issue_key": issue_key,
                "author": {
                    "name": getattr(work_log.author, "name", None),
                    "display_name": getattr(work_log.author, "displayName", None),
                    "email": getattr(work_log.author, "emailAddress", None),
                    "account_id": getattr(work_log.author, "accountId", None),
                },
                "started": work_log.started,
                "time_spent": work_log.timeSpent,
                "time_spent_seconds": work_log.timeSpentSeconds,
                "comment": getattr(work_log, "comment", None),
                "created": work_log.created,
                "updated": work_log.updated,
            }

            # Add update author if different from original author
            if hasattr(work_log, "updateAuthor") and work_log.updateAuthor:
                work_log_data["update_author"] = {
                    "name": getattr(work_log.updateAuthor, "name", None),
                    "display_name": getattr(work_log.updateAuthor, "displayName", None),
                    "email": getattr(work_log.updateAuthor, "emailAddress", None),
                    "account_id": getattr(work_log.updateAuthor, "accountId", None),
                }

            # Add visibility restrictions if present
            if hasattr(work_log, "visibility") and work_log.visibility:
                work_log_data["visibility"] = {
                    "type": getattr(work_log.visibility, "type", None),
                    "value": getattr(work_log.visibility, "value", None),
                }

            return work_log_data

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
