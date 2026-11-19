# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

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
from typing import Any

from src.infrastructure.jira.comment_audience import has_comment_audience
from src.infrastructure.jira.jira_client import (
    HTTP_NOT_FOUND,
    HTTP_OK,
    JiraApiError,
    JiraAuthenticationError,
    JiraCaptchaError,
    JiraClient,
    JiraConnectionError,
    JiraResourceNotFoundError,
    JiraServiceUnavailableError,
    _assert_json_response,
    http_status_from_error,
)
from src.utils.timezone import UTC

# Allowed WorkAttributeType values in the Tempo Data Center Core API.
_WORK_ATTRIBUTE_TYPES = frozenset(
    {"ACCOUNT", "BILLABLE_SECONDS", "CHECKBOX", "DYNAMIC_DROPDOWN", "INPUT_FIELD", "INPUT_NUMERIC", "STATIC_LIST"}
)


def _work_attributes(work_log: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconcile Tempo Server's nested and flat attributes against the same Jira worklog."""
    flat = work_log.get("worklogAttributes", work_log.get("workAttributes", []))
    values = work_log.get("workAttributeValues", [])
    if not values:
        return flat
    nested = []
    for value in values:
        if str(value["worklogId"]) != str(work_log["jiraWorklogId"]):
            msg = "Tempo returned an attribute for a different work log."
            raise JiraApiError(msg)
        nested.append({"key": value["workAttribute"]["key"], "value": value["value"]})
    if flat and sorted(flat, key=lambda item: item["key"]) != sorted(nested, key=lambda item: item["key"]):
        msg = "Conflicting Tempo work attribute representations."
        raise JiraApiError(msg)
    return flat or nested


def _requested_project(project: str | None, issue: str | None) -> str | None:
    if not issue:
        return project
    issue_project, _, number = issue.rpartition("-")
    if not issue_project or not number.isdecimal() or (project and project != issue_project):
        msg = "Issue key does not belong to the requested project."
        raise JiraApiError(msg)
    return issue_project


def _matches_scope(row: dict[str, Any], project: str | None, issue: str | None, user: str | None) -> bool:
    issue_key = row.get("issue_key") or ""
    if project and issue_key.rpartition("-")[0] != project:
        msg = "Tempo returned a work log outside the requested project."
        raise JiraApiError(msg)
    author = row.get("author") or {}
    return (not issue or issue_key == issue) and (
        not user or user in (author.get("key"), author.get("username"), author.get("account_id"))
    )


def _visible_tempo_record(work_log: dict[str, Any]) -> dict[str, Any]:
    if has_comment_audience(work_log):
        msg = "Cannot expose audience-restricted Tempo work logs."
        raise JiraApiError(msg)

    return {
        "tempo_worklog_id": work_log.get("tempoWorklogId", work_log.get("id")),
        "jira_worklog_id": work_log.get("jiraWorklogId"),
        "issue_key": work_log.get("issue", {}).get("key"),
        "issue_id": work_log.get("issue", {}).get("id"),
        "author": {
            "username": work_log.get("author", {}).get("name"),
            "key": work_log.get("author", {}).get("key"),
            "display_name": work_log.get("author", {}).get("displayName"),
            "account_id": work_log.get("author", {}).get("accountId"),
        },
        "time_spent_seconds": work_log.get("timeSpentSeconds"),
        "billable_seconds": work_log.get("billableSeconds"),
        "date_started": work_log.get("dateStarted"),
        "time_started": work_log.get("timeStarted"),
        "comment": work_log.get("comment"),
        "created": work_log.get("dateCreated", work_log.get("created")),
        "updated": work_log.get("dateUpdated", work_log.get("updated")),
        "work_attributes": _work_attributes(work_log),
        "work_attribute_values": work_log.get("workAttributeValues", []),
        "account": work_log.get("account", {}),
        "approval_status": work_log.get("approvalStatus"),
        "external_hours": work_log.get("externalHours"),
        "external_id": work_log.get("externalId"),
        "origin_task_id": work_log.get("originTaskId"),
    }


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
            _assert_json_response(response, path=path)
            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo accounts: HTTP {response.status_code}"
                raise JiraApiError(msg)

            accounts = response.json()
            self._logger.info("Successfully retrieved %s Tempo accounts.", len(accounts))
            return accounts
        except JiraServiceUnavailableError as e:
            self._logger.warning(
                "Tempo accounts endpoint unavailable, skipping (Tempo may not be installed on this Jira). Details: %s",
                e,
            )
            return []
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
            _assert_json_response(response, path=path)
            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo customers: HTTP {response.status_code}"
                raise JiraApiError(msg)

            customers = response.json()
            self._logger.info("Successfully retrieved %s Tempo customers.", len(customers))
            return customers
        except JiraServiceUnavailableError as e:
            self._logger.warning(
                "Tempo customers endpoint unavailable, skipping (Tempo may not be installed on this Jira). Details: %s",
                e,
            )
            return []
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

            _assert_json_response(response, path=path)

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
        except JiraServiceUnavailableError as e:
            self._logger.warning(
                "Tempo account-links endpoint unavailable for project %s, skipping. Details: %s",
                project_id,
                e,
            )
            return []
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
        *,
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
            project_key = _requested_project(project_key, issue_key)
            filters = {"projectKey": project_key, "username": user_key, "dateFrom": date_from, "dateTo": date_to}
            params = {"limit": limit, **{key: value for key, value in filters.items() if value}}

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
            _assert_json_response(response, path=path)

            work_logs = response.json()
            self._logger.info("Successfully retrieved %s Tempo work logs", len(work_logs))

            records = (_visible_tempo_record(row) for row in work_logs)
            return [row for row in records if _matches_scope(row, project_key, issue_key, user_key)][:limit]

        except JiraServiceUnavailableError as e:
            self._logger.warning(
                "Tempo work-logs endpoint unavailable, skipping (Tempo "
                "Timesheets may not be installed on this Jira). Details: %s",
                e,
            )
            return []
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
        paths = (
            "/rest/tempo-core/1/work-attribute",
            "/rest/tempo-timesheets/3/worklog-attributes",
            "/rest/tempo-timesheets/3/work-attributes",
        )
        for path in paths:
            try:
                return self._work_attribute_catalogue(path)
            except JiraResourceNotFoundError:
                continue
            except Exception as exc:
                status = http_status_from_error(exc)
                if status == HTTP_NOT_FOUND:
                    continue
                if isinstance(exc, JiraServiceUnavailableError) and status == HTTP_OK:
                    self._logger.warning("Tempo work attributes unavailable: %s", exc)
                    return []
                msg = f"Failed to retrieve Tempo work attributes: {exc!s}"
                raise JiraApiError(msg) from exc

        self._logger.warning("Tempo work attribute endpoints unavailable")
        return []

    def _work_attribute_catalogue(self, path: str) -> list[dict[str, Any]]:
        response = self._client.jira._session.get(f"{self._client.base_url}{path}")
        if response.status_code != HTTP_OK:
            self._client._handle_response(response)
            msg = f"Unexpected HTTP {response.status_code} for {path}"
            raise JiraApiError(msg)
        _assert_json_response(response, path=path)
        attributes = response.json()
        if not isinstance(attributes, list) or not all(isinstance(attribute, dict) for attribute in attributes):
            msg = f"Unexpected work attribute catalogue from {path}"
            raise JiraApiError(msg)
        return attributes

    def work_attribute_snapshot(self) -> list[dict[str, Any]]:
        """Read the instance catalogue independently of selected projects and logged values."""
        path = "/rest/tempo-core/1/work-attribute"
        definitions = self._work_attribute_catalogue(path)
        self._unique_catalogue_ids(definitions)
        snapshot = []
        for item in sorted(definitions, key=lambda definition: definition["id"]):
            options = []
            attribute_type = item.get("type")
            if not isinstance(attribute_type, dict) or attribute_type.get("value") not in _WORK_ATTRIBUTE_TYPES:
                msg = "Tempo returned an unsupported work attribute type; its options cannot be verified."
                raise JiraApiError(msg)
            if attribute_type["value"] == "STATIC_LIST":
                options = self._work_attribute_catalogue(f"{path}/{item['id']}/static-list-value")
                self._unique_catalogue_ids(options)
                if any(
                    type(option.get("workAttributeId")) is not int or option["workAttributeId"] != item["id"]
                    for option in options
                ):
                    msg = "Tempo returned a static option for a different work attribute."
                    raise JiraApiError(msg)
            snapshot.append({"definition": item, "static_options": options})
        return snapshot

    @staticmethod
    def _unique_catalogue_ids(records: list[dict[str, Any]]) -> None:
        identifiers = [record.get("id") for record in records]
        if any(type(i) is not int or i <= 0 for i in identifiers) or len(set(identifiers)) != len(identifiers):
            msg = "Tempo returned invalid or duplicate catalogue IDs."
            raise JiraApiError(msg)

    def _project_worklog_page(
        self, project_key: str, offset: int, limit: int, date_from: str | None, date_to: str | None
    ) -> list[dict[str, Any]]:
        # Apply adaptive rate limiting before request
        self._client.rate_limiter.wait_if_needed(f"get_tempo_work_logs_{project_key}")

        # Build query parameters
        params = {
            "projectKey": project_key,
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
        _assert_json_response(response, path=path)

        return response.json()

    def get_tempo_all_work_logs_for_project(
        self,
        project_key: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read a complete project response. Pass date bounds to override the Tempo Server default period."""
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
            seen = set()

            while True:
                work_logs_batch = self._project_worklog_page(project_key, offset, limit, date_from, date_to)
                if not work_logs_batch:
                    break

                rows = [_visible_tempo_record(row) for row in work_logs_batch]
                ids = {str(row["jira_worklog_id"] or row["tempo_worklog_id"]) for row in rows}
                if "None" in ids or len(ids) != len(rows) or seen.intersection(ids):
                    msg = "Tempo returned missing, repeated or overlapping work log IDs."
                    raise JiraApiError(msg)
                seen.update(ids)
                all_work_logs.extend(row for row in rows if _matches_scope(row, project_key, None, None))

                # Server v3 may return its entire unpaginated array; older paged
                # implementations return at most the requested number of records.
                if len(rows) != limit:
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

            _assert_json_response(response, path=path)

            if response.status_code != HTTP_OK:
                msg = f"Failed to retrieve Tempo work log {tempo_worklog_id}: HTTP {response.status_code}"
                self._logger.error(msg)
                raise JiraApiError(msg)

            record = _visible_tempo_record(response.json())
            if str(record["tempo_worklog_id"]) != str(tempo_worklog_id):
                msg = "Tempo returned a different work log than requested."
                raise JiraApiError(msg)
            return record

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

    @staticmethod
    def _migration_time_entry(entry: dict[str, Any]) -> dict[str, Any]:
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

        return enhanced_entry

    def _project_time_entries(
        self, project_keys: list[str], user_key: str | None, date_from: str | None, date_to: str | None
    ) -> list[dict[str, Any]]:
        all_time_entries = []
        for project_key in project_keys:
            project_work_logs = self.get_tempo_all_work_logs_for_project(
                project_key=project_key, date_from=date_from, date_to=date_to
            )
            all_time_entries.extend(
                row for row in project_work_logs if _matches_scope(row, project_key, None, user_key)
            )
        return all_time_entries

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
                all_time_entries = self._project_time_entries(project_keys, user_key, date_from, date_to)
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
            enhanced_entries = [self._migration_time_entry(entry) for entry in all_time_entries]

            self._logger.success(
                "Retrieved %d Tempo time entries total",
                len(enhanced_entries),
            )
            return enhanced_entries

        except Exception as e:
            error_msg = f"Failed to retrieve Tempo time entries: {e!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from e
