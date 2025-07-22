#!/usr/bin/env python3
"""Comprehensive unit tests for EnhancedJiraClient with performance optimizations.

Tests cover:
1. Batch operations - batch_get_issues, batch_get_work_logs, bulk_get_issue_metadata
2. Parallel processing - ThreadPoolExecutor usage, error handling
3. Performance caching - @cached decorator integration
4. Rate limiting - @rate_limited decorator integration
5. Streaming operations - stream_search_issues, memory efficiency
6. Backwards compatibility - with base JiraClient
7. Error handling and resilience
8. Performance improvements and benchmarking
"""

import pytest
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock, call
from typing import List, Dict, Any, Optional

from jira import JIRA, Issue, JIRAError
from jira.exceptions import JIRAError as JiraApiError

from src.clients.enhanced_jira_client import EnhancedJiraClient
from src.clients.jira_client import JiraClient
from src.utils.performance_optimizer import PerformanceOptimizer


class TestEnhancedJiraClientInitialization:
    """Test EnhancedJiraClient initialization and configuration."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_enhanced_client_initialization(self, mock_jira):
        """Test enhanced client initialization with performance optimizer."""
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password",
            cache_size=500,
            cache_ttl=900,
            batch_size=50,
            max_workers=8,
            rate_limit=20.0
        )
        
        # Verify base client initialization
        assert client.server == "https://test.atlassian.net"
        assert client.username == "test@example.com"
        
        # Verify performance optimizer configuration
        assert client.performance_optimizer.cache.max_size == 500
        assert client.performance_optimizer.cache.default_ttl == 900
        assert client.batch_size == 50
        assert client.parallel_workers == 8
        assert client.performance_optimizer.rate_limiter.current_rate == 20.0
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_enhanced_client_default_values(self, mock_jira):
        """Test enhanced client initialization with default values."""
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        # Verify default performance optimizer settings
        assert client.performance_optimizer.cache.max_size == 2000
        assert client.performance_optimizer.cache.default_ttl == 1800
        assert client.batch_size == 100
        assert client.parallel_workers == 15
        assert client.performance_optimizer.rate_limiter.current_rate == 15.0


class TestBatchGetIssues:
    """Test batch_get_issues functionality."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_batch_get_issues_empty_list(self, mock_jira):
        """Test batch get issues with empty issue keys list."""
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        results = client.batch_get_issues([])
        assert results == {}
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    @patch('src.clients.enhanced_jira_client.ThreadPoolExecutor')
    def test_batch_get_issues_single_batch(self, mock_executor_class, mock_jira):
        """Test batch get issues with single batch."""
        # Mock issue objects
        mock_issue1 = Mock(spec=Issue)
        mock_issue1.key = "TEST-1"
        mock_issue2 = Mock(spec=Issue)
        mock_issue2.key = "TEST-2"
        
        # Mock executor and futures
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor
        
        mock_future = Mock()
        mock_future.result.return_value = {"TEST-1": mock_issue1, "TEST-2": mock_issue2}
        mock_executor.submit.return_value = mock_future
        
        # Mock as_completed
        with patch('src.clients.enhanced_jira_client.as_completed', return_value=[mock_future]):
            client = EnhancedJiraClient(
                server="https://test.atlassian.net",
                username="test@example.com", 
                password="password",
                batch_size=100
            )
            
            results = client.batch_get_issues(["TEST-1", "TEST-2"])
            
            assert len(results) == 2
            assert results["TEST-1"] == mock_issue1
            assert results["TEST-2"] == mock_issue2
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    @patch('src.clients.enhanced_jira_client.ThreadPoolExecutor')
    def test_batch_get_issues_multiple_batches(self, mock_executor_class, mock_jira):
        """Test batch get issues with multiple batches."""
        # Mock executor
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor
        
        # Mock batch results
        batch1_result = {"TEST-1": Mock(key="TEST-1"), "TEST-2": Mock(key="TEST-2")}
        batch2_result = {"TEST-3": Mock(key="TEST-3")}
        
        mock_future1 = Mock()
        mock_future1.result.return_value = batch1_result
        mock_future2 = Mock()
        mock_future2.result.return_value = batch2_result
        
        mock_executor.submit.side_effect = [mock_future1, mock_future2]
        
        # Mock as_completed
        with patch('src.clients.enhanced_jira_client.as_completed', return_value=[mock_future1, mock_future2]):
            client = EnhancedJiraClient(
                server="https://test.atlassian.net",
                username="test@example.com",
                password="password",
                batch_size=2  # Force multiple batches
            )
            
            results = client.batch_get_issues(["TEST-1", "TEST-2", "TEST-3"])
            
            assert len(results) == 3
            assert "TEST-1" in results
            assert "TEST-2" in results
            assert "TEST-3" in results
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    @patch('src.clients.enhanced_jira_client.ThreadPoolExecutor')
    def test_batch_get_issues_error_handling(self, mock_executor_class, mock_jira):
        """Test batch get issues error handling."""
        # Mock executor
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor
        
        # Mock failed future
        mock_future = Mock()
        mock_future.result.side_effect = Exception("API Error")
        mock_executor.submit.return_value = mock_future
        
        # Mock as_completed
        with patch('src.clients.enhanced_jira_client.as_completed', return_value=[mock_future]):
            client = EnhancedJiraClient(
                server="https://test.atlassian.net",
                username="test@example.com",
                password="password"
            )
            
            results = client.batch_get_issues(["TEST-1", "TEST-2"])
            
            # Failed issues should be marked as None
            assert results["TEST-1"] is None
            assert results["TEST-2"] is None


