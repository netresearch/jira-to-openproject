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

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from src.clients.jira_client import (
    JiraApiError,
    JiraConnectionError,
    JiraResourceNotFoundError,
)
from src.utils.performance_optimizer import StreamingPaginator, rate_limited

if TYPE_CHECKING:
    from jira import Issue
    from src.clients.jira_client import JiraClient


class JiraIssueService:
    """Issue-domain queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.clients.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.clients.jira_client import logger

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

        return self._client.performance_optimizer.batch_processor.process_batches(
            issue_keys,
            self._fetch_issues_batch,
        )

    def _fetch_issues_batch(self, issue_keys: list[str], **kwargs: object) -> dict[str, Issue]:
        """Fetch a batch of issues from Jira API."""
        if not issue_keys:
            return {}

        # Use JQL to fetch multiple issues at once
        jql = f"key in ({','.join(issue_keys)})"

        try:
            issues = self._client.jira.search_issues(
                jql,
                maxResults=len(issue_keys),
                expand="changelog",
            )
            return {issue.key: issue for issue in issues}
        except Exception:
            self._logger.exception(
                "Batch issue fetch failed for %d issues",
                len(issue_keys),
            )
            return {}

    # ── streaming ────────────────────────────────────────────────────────

    @rate_limited()
    def stream_all_issues_for_project(
        self,
        project_key: str,
        fields: str | None = None,
        batch_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream all issues for a project with memory-efficient pagination."""
        effective_batch_size = batch_size or self._client.batch_size

        paginator = StreamingPaginator(
            batch_size=effective_batch_size,
            rate_limiter=self._client.rate_limiter,
        )

        return paginator.paginate_jql_search(
            jira_client=self._client.jira,
            jql=f"project = {project_key}",
            fields=fields,
        )
