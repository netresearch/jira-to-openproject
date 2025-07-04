"""Tests for Jira client work log functionality."""

import pytest
from unittest.mock import Mock, patch

from src.clients.jira_client import (
    JiraClient,
    JiraApiError,
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

        monkeypatch.setattr("src.clients.jira_client.config", mock_config)

        # Mock the JIRA class to avoid actual API calls
        mock_jira_instance = Mock()

        with patch("src.clients.jira_client.JIRA") as mock_jira_class:
            mock_jira_class.return_value = mock_jira_instance
            mock_jira_instance.server_info.return_value = {
                "baseUrl": "https://test.atlassian.net",
                "version": "8.0.0"
            }

            client = JiraClient()
            client.jira = mock_jira_instance
            return client

    def test_get_work_logs_for_issue_success(self, mock_jira_client):
        """Test successful work log retrieval for an issue."""
        # Create mock work log objects
        mock_work_log_1 = Mock()
        mock_work_log_1.id = "10001"
        mock_work_log_1.started = "2024-01-15T10:00:00.000+0000"
        mock_work_log_1.timeSpent = "2h"
        mock_work_log_1.timeSpentSeconds = 7200
        mock_work_log_1.comment = "Fixed bug in authentication"
        mock_work_log_1.created = "2024-01-15T10:00:00.000+0000"
        mock_work_log_1.updated = "2024-01-15T10:00:00.000+0000"

        # Mock author
        mock_author = Mock()
        mock_author.name = "john.doe"
        mock_author.displayName = "John Doe"
        mock_author.emailAddress = "john.doe@example.com"
        mock_author.accountId = "account123"
        mock_work_log_1.author = mock_author

        # Mock the worklogs method
        mock_jira_client.jira.worklogs.return_value = [mock_work_log_1]

        # Test the method
        result = mock_jira_client.get_work_logs_for_issue("TEST-123")

        # Verify the results
        assert len(result) == 1
        assert result[0]["id"] == "10001"
        assert result[0]["issue_key"] == "TEST-123"
        assert result[0]["time_spent"] == "2h"
        assert result[0]["time_spent_seconds"] == 7200
        assert result[0]["comment"] == "Fixed bug in authentication"
        assert result[0]["author"]["name"] == "john.doe"
        assert result[0]["author"]["display_name"] == "John Doe"

        # Verify API was called correctly
        mock_jira_client.jira.worklogs.assert_called_once_with("TEST-123")

    def test_get_work_logs_for_issue_not_found(self, mock_jira_client):
        """Test work log retrieval for non-existent issue."""
        mock_jira_client.jira.worklogs.side_effect = Exception("Issue does not exist")

        with pytest.raises(JiraResourceNotFoundError, match="Issue TEST-404 not found"):
            mock_jira_client.get_work_logs_for_issue("TEST-404")

    def test_get_work_logs_for_issue_api_error(self, mock_jira_client):
        """Test handling of API errors when retrieving work logs."""
        # Mock the response to raise an exception
        with patch.object(mock_jira_client.jira, "worklogs", side_effect=Exception("API Error")):
            with pytest.raises(JiraApiError, match="Failed to get work logs for issue TEST-123"):
                mock_jira_client.get_work_logs_for_issue("TEST-123")

    def test_get_tempo_work_logs_success(self, mock_jira_client):
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
            }
        ]

        with patch.object(mock_jira_client.jira._session, "get", return_value=mock_response):
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

    def test_get_tempo_work_logs_api_error(self, mock_jira_client):
        """Test handling of API errors when retrieving Tempo work logs."""
        mock_response = Mock()
        mock_response.status_code = 500

        with patch.object(mock_jira_client.jira._session, "get", return_value=mock_response):
            with pytest.raises(JiraApiError, match="Failed to retrieve Tempo work logs"):
                mock_jira_client.get_tempo_work_logs(project_key="TEST")

    def test_get_tempo_work_attributes_success(self, mock_jira_client):
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

        with patch.object(mock_jira_client.jira._session, "get", return_value=mock_response):
            result = mock_jira_client.get_tempo_work_attributes()

            assert len(result) == 2
            assert result[0]["key"] == "_Account_"
            assert result[0]["required"] is True
            assert result[1]["key"] == "_Category_"
            assert result[1]["values"] == ["Development", "Testing", "Documentation"]

    def test_get_tempo_all_work_logs_for_project_pagination(self, mock_jira_client):
        """Test Tempo work logs retrieval with pagination."""
        # Mock first batch (1000 items)
        first_batch = []
        for i in range(1000):
            first_batch.append({
                "tempoWorklogId": i + 1,
                "jiraWorklogId": i + 10000,
                "issue": {"key": f"TEST-{i}", "id": i + 20000},
                "author": {"name": "test.user", "displayName": "Test User"},
                "timeSpentSeconds": 3600,
                "billableSeconds": 3600,
            })

        # Mock second batch (500 items)
        second_batch = []
        for i in range(500):
            second_batch.append({
                "tempoWorklogId": i + 1001,
                "jiraWorklogId": i + 11000,
                "issue": {"key": f"TEST-{i + 1000}", "id": i + 21000},
                "author": {"name": "test.user", "displayName": "Test User"},
                "timeSpentSeconds": 1800,
                "billableSeconds": 1800,
            })

        mock_responses = [
            Mock(status_code=200, json=Mock(return_value=first_batch)),
            Mock(status_code=200, json=Mock(return_value=second_batch)),
        ]

        with patch.object(mock_jira_client.jira._session, "get", side_effect=mock_responses):
            result = mock_jira_client.get_tempo_all_work_logs_for_project("TEST")

            assert len(result) == 1500
            assert result[0]["tempo_worklog_id"] == 1
            assert result[1000]["tempo_worklog_id"] == 1001
            assert result[-1]["tempo_worklog_id"] == 1500

    def test_get_tempo_work_log_by_id_success(self, mock_jira_client):
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

        with patch.object(mock_jira_client.jira._session, "get", return_value=mock_response):
            result = mock_jira_client.get_tempo_work_log_by_id("12345")

            # Verify the result structure
            assert "tempo_worklog_id" in result
            assert result["tempo_worklog_id"] == 12345
            assert result["issue_key"] == "TEST-123"

    def test_get_tempo_work_log_by_id_not_found(self, mock_jira_client):
        """Test handling when Tempo work log is not found."""
        mock_response = Mock()
        mock_response.status_code = 404

        with patch.object(mock_jira_client.jira._session, "get", return_value=mock_response):
            with pytest.raises(JiraResourceNotFoundError, match="Tempo work log 12345 not found"):
                mock_jira_client.get_tempo_work_log_by_id("12345")

    def test_get_tempo_user_work_logs_success(self, mock_jira_client):
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

        with patch.object(mock_jira_client.jira._session, "get", return_value=mock_response):
            result = mock_jira_client.get_tempo_user_work_logs(
                user_key="john.doe",
                date_from="2023-01-01",
                date_to="2023-01-31"
            )

            assert len(result) == 2
            assert all(wl["author"]["username"] == "john.doe" for wl in result)
            assert result[0]["time_spent_seconds"] == 3600
            assert result[1]["time_spent_seconds"] == 1800