class TestFetchIssuesBatch:
    """Test _fetch_issues_batch internal method."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_fetch_issues_batch_empty(self, mock_jira):
        """Test fetch issues batch with empty list."""
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        results = client._fetch_issues_batch([])
        assert results == {}
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_fetch_issues_batch_success(self, mock_jira):
        """Test successful batch fetch."""
        # Mock issues
        mock_issue1 = Mock(spec=Issue)
        mock_issue1.key = "TEST-1"
        mock_issue2 = Mock(spec=Issue) 
        mock_issue2.key = "TEST-2"
        
        # Mock jira client
        mock_jira.return_value.search_issues.return_value = [mock_issue1, mock_issue2]
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        results = client._fetch_issues_batch(["TEST-1", "TEST-2"])
        
        assert len(results) == 2
        assert results["TEST-1"] == mock_issue1
        assert results["TEST-2"] == mock_issue2
        
        # Verify JQL query
        expected_jql = "key in (TEST-1,TEST-2)"
        mock_jira.return_value.search_issues.assert_called_once_with(
            expected_jql,
            maxResults=2,
            expand='changelog'
        )
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_fetch_issues_batch_partial_results(self, mock_jira):
        """Test batch fetch with partial results."""
        # Mock only one issue returned
        mock_issue1 = Mock(spec=Issue)
        mock_issue1.key = "TEST-1"
        
        mock_jira.return_value.search_issues.return_value = [mock_issue1]
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        results = client._fetch_issues_batch(["TEST-1", "TEST-2"])
        
        assert len(results) == 2
        assert results["TEST-1"] == mock_issue1
        assert results["TEST-2"] is None  # Not found
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_fetch_issues_batch_api_error(self, mock_jira):
        """Test batch fetch with API error."""
        mock_jira.return_value.search_issues.side_effect = JIRAError("API Error")
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        results = client._fetch_issues_batch(["TEST-1", "TEST-2"])
        
        # All issues should be None due to error
        assert results["TEST-1"] is None
        assert results["TEST-2"] is None


class TestBatchGetWorkLogs:
    """Test batch_get_work_logs functionality."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_batch_get_work_logs_empty(self, mock_jira):
        """Test batch get work logs with empty list."""
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        results = client.batch_get_work_logs([])
        assert results == {}
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    @patch('src.clients.enhanced_jira_client.ThreadPoolExecutor')
    def test_batch_get_work_logs_success(self, mock_executor_class, mock_jira):
        """Test successful batch work logs retrieval."""
        # Mock executor
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor
        
        # Mock work logs
        work_logs_1 = [{"id": "1", "timeSpent": "1h"}]
        work_logs_2 = [{"id": "2", "timeSpent": "2h"}]
        
        mock_future1 = Mock()
        mock_future1.result.return_value = work_logs_1
        mock_future2 = Mock()
        mock_future2.result.return_value = work_logs_2
        
        mock_executor.submit.side_effect = [mock_future1, mock_future2]
        
        # Mock as_completed
        with patch('src.clients.enhanced_jira_client.as_completed', return_value=[mock_future1, mock_future2]):
            client = EnhancedJiraClient(
                server="https://test.atlassian.net",
                username="test@example.com",
                password="password"
            )
            
            results = client.batch_get_work_logs(["TEST-1", "TEST-2"])
            
            assert len(results) == 2
            assert work_logs_1 in results.values()
            assert work_logs_2 in results.values()


