# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Tests for Jira client work log functionality."""

from unittest.mock import Mock, patch

import pytest

from src.infrastructure.jira.jira_client import (
    JiraApiError,
    JiraClient,
    JiraResourceNotFoundError,
)


class TestJiraClientWorkLog:
    """Test work log functionality in Jira client."""

    @pytest.fixture
    def mock_jira_client(self, monkeypatch):
        """Create a mock JiraClient for testing."""
        # Mock the config module to avoid import issues
        mock_config = Mock()
        mock_config.logger = Mock()
        mock_config.jira_config = {
            "url": "https://test.atlassian.net",
            "username": "test@example.com",
            "api_token": "test_token",
            "verify_ssl": True,
        }

        monkeypatch.setattr("src.infrastructure.jira.jira_client.config", mock_config)

        # Mock the JIRA instance to avoid actual API calls
        mock_jira_instance = Mock()
        mock_jira_instance.server_info.return_value = {
            "baseUrl": "https://test.atlassian.net",
            "version": "8.0.0",
        }

        # Create client and directly assign mocked jira instance
        # (JIRA class removed from enterprise bloat - only type annotation remains)
        client = JiraClient.__new__(JiraClient)
        client.jira = mock_jira_instance
        client.jira_url = "https://test.atlassian.net"
        client.base_url = "https://test.atlassian.net"

        # Set required attributes that __init__ would normally set
        import time

        from src.infrastructure.jira.jira_tempo_service import JiraTempoService
        from src.infrastructure.jira.jira_worklog_service import JiraWorklogService
        from src.utils.rate_limiter import create_jira_rate_limiter

        client.rate_limiter = create_jira_rate_limiter()
        client.request_count = 0
        client.period_start = time.time()

        # Phase 3e: ``get_work_logs_for_issue`` and friends now delegate
        # through ``self.worklogs``. Fixtures that bypass ``__init__``
        # have to wire the service explicitly.
        client.worklogs = JiraWorklogService(client)
        # Phase 3f: ``get_tempo_*`` methods now delegate through
        # ``self.tempo``. Fixtures that bypass ``__init__`` have to wire
        # the service explicitly.
        client.tempo = JiraTempoService(client)

        return client

    def test_get_work_logs_for_issue_success(self, mock_jira_client) -> None:
        """Test successful work log retrieval for an issue."""
        sdk = mock_jira_client.jira
        row = {
            "id": "1",
            "started": "2024-01-01T23:15:00Z",
            "timeSpentSeconds": 17,
            "timeSpent": "17s",
            "author": {"key": "stable", "name": "renamed"},
            "updateAuthor": {"key": "editor"},
            "comment": "A verified note",
        }
        sdk._get_json.side_effect = [
            {"key": "TEST-123", "fields": {"security": None}},
            {"startAt": 0, "total": 2, "worklogs": [row]},
            {"key": "stable", "timeZone": "Europe/Berlin"},
            {"startAt": 1, "total": 2, "worklogs": [{**row, "id": "2"}]},
        ]
        result = mock_jira_client.get_work_logs_for_issue("TEST-123")
        assert [r["id"] for r in result] == ["1", "2"]
        assert result[1]["author"]["time_zone"] == "Europe/Berlin"
        assert result[1]["author"]["key"] == "stable"
        assert result[1]["update_author"]["key"] == "editor"
        assert result[1]["comment"] == row["comment"]
        calls = sdk._get_json.call_args_list
        assert [call.args[0] for call in calls] == [
            "issue/TEST-123",
            "issue/TEST-123/worklog",
            "user",
            "issue/TEST-123/worklog",
        ]
        assert calls[2].kwargs["params"] == {"key": "stable"}
        assert calls[3].kwargs["params"] == {"startAt": 1, "maxResults": 100, "expand": "properties"}

    @pytest.mark.parametrize("row", [{"visibility": {"type": "role"}}, {"properties": [{"key": "unknown"}]}])
    def test_audience_is_checked_before_projection(self, mock_jira_client, row) -> None:
        row["id"] = "1"
        mock_jira_client.jira._get_json.side_effect = [
            {"key": "TEST-1", "fields": {"security": None}},
            {"startAt": 0, "total": 1, "worklogs": [row]},
        ]
        with pytest.raises(JiraApiError, match="audience"):
            mock_jira_client.get_work_logs_for_issue("TEST-1")

    def test_stalled_page_is_not_reported_as_empty(self, mock_jira_client) -> None:
        mock_jira_client.jira._get_json.side_effect = [
            {"key": "TEST-1", "fields": {"security": None}},
            {"startAt": 0, "total": 1, "worklogs": []},
        ]
        with pytest.raises(JiraApiError, match="incomplete"):
            mock_jira_client.get_work_logs_for_issue("TEST-1")

    @pytest.mark.parametrize("matched", [True, False])
    def test_missing_security_field_requires_positive_empty_level_match(self, mock_jira_client, matched) -> None:
        sdk = mock_jira_client.jira
        sdk._get_json.side_effect = [
            {"id": "42", "key": "TEST-1"},
            {"startAt": 0, "total": 0, "worklogs": []},
        ]
        sdk.search_issues.return_value = [Mock(id="42")] if matched else []
        if matched:
            assert mock_jira_client.get_work_logs_for_issue("TEST-1") == []
        else:
            with pytest.raises(JiraApiError, match="Issue security"):
                mock_jira_client.get_work_logs_for_issue("TEST-1")
        sdk.search_issues.assert_called_once_with("id = 42 AND level IS EMPTY", fields="id", maxResults=1)

    @pytest.mark.parametrize(
        ("fields", "allowed"),
        [
            ({"security": {"id": "10000"}}, True),
            ({"security": {"id": 10000}}, True),
            ({"security": {"id": "10001"}}, False),
            ({"security": None}, False),
            ({}, False),
        ],
    )
    def test_level_issue_needs_exactly_the_selected_level(self, mock_jira_client, fields, allowed) -> None:
        sdk = mock_jira_client.jira
        sdk._get_json.side_effect = [
            {"id": "42", "key": "TEST-1", "fields": fields},
            {"startAt": 0, "total": 0, "worklogs": []},
        ]
        if allowed:
            assert mock_jira_client.get_work_logs_for_issue("TEST-1", level_id="10000") == []
        else:
            with pytest.raises(JiraApiError, match="Issue security"):
                mock_jira_client.get_work_logs_for_issue("TEST-1", level_id="10000")
        assert sdk._get_json.call_args_list[0].args[0] == "issue/TEST-1"
        assert sdk._get_json.call_args_list[0].kwargs["params"] == {"fields": "project,security"}
        # A level is never confirmed through the empty-level search.
        sdk.search_issues.assert_not_called()

    def test_issue_with_a_level_is_still_refused_without_a_selected_level(self, mock_jira_client) -> None:
        mock_jira_client.jira._get_json.side_effect = [{"key": "TEST-1", "fields": {"security": {"id": "10000"}}}]
        with pytest.raises(JiraApiError, match="Issue security"):
            mock_jira_client.get_work_logs_for_issue("TEST-1")

    def test_get_work_logs_for_issue_not_found(self, mock_jira_client) -> None:
        mock_jira_client.jira._get_json.side_effect = Exception("Issue does not exist")
        with pytest.raises(JiraResourceNotFoundError, match="Issue TEST-404 not found"):
            mock_jira_client.get_work_logs_for_issue("TEST-404")

    def test_get_work_logs_for_issue_api_error(self, mock_jira_client) -> None:
        mock_jira_client.jira._get_json.side_effect = Exception("API Error")
        with pytest.raises(JiraApiError, match="Failed to get work logs for issue TEST-123"):
            mock_jira_client.get_work_logs_for_issue("TEST-123")

    def test_get_tempo_work_logs_success(self, mock_jira_client) -> None:
        """Test successful Tempo work logs retrieval."""
        # Mock the session response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "tempoWorklogId": 12345,
                "jiraWorklogId": 67890,
                "issue": {"key": "TEST-123", "id": 10001},
                "author": {
                    "name": "john.doe",
                    "displayName": "John Doe",
                    "accountId": "557058:12345",
                },
                "timeSpentSeconds": 3600,
                "billableSeconds": 3600,
                "dateStarted": "2023-01-15",
                "timeStarted": "09:00:00",
                "comment": "Fixed bug in authentication",
                "created": "2023-01-15T09:00:00.000Z",
                "updated": "2023-01-15T09:00:00.000Z",
                "workAttributes": [
                    {"key": "_Account_", "value": "PROJ-001"},
                    {"key": "_Category_", "value": "Development"},
                ],
                "account": {"key": "PROJ-001", "name": "Project Account"},
                "approvalStatus": "APPROVED",
                "externalHours": None,
                "externalId": None,
                "originTaskId": None,
            },
        ]

        with patch.object(
            mock_jira_client.jira._session,
            "get",
            return_value=mock_response,
        ):
            result = mock_jira_client.get_tempo_work_logs(project_key="TEST")

            assert len(result) == 1
            work_log = result[0]

            assert work_log["tempo_worklog_id"] == 12345
            assert work_log["jira_worklog_id"] == 67890
            assert work_log["issue_key"] == "TEST-123"
            assert work_log["author"]["username"] == "john.doe"
            assert work_log["time_spent_seconds"] == 3600
            assert work_log["billable_seconds"] == 3600
            assert work_log["approval_status"] == "APPROVED"
            assert len(work_log["work_attributes"]) == 2

    def test_get_tempo_work_logs_api_error(self, mock_jira_client) -> None:
        """Test handling of API errors when retrieving Tempo work logs."""
        mock_response = Mock()
        mock_response.status_code = 500

        with (
            patch.object(
                mock_jira_client.jira._session,
                "get",
                return_value=mock_response,
            ),
            pytest.raises(
                JiraApiError,
                match="Failed to retrieve Tempo work logs",
            ),
        ):
            mock_jira_client.get_tempo_work_logs(project_key="TEST")

    def test_get_tempo_work_attributes_success(self, mock_jira_client) -> None:
        """Test successful Tempo work attributes retrieval."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "key": "_Account_",
                "name": "Account",
                "type": "ACCOUNT",
                "required": True,
            },
            {
                "key": "_Category_",
                "name": "Work Category",
                "type": "STATIC_LIST",
                "required": False,
                "values": ["Development", "Testing", "Documentation"],
            },
        ]

        with patch.object(
            mock_jira_client.jira._session,
            "get",
            return_value=mock_response,
        ):
            result = mock_jira_client.get_tempo_work_attributes()

            assert len(result) == 2
            assert result[0]["key"] == "_Account_"
            assert result[0]["required"] is True
            assert result[1]["key"] == "_Category_"
            assert result[1]["values"] == ["Development", "Testing", "Documentation"]

    def test_get_tempo_all_work_logs_for_project_pagination(
        self,
        mock_jira_client,
    ) -> None:
        """Test Tempo work logs retrieval with pagination."""
        # Mock first batch (1000 items)
        first_batch = []
        for i in range(1000):
            first_batch.append(
                {
                    "tempoWorklogId": i + 1,
                    "jiraWorklogId": i + 10000,
                    "issue": {"key": f"TEST-{i}", "id": i + 20000},
                    "author": {"name": "test.user", "displayName": "Test User"},
                    "timeSpentSeconds": 3600,
                    "billableSeconds": 3600,
                },
            )

        # Mock second batch (500 items)
        second_batch = []
        for i in range(500):
            second_batch.append(
                {
                    "tempoWorklogId": i + 1001,
                    "jiraWorklogId": i + 11000,
                    "issue": {"key": f"TEST-{i + 1000}", "id": i + 21000},
                    "author": {"name": "test.user", "displayName": "Test User"},
                    "timeSpentSeconds": 1800,
                    "billableSeconds": 1800,
                },
            )

        mock_responses = [
            Mock(status_code=200, json=Mock(return_value=first_batch)),
            Mock(status_code=200, json=Mock(return_value=second_batch)),
        ]

        with patch.object(
            mock_jira_client.jira._session,
            "get",
            side_effect=mock_responses,
        ):
            result = mock_jira_client.get_tempo_all_work_logs_for_project("TEST")

            assert len(result) == 1500
            assert result[0]["tempo_worklog_id"] == 1
            assert result[1000]["tempo_worklog_id"] == 1001
            assert result[-1]["tempo_worklog_id"] == 1500

    def test_get_tempo_work_log_by_id_success(self, mock_jira_client) -> None:
        """Test successful retrieval of specific Tempo work log by ID."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tempoWorklogId": 12345,
            "jiraWorklogId": 67890,
            "issue": {"key": "TEST-123", "id": 10001},
            "author": {
                "name": "john.doe",
                "displayName": "John Doe",
                "accountId": "557058:12345",
            },
            "timeSpentSeconds": 7200,
            "billableSeconds": 6000,
            "dateStarted": "2023-01-15",
            "timeStarted": "09:00:00",
            "comment": "Code review and bug fixes",
            "workAttributes": [{"key": "_Account_", "value": "PROJ-001"}],
            "account": {"key": "PROJ-001", "name": "Project Account"},
            "approvalStatus": "PENDING",
        }

        with patch.object(
            mock_jira_client.jira._session,
            "get",
            return_value=mock_response,
        ):
            result = mock_jira_client.get_tempo_work_log_by_id("12345")

            # Verify the result structure
            assert "tempo_worklog_id" in result
            assert result["tempo_worklog_id"] == 12345
            assert result["issue_key"] == "TEST-123"

    def test_get_tempo_work_log_by_id_not_found(self, mock_jira_client) -> None:
        """Test handling when Tempo work log is not found."""
        mock_response = Mock()
        mock_response.status_code = 404

        with (
            patch.object(
                mock_jira_client.jira._session,
                "get",
                return_value=mock_response,
            ),
            pytest.raises(
                JiraResourceNotFoundError,
                match="Tempo work log 12345 not found",
            ),
        ):
            mock_jira_client.get_tempo_work_log_by_id("12345")

    def test_get_tempo_user_work_logs_success(self, mock_jira_client) -> None:
        """Test successful retrieval of Tempo work logs for a specific user."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "tempoWorklogId": 1,
                "jiraWorklogId": 101,
                "author": {"name": "john.doe", "displayName": "John Doe"},
                "timeSpentSeconds": 3600,
            },
            {
                "tempoWorklogId": 2,
                "jiraWorklogId": 102,
                "author": {"name": "john.doe", "displayName": "John Doe"},
                "timeSpentSeconds": 1800,
            },
        ]

        with patch.object(
            mock_jira_client.jira._session,
            "get",
            return_value=mock_response,
        ):
            result = mock_jira_client.get_tempo_user_work_logs(
                user_key="john.doe",
                date_from="2023-01-01",
                date_to="2023-01-31",
            )

            assert len(result) == 2
            assert all(wl["author"]["username"] == "john.doe" for wl in result)
            assert result[0]["time_spent_seconds"] == 3600
            assert result[1]["time_spent_seconds"] == 1800

    def test_server_worklog_through_transformer(self, mock_jira_client):
        from src.utils.time_entry_transformer import TimeEntryTransformer

        canonical = {"key": "billable_flag", "value": False}
        nested = {"worklogId": 14, "workAttribute": {"id": 3, "key": canonical["key"]}, "value": canonical["value"]}
        row = {
            "id": 71,
            "jiraWorklogId": 14,
            "issue": {"key": "TEST-2"},
            "author": {"name": "worker", "key": "stable"},
            "timeSpentSeconds": 90,
            "dateStarted": "2011-05-02 23:45:00.000",
            "dateCreated": "2011-05-03 02:00:00.000",
            "dateUpdated": "2011-05-04 09:00:00.000",
            "worklogAttributes": [canonical],
            "workAttributeValues": [nested],
        }
        get = mock_jira_client.jira._session.get
        get.return_value = Mock(status_code=200, json=Mock(return_value=[row, {**row, "issue": {"key": "TEST-3"}}]))
        result = mock_jira_client.get_tempo_work_logs(issue_key="TEST-2", user_key="worker")
        assert get.call_args.kwargs["params"] == {"projectKey": "TEST", "username": "worker", "limit": 1000}
        assert len(result) == 1
        source = result[0]
        assert (source["tempo_worklog_id"], source["created"], source["updated"]) == (
            71,
            row["dateCreated"],
            row["dateUpdated"],
        )
        assert source["work_attribute_values"] == [nested]
        target = TimeEntryTransformer(
            user_mapping={"stable": 4}, work_package_mapping={"TEST-2": 9}
        ).transform_tempo_work_log(source)
        assert target["spentOn"] == "2011-05-02"
        assert target["_meta"]["jira_worklog_key"] == "TEST-2:14"
        assert target["_meta"]["tempo_attributes"] == {"billable_flag": False}
        for wrong in (
            {**row, "issue": {"key": "OTHER-2"}},
            {**row, "workAttributeValues": [{**nested, "worklogId": 15}]},
            {**row, "worklogAttributes": [{**canonical, "value": True}]},
        ):
            get.return_value.json.return_value = [wrong]
            with pytest.raises(JiraApiError):
                mock_jira_client.get_tempo_work_logs(project_key="TEST")

    def test_repeated_page_or_later_failure_is_reported(self, mock_jira_client):
        rows = [{"jiraWorklogId": idx, "issue": {"key": "TEST-2"}} for idx in range(1, 1001)]
        first = Mock(status_code=200, json=Mock(return_value=rows))
        session = mock_jira_client.jira._session
        for second in (first, Mock(status_code=403)):
            session.get.side_effect = [first, second]
            with pytest.raises(JiraApiError):
                mock_jira_client.get_tempo_time_entries(project_keys=["TEST"])
