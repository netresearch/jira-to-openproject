"""Jira Tempo plugin queries.

Phase 3f of ADR-002 continues the jira_client.py decomposition. The
Tempo plugin methods (accounts, customers, account links, work logs,
work attributes, time entries) move into a focused service.

The service is exposed on ``JiraClient`` as ``self.tempo`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like the other Phase 3 services this is HTTP-only — calls
go through the ``jira`` SDK or its session, and through the client's
``_make_request`` helper — so there is no Ruby-script escaping to
worry about.

Tempo is a third-party Jira plugin for time tracking. The endpoints
under ``/rest/tempo-accounts/1`` and ``/rest/tempo-timesheets/3`` are
plugin-provided and only available on Jira instances with Tempo
installed.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.infrastructure.jira.jira_client import (
    HTTP_NOT_FOUND,
    HTTP_OK,
    JiraApiError,
    JiraAuthenticationError,
    JiraCaptchaError,
    JiraConnectionError,
    JiraResourceNotFoundError,
)
from src.utils.timezone import UTC

if TYPE_CHECKING:
    from src.infrastructure.jira.jira_client import JiraClient


class JiraTempoService:
    """Tempo-plugin queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_tempo_accounts(self, *, expand: bool = False) -> list[dict[str, Any]]:
        """Retrieve all Tempo accounts."""
        path = "/rest/tempo-accounts/1/account"
        # The pre-extraction code accepted ``expand`` as a parameter but
        # always sent ``expand=true`` to the Tempo API regardless of
        # the caller's value. Honour the parameter so callers who pass
        # ``expand=False`` actually get the cheaper, slimmer response.
        params = {
            "expand": "true" if expand else "false",
            "skipArchived": "false",
        }

        self._logger.info("Fetching Tempo accounts")
        try:
            response = self._client._make_request(path, params=params)
            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo accounts: HTTP {response.status_code}"
                raise JiraApiError(msg)

            accounts = response.json()
            self._logger.info("Successfully retrieved %s Tempo accounts.", len(accounts))
            return accounts
        except JiraCaptchaError, JiraAuthenticationError, JiraConnectionError:
            raise  # Re-raise specific exceptions
        except Exception as e:
            error_msg = f"Failed to retrieve Tempo accounts: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_customers(self) -> list[dict[str, Any]]:
        """Retrieve all Tempo customers (often used for Companies).

        Returns:
            A list of Tempo customers

        Raises:
            JiraApiError: If the API request fails

        """
        path = "/rest/tempo-accounts/1/customer"
        self._logger.info("Fetching Tempo customers (Companies)")

        try:
            response = self._client._make_request(path)
            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo customers: HTTP {response.status_code}"
                raise JiraApiError(msg)

            customers = response.json()
            self._logger.info("Successfully retrieved %s Tempo customers.", len(customers))
            return customers
        except JiraCaptchaError, JiraAuthenticationError, JiraConnectionError:
            raise  # Re-raise specific exceptions
        except Exception as e:
            error_msg = f"Failed to retrieve Tempo customers: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_account_links_for_project(
        self,
        project_id: int,
    ) -> list[dict[str, Any]]:
        """Retrieve Tempo account links for a specific Jira project.

        Args:
            project_id: The Jira project ID.

        Returns:
            A list of account links

        Raises:
            JiraResourceNotFoundError: If the project is not found
            JiraApiError: If the API request fails

        """
        # Use the Tempo account-by-project endpoint for project-specific account lookup
        path = f"/rest/tempo-accounts/1/account/project/{project_id}"

        self._logger.debug("Fetching Tempo account links for project '%s'", project_id)
        try:
            response = self._client._make_request(path)

            # Handle 404s specially - these might be expected if no links exist
            if response.status_code == HTTP_NOT_FOUND:
                self._logger.warning("No account links found for project %s.", project_id)
                return []

            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve account links: HTTP {response.status_code}"
                raise JiraApiError(msg)

            links = response.json()
            self._logger.debug(
                "Successfully retrieved %s account links for project %s.",
                len(links),
                project_id,
            )
            return links
        except JiraCaptchaError, JiraAuthenticationError, JiraConnectionError:
            raise  # Re-raise specific exceptions
        except JiraResourceNotFoundError:
            # Convert to empty list for this specific case since it's an expected condition
            self._logger.warning(
                "Project %s not found or no account links exist.",
                project_id,
            )
            return []
        except Exception as e:
            error_msg = f"Failed to retrieve account links for project {project_id}: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_work_logs(
        self,
        issue_key: str | None = None,
        project_key: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        user_key: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Get Tempo work logs with enhanced metadata and attributes.

        Args:
            issue_key: Filter by specific issue key
            project_key: Filter by specific project key
            date_from: Start date in YYYY-MM-DD format
            date_to: End date in YYYY-MM-DD format
            user_key: Filter by specific user key
            limit: Maximum number of results per request (default: 1000)

        Returns:
            List of Tempo work log dictionaries with enhanced metadata

        Raises:
            JiraApiError: If the API request fails

        """
        try:
            # Build query parameters
            params = {"limit": limit}
            if issue_key:
                params["issue"] = issue_key
            if project_key:
                params["project"] = project_key
            if date_from:
                params["dateFrom"] = date_from
            if date_to:
                params["dateTo"] = date_to
            if user_key:
                params["user"] = user_key

            # Use Tempo Timesheets API v3 endpoint
            path = "/rest/tempo-timesheets/3/worklogs"
            self._logger.info(
                "Fetching Tempo work logs with params: %s",
                {k: v for k, v in params.items() if k != "limit"},
            )

            response = self._client.jira._session.get(
                f"{self._client.base_url}{path}",
                params=params,
            )

            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo work logs: HTTP {response.status_code}"
                self._logger.error(msg)
                raise JiraApiError(msg)

            work_logs = response.json()
            self._logger.info("Successfully retrieved %s Tempo work logs", len(work_logs))

            # Enhance work logs with additional metadata
            enhanced_work_logs = []
            for work_log in work_logs:
                enhanced_work_log = {
                    "tempo_worklog_id": work_log.get("tempoWorklogId"),
                    "jira_worklog_id": work_log.get("jiraWorklogId"),
                    "issue_key": work_log.get("issue", {}).get("key"),
                    "issue_id": work_log.get("issue", {}).get("id"),
                    "author": {
                        "username": work_log.get("author", {}).get("name"),
                        "display_name": work_log.get("author", {}).get("displayName"),
                        "account_id": work_log.get("author", {}).get("accountId"),
                    },
                    "time_spent_seconds": work_log.get("timeSpentSeconds"),
                    "billable_seconds": work_log.get("billableSeconds"),
                    "date_started": work_log.get("dateStarted"),
                    "time_started": work_log.get("timeStarted"),
                    "comment": work_log.get("comment"),
                    "created": work_log.get("created"),
                    "updated": work_log.get("updated"),
                    "work_attributes": work_log.get("workAttributes", []),
                    "account": work_log.get("account", {}),
                    "approval_status": work_log.get("approvalStatus"),
                    "external_hours": work_log.get("externalHours"),
                    "external_id": work_log.get("externalId"),
                    "origin_task_id": work_log.get("originTaskId"),
                }
                enhanced_work_logs.append(enhanced_work_log)

            return enhanced_work_logs

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo work logs: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_work_attributes(self) -> list[dict[str, Any]]:
        """Get all Tempo work attributes (custom fields for work logs).

        Returns:
            List of work attribute dictionaries

        Raises:
            JiraApiError: If the API request fails

        """
        try:
            path = "/rest/tempo-timesheets/3/work-attributes"
            self._logger.info("Fetching Tempo work attributes")

            response = self._client.jira._session.get(
                f"{self._client.base_url}{path}",
            )

            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo work attributes: HTTP {response.status_code}"
                self._logger.error(msg)
                raise JiraApiError(msg)

            attributes = response.json()
            self._logger.info(
                "Successfully retrieved %s Tempo work attributes",
                len(attributes),
            )

            return attributes

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo work attributes: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_all_work_logs_for_project(
        self,
        project_key: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all Tempo work logs for a project with pagination handling."""
        try:
            self._logger.info(
                "Fetching all Tempo work logs for project '%s' from %s to %s",
                project_key,
                date_from or "beginning",
                date_to or "end",
            )

            all_work_logs = []
            limit = 1000
            offset = 0

            while True:
                # Apply adaptive rate limiting before request
                self._client.rate_limiter.wait_if_needed(f"get_tempo_work_logs_{project_key}")

                # Build query parameters
                params = {
                    "project": project_key,
                    "limit": limit,
                    "offset": offset,
                }
                if date_from:
                    params["dateFrom"] = date_from
                if date_to:
                    params["dateTo"] = date_to

                path = "/rest/tempo-timesheets/3/worklogs"

                request_start = time.time()
                response = self._client.jira._session.get(
                    f"{self._client.base_url}{path}",
                    params=params,
                )
                response_time = time.time() - request_start

                # Record response for rate limiting adaptation
                self._client.rate_limiter.record_response(response_time, response.status_code)

                if response.status_code != HTTP_OK:
                    msg = f"Failed to retrieve Tempo work logs for project {project_key}: HTTP {response.status_code}"
                    self._logger.error(msg)
                    raise JiraApiError(msg)

                work_logs_batch = response.json()

                if not work_logs_batch:
                    break

                # Process batch and add to results
                for work_log in work_logs_batch:
                    enhanced_work_log = {
                        "tempo_worklog_id": work_log.get("tempoWorklogId"),
                        "jira_worklog_id": work_log.get("jiraWorklogId"),
                        "issue_key": work_log.get("issue", {}).get("key"),
                        "issue_id": work_log.get("issue", {}).get("id"),
                        "author": {
                            "username": work_log.get("author", {}).get("name"),
                            "display_name": work_log.get("author", {}).get(
                                "displayName",
                            ),
                            "account_id": work_log.get("author", {}).get("accountId"),
                        },
                        "time_spent_seconds": work_log.get("timeSpentSeconds"),
                        "billable_seconds": work_log.get("billableSeconds"),
                        "date_started": work_log.get("dateStarted"),
                        "time_started": work_log.get("timeStarted"),
                        "comment": work_log.get("comment"),
                        "created": work_log.get("created"),
                        "updated": work_log.get("updated"),
                        "work_attributes": work_log.get("workAttributes", []),
                        "account": work_log.get("account", {}),
                        "approval_status": work_log.get("approvalStatus"),
                        "external_hours": work_log.get("externalHours"),
                        "external_id": work_log.get("externalId"),
                        "origin_task_id": work_log.get("originTaskId"),
                    }
                    all_work_logs.append(enhanced_work_log)

                # Check if we've reached the end
                if len(work_logs_batch) < limit:
                    break

                offset += limit

            self._logger.info(
                "Tempo work log extraction complete for project '%s': %s total work logs",
                project_key,
                len(all_work_logs),
            )
            return all_work_logs

        except Exception as e:
            error_msg = f"Failed to retrieve all Tempo work logs for project {project_key}: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_work_log_by_id(self, tempo_worklog_id: str) -> dict[str, Any]:
        """Get a specific Tempo work log by its Tempo ID.

        Args:
            tempo_worklog_id: The Tempo work log ID

        Returns:
            Dictionary containing detailed Tempo work log information

        Raises:
            JiraResourceNotFoundError: If the work log is not found
            JiraApiError: If the API request fails

        """
        try:
            path = f"/rest/tempo-timesheets/3/worklogs/{tempo_worklog_id}"
            self._logger.debug("Fetching Tempo work log with ID: %s", tempo_worklog_id)

            response = self._client.jira._session.get(
                f"{self._client.base_url}{path}",
            )

            if response.status_code == HTTP_NOT_FOUND:
                msg = f"Tempo work log {tempo_worklog_id} not found"
                raise JiraResourceNotFoundError(msg)
            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo work log {tempo_worklog_id}: HTTP {response.status_code}"
                self._logger.error(msg)
                raise JiraApiError(msg)

            work_log = response.json()

            # Return enhanced work log data
            return {
                "tempo_worklog_id": work_log.get("tempoWorklogId"),
                "jira_worklog_id": work_log.get("jiraWorklogId"),
                "issue_key": work_log.get("issue", {}).get("key"),
                "issue_id": work_log.get("issue", {}).get("id"),
                "author": {
                    "username": work_log.get("author", {}).get("name"),
                    "display_name": work_log.get("author", {}).get("displayName"),
                    "account_id": work_log.get("author", {}).get("accountId"),
                },
                "time_spent_seconds": work_log.get("timeSpentSeconds"),
                "billable_seconds": work_log.get("billableSeconds"),
                "date_started": work_log.get("dateStarted"),
                "time_started": work_log.get("timeStarted"),
                "comment": work_log.get("comment"),
                "created": work_log.get("created"),
                "updated": work_log.get("updated"),
                "work_attributes": work_log.get("workAttributes", []),
                "account": work_log.get("account", {}),
                "approval_status": work_log.get("approvalStatus"),
                "external_hours": work_log.get("externalHours"),
                "external_id": work_log.get("externalId"),
                "origin_task_id": work_log.get("originTaskId"),
            }

        except Exception as e:
            if "not found" in str(e).lower():
                msg = f"Tempo work log {tempo_worklog_id} not found"
                raise JiraResourceNotFoundError(msg) from e
            error_msg = f"Failed to retrieve Tempo work log {tempo_worklog_id}: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_user_work_logs(
        self,
        user_key: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all Tempo work logs for a specific user."""
        try:
            return self.get_tempo_work_logs(
                user_key=user_key,
                date_from=date_from,
                date_to=date_to,
            )

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo work logs for user {user_key}: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e

    def get_tempo_time_entries(
        self,
        project_keys: list[str] | None = None,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        user_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get Tempo time entries with enhanced metadata for migration."""
        try:
            self._logger.info(
                "Fetching Tempo time entries for projects: %s, date range: %s to %s, user: %s",
                project_keys,
                date_from,
                date_to,
                user_key,
            )

            all_time_entries = []

            if project_keys:
                # Get work logs for specific projects
                for project_key in project_keys:
                    try:
                        project_work_logs = self.get_tempo_all_work_logs_for_project(
                            project_key=project_key,
                            date_from=date_from,
                            date_to=date_to,
                        )

                        # Filter by user if specified. The enhanced
                        # worklog shape produced by
                        # ``get_tempo_all_work_logs_for_project`` only
                        # populates ``author.username`` /
                        # ``author.display_name`` / ``author.account_id``
                        # — there is no ``author.key``. Match against
                        # any of the populated identifiers so callers
                        # can pass either a username or an accountId.
                        if user_key:
                            project_work_logs = [
                                log
                                for log in project_work_logs
                                if user_key
                                in (
                                    log.get("author", {}).get("username"),
                                    log.get("author", {}).get("account_id"),
                                )
                            ]

                        all_time_entries.extend(project_work_logs)
                        self._logger.debug(
                            "Retrieved %d entries for project %s",
                            len(project_work_logs),
                            project_key,
                        )

                    except Exception as e:
                        self._logger.warning(
                            "Failed to get Tempo entries for project %s: %s",
                            project_key,
                            e,
                        )
                        continue
            # Get work logs using general method (may be limited by Tempo API)
            elif user_key:
                all_time_entries = self.get_tempo_user_work_logs(
                    user_key=user_key,
                    date_from=date_from,
                    date_to=date_to,
                )
            else:
                all_time_entries = self.get_tempo_work_logs(
                    date_from=date_from,
                    date_to=date_to,
                )

            # Enhance entries with migration metadata. The upstream
            # ``get_tempo_all_work_logs_for_project`` /
            # ``get_tempo_user_work_logs`` / ``get_tempo_work_logs``
            # methods all return the same snake_case-keyed enhanced
            # shape (``tempo_worklog_id``, ``jira_worklog_id``,
            # ``issue_key``, ``time_spent_seconds``, ``date_started``,
            # ...). The pre-extraction code mistakenly reached for
            # camelCase keys (``worklogId``, ``issue.projectKey``,
            # ``timeSpentSeconds``, ``dateStarted``) which never
            # existed on these dicts, so ``_migration_metadata`` was
            # mostly ``None`` and the ``timeSpent`` / ``started``
            # alias branches never fired. Match the actual keys.
            enhanced_entries = []
            for entry in all_time_entries:
                enhanced_entry = entry.copy()

                # Project key isn't carried on the enhanced entry, but
                # the issue key encodes it (Jira project keys are the
                # prefix before the first ``-``).
                issue_key = entry.get("issue_key") or ""
                project_key_from_issue = issue_key.split("-", 1)[0] if "-" in issue_key else None

                # Add migration-specific metadata
                enhanced_entry["_migration_metadata"] = {
                    "source_type": "tempo",
                    "extraction_timestamp": datetime.now(tz=UTC).isoformat(),
                    "tempo_worklog_id": entry.get("tempo_worklog_id"),
                    "jira_worklog_id": entry.get("jira_worklog_id"),
                    "issue_key": issue_key or None,
                    "project_key": project_key_from_issue,
                }

                # Ensure consistent field naming for migration. Map
                # snake_case → migration aliases (``timeSpent``,
                # ``started``) that downstream consumers expect.
                if "time_spent_seconds" in entry:
                    enhanced_entry["timeSpent"] = entry["time_spent_seconds"]

                if "date_started" in entry:
                    enhanced_entry["started"] = entry["date_started"]
                elif "started" not in entry and "created" in entry:
                    enhanced_entry["started"] = entry["created"]

                enhanced_entries.append(enhanced_entry)

            self._logger.success(
                "Retrieved %d Tempo time entries total",
                len(enhanced_entries),
            )
            return enhanced_entries

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo time entries: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e