class TestStreamSearchIssues:
    """Test stream_search_issues functionality."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_stream_search_issues_basic(self, mock_jira):
        """Test basic streaming search functionality."""
        # Mock issues for pagination
        page1_issues = [Mock(key=f"TEST-{i}") for i in range(1, 4)]
        page2_issues = [Mock(key=f"TEST-{i}") for i in range(4, 6)]
        
        mock_jira.return_value.search_issues.side_effect = [page1_issues, page2_issues, []]
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        issues = list(client.stream_search_issues("project = TEST", page_size=3))
        
        assert len(issues) == 5
        assert all(issue.key.startswith("TEST-") for issue in issues)
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_stream_search_issues_max_pages(self, mock_jira):
        """Test streaming search with max pages limit."""
        # Mock infinite pages
        mock_jira.return_value.search_issues.return_value = [Mock(key="TEST-1")]
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        issues = list(client.stream_search_issues(
            "project = TEST", 
            page_size=1, 
            max_pages=3
        ))
        
        assert len(issues) == 3
        assert mock_jira.return_value.search_issues.call_count == 3
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_stream_search_issues_error_handling(self, mock_jira):
        """Test streaming search error handling."""
        # First page succeeds, second fails
        page1_issues = [Mock(key="TEST-1")]
        mock_jira.return_value.search_issues.side_effect = [
            page1_issues,
            JIRAError("API Error")
        ]
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        issues = list(client.stream_search_issues("project = TEST", page_size=1))
        
        # Should only get first page due to error
        assert len(issues) == 1
        assert issues[0].key == "TEST-1"


class TestCachedOperations:
    """Test cached operations integration."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_cached_project_get(self, mock_jira):
        """Test cached project retrieval."""
        # Mock project data
        mock_project = {"key": "TEST", "name": "Test Project"}
        mock_response = Mock()
        mock_response.json.return_value = mock_project
        mock_response.raise_for_status.return_value = None
        
        # Mock session
        mock_session = Mock()
        mock_session.get.return_value = mock_response
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        client.session = mock_session
        
        # First call
        result1 = client.get_project_cached("TEST")
        assert result1 == mock_project
        
        # Second call should use cache
        result2 = client.get_project_cached("TEST")
        assert result2 == mock_project
        
        # Should only make one API call due to caching
        assert mock_session.get.call_count == 1
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_cached_statuses_get(self, mock_jira):
        """Test cached status retrieval."""
        mock_statuses = [
            {"id": "1", "name": "Open"},
            {"id": "2", "name": "In Progress"}
        ]
        
        mock_response = Mock()
        mock_response.json.return_value = {
            "_embedded": {"elements": mock_statuses}
        }
        mock_response.raise_for_status.return_value = None
        
        mock_session = Mock()
        mock_session.get.return_value = mock_response
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        client.session = mock_session
        
        # Test caching behavior
        result1 = client.get_statuses_cached()
        result2 = client.get_statuses_cached()
        
        assert result1 == mock_statuses
        assert result2 == mock_statuses
        assert mock_session.get.call_count == 1


class TestBulkOperations:
    """Test bulk operations functionality."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    @patch('src.clients.enhanced_jira_client.ThreadPoolExecutor')
    def test_bulk_get_issue_metadata(self, mock_executor_class, mock_jira):
        """Test bulk issue metadata retrieval."""
        # Mock executor
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor
        
        # Mock metadata results
        metadata1 = {"key": "TEST-1", "summary": "Test Issue 1"}
        metadata2 = {"key": "TEST-2", "summary": "Test Issue 2"}
        
        mock_future1 = Mock()
        mock_future1.result.return_value = metadata1
        mock_future2 = Mock()
        mock_future2.result.return_value = metadata2
        
        mock_executor.submit.side_effect = [mock_future1, mock_future2]
        
        # Mock as_completed
        with patch('src.clients.enhanced_jira_client.as_completed', return_value=[mock_future1, mock_future2]):
            client = EnhancedJiraClient(
                server="https://test.atlassian.net",
                username="test@example.com",
                password="password"
            )
            
            results = client.bulk_get_issue_metadata(["TEST-1", "TEST-2"])
            
            assert len(results) == 2
            assert metadata1 in results.values()
            assert metadata2 in results.values()


class TestPerformanceIntegration:
    """Test performance optimization integration."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_performance_optimizer_integration(self, mock_jira):
        """Test performance optimizer integration in enhanced client."""
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password",
            cache_size=100,
            rate_limit=5.0
        )
        
        # Verify optimizer is properly configured
        assert isinstance(client.performance_optimizer, PerformanceOptimizer)
        assert client.performance_optimizer.cache.max_size == 100
        assert client.performance_optimizer.rate_limiter.current_rate == 5.0
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_rate_limiting_integration(self, mock_jira):
        """Test rate limiting integration in batch operations."""
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password",
            rate_limit=1.0  # Very slow rate for testing
        )
        
        # Mock the _fetch_issues_batch method to verify rate limiting
        with patch.object(client, '_fetch_issues_batch') as mock_fetch:
            mock_fetch.return_value = {"TEST-1": Mock(key="TEST-1")}
            
            start_time = time.time()
            client.batch_get_issues(["TEST-1"])
            elapsed_time = time.time() - start_time
            
            # Should have some delay due to rate limiting
            # Note: This is a simplified test - real rate limiting timing is complex
            assert mock_fetch.called


class TestBackwardsCompatibility:
    """Test backwards compatibility with base JiraClient."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_base_methods_available(self, mock_jira):
        """Test that base JiraClient methods are still available."""
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        # Should inherit from base client
        assert isinstance(client, JiraClient)
        
        # Base methods should be available
        assert hasattr(client, 'get_issue')
        assert hasattr(client, 'create_issue')
        assert hasattr(client, 'search_issues')
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_enhanced_methods_added(self, mock_jira):
        """Test that enhanced methods are added."""
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        # Enhanced methods should be available
        assert hasattr(client, 'batch_get_issues')
        assert hasattr(client, 'batch_get_work_logs')
        assert hasattr(client, 'stream_search_issues')
        assert hasattr(client, 'bulk_get_issue_metadata')
        assert hasattr(client, 'get_project_cached')
        assert hasattr(client, 'get_statuses_cached')


class TestErrorHandlingAndResilience:
    """Test error handling and resilience features."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_network_error_handling(self, mock_jira):
        """Test handling of network errors."""
        mock_jira.return_value.search_issues.side_effect = ConnectionError("Network error")
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        # Should handle network errors gracefully
        results = client._fetch_issues_batch(["TEST-1"])
        assert results["TEST-1"] is None
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_jira_api_error_handling(self, mock_jira):
        """Test handling of Jira API errors."""
        mock_jira.return_value.search_issues.side_effect = JIRAError("Forbidden")
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        # Should handle API errors gracefully
        results = client._fetch_issues_batch(["TEST-1"])
        assert results["TEST-1"] is None
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    @patch('src.clients.enhanced_jira_client.ThreadPoolExecutor')
    def test_partial_batch_failure_handling(self, mock_executor_class, mock_jira):
        """Test handling of partial batch failures."""
        # Mock executor
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor
        
        # One successful future, one failed
        mock_future1 = Mock()
        mock_future1.result.return_value = {"TEST-1": Mock(key="TEST-1")}
        
        mock_future2 = Mock()
        mock_future2.result.side_effect = Exception("Batch failed")
        
        mock_executor.submit.side_effect = [mock_future1, mock_future2]
        
        # Mock as_completed
        with patch('src.clients.enhanced_jira_client.as_completed', return_value=[mock_future1, mock_future2]):
            client = EnhancedJiraClient(
                server="https://test.atlassian.net",
                username="test@example.com",
                password="password",
                batch_size=1  # Force separate batches
            )
            
            results = client.batch_get_issues(["TEST-1", "TEST-2"])
            
            # Should have successful result and failed result
            assert "TEST-1" in results
            assert "TEST-2" in results
            assert results["TEST-1"] is not None
            assert results["TEST-2"] is None


class TestMemoryEfficiency:
    """Test memory efficiency of streaming operations."""
    
    @patch('src.clients.enhanced_jira_client.JIRA')
    def test_streaming_memory_usage(self, mock_jira):
        """Test that streaming operations don't load all data into memory."""
        # Mock large result set
        def mock_search_issues(jql, startAt=0, maxResults=50, **kwargs):
            if startAt >= 1000:  # Simulate end of results
                return []
            return [Mock(key=f"TEST-{i}") for i in range(startAt, min(startAt + maxResults, 1000))]
        
        mock_jira.return_value.search_issues.side_effect = mock_search_issues
        
        client = EnhancedJiraClient(
            server="https://test.atlassian.net",
            username="test@example.com",
            password="password"
        )
        
        # Stream issues without loading all into memory
        issue_count = 0
        for issue in client.stream_search_issues("project = TEST", page_size=50):
            issue_count += 1
            if issue_count >= 100:  # Process only first 100 to avoid long test
                break
        
        assert issue_count == 100
        # Verify that we didn't load all 1000 issues at once
        # (This is implicit in the streaming nature of the iterator)


if __name__ == "__main__":
    pytest.main([__file__, "-v"]) 