#!/usr/bin/env python3
"""End-to-End Integration Tests for Enhanced User Association Migrator.

This module tests Task 76.6 implementation - complete workflows that exercise:
1. User association migration with MetricsCollector integration
2. Staleness detection with cache seeding
3. Refresh workflows with JiraClient responses
4. Fallback strategies with comprehensive monitoring
5. Cache updates, logs, and metrics verification across scenarios
"""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.utils.enhanced_user_association_migrator import (
    EnhancedUserAssociationMigrator,
)
from src.utils.metrics_collector import MetricsCollector


class TestEndToEndUserAssociationMigration:
    """End-to-end integration tests for complete user association migration workflows."""

    @pytest.fixture
    def mock_jira_client(self):
        """Create a mock Jira client with realistic responses."""
        return Mock(spec=JiraClient)

    @pytest.fixture
    def mock_op_client(self):
        """Create a mock OpenProject client with realistic responses."""
        return Mock(spec=OpenProjectClient)

    @pytest.fixture
    def metrics_collector(self):
        """Create a real MetricsCollector for end-to-end testing."""
        return MetricsCollector()

    @pytest.fixture
    def test_jira_issue(self):
        """Sample Jira issue data for testing."""
        # Create a mock object that mimics the actual Jira issue structure
        issue = Mock()
        issue.key = "TEST-123"
        issue.fields = Mock()
        issue.fields.assignee = Mock()
        issue.fields.assignee.name = "john.doe"
        issue.fields.assignee.displayName = "John Doe"

        issue.fields.reporter = Mock()
        issue.fields.reporter.name = "jane.smith"
        issue.fields.reporter.displayName = "Jane Smith"

        issue.fields.creator = Mock()
        issue.fields.creator.name = "jane.smith"
        issue.fields.creator.displayName = "Jane Smith"

        issue.fields.watches = Mock()
        issue.fields.watches.watchers = [
            Mock(name="bob.wilson", displayName="Bob Wilson"),
            Mock(name="alice.brown", displayName="Alice Brown"),
        ]

        return issue

    @pytest.fixture
    def seeded_migrator(self, mock_jira_client, mock_op_client, metrics_collector):
        """Create migrator with pre-seeded cache entries for testing."""
        with (
            patch(
                "src.utils.enhanced_user_association_migrator.config.migration_config",
                {
                    "mapping": {
                        "refresh_interval": "1h",
                        "fallback_strategy": "skip",
                        "admin_user_id": 999,
                    },
                },
            ),
            patch(
                "src.utils.enhanced_user_association_migrator.config.get_path",
            ) as mock_path,
        ):
            # Setup file system mocks
            mock_path.return_value.exists.return_value = False
            mock_path.return_value.open.return_value.__enter__.return_value.read.return_value = (
                "{}"
            )

            # Setup OpenProject client mocks for fallback user identification
            mock_op_client.get_users.return_value = [
                {"id": 999, "login": "admin", "admin": True, "status": 1},
            ]

            migrator = EnhancedUserAssociationMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client,
                user_mapping={},
                metrics_collector=metrics_collector,
            )

            # Seed cache with test data
            current_time = datetime.now(tz=UTC)
            old_time = current_time - timedelta(hours=2)  # Stale
            very_old_time = current_time - timedelta(days=1)  # Very stale

            migrator.enhanced_user_mappings = {
                # Fresh mapping
                "john.doe": {
                    "jira_username": "john.doe",
                    "openproject_user_id": 123,
                    "mapping_status": "mapped",
                    "lastRefreshed": current_time.isoformat(),
                    "metadata": {"jira_active": True, "openproject_active": True},
                },
                # Stale mapping that can be refreshed successfully
                "jane.smith": {
                    "jira_username": "jane.smith",
                    "openproject_user_id": 456,
                    "mapping_status": "mapped",
                    "lastRefreshed": old_time.isoformat(),
                    "metadata": {"jira_active": True, "openproject_active": True},
                },
                # Very stale mapping for refresh failure scenarios
                "bob.wilson": {
                    "jira_username": "bob.wilson",
                    "openproject_user_id": 789,
                    "mapping_status": "mapped",
                    "lastRefreshed": very_old_time.isoformat(),
                    "metadata": {"jira_active": True, "openproject_active": True},
                },
                # alice.brown is missing from cache (cache miss scenario)
            }

            return migrator

    def test_end_to_end_fresh_mapping_workflow(
        self,
        seeded_migrator,
        metrics_collector,
        test_jira_issue,
        caplog,
    ) -> None:
        """Test complete workflow with fresh mapping - should use cache without refresh."""
        with caplog.at_level(logging.DEBUG):
            # Execute migration for assignee with fresh mapping
            work_package_data = {"jira_key": "TEST-123"}
            result = seeded_migrator.migrate_user_associations(
                test_jira_issue,
                work_package_data,
                preserve_creator_via_rails=False,
            )

        # Verify successful mapping
        assert result["status"] == "success"
        assert work_package_data["assigned_to_id"] == 123  # john.doe's ID
        assert (
            work_package_data["author_id"] == 456
        )  # jane.smith's ID (will be refreshed)

        # Verify no staleness detected for fresh user
        fresh_staleness_count = metrics_collector.get_tagged_counter(
            "staleness_detected_total",
            {"reason": "missing", "username": "john.doe"},
        )
        assert fresh_staleness_count == 0

        # Verify cache hit logging for fresh mapping
        debug_logs = [
            record.message for record in caplog.records if record.levelname == "DEBUG"
        ]
        assert any("Cache hit for john.doe" in log for log in debug_logs)

        # Verify stale mapping refresh for jane.smith (author)
        stale_staleness_count = metrics_collector.get_tagged_counter(
            "staleness_detected_total",
            {"reason": "expired", "username": "jane.smith"},
        )
        assert stale_staleness_count > 0

    def test_end_to_end_stale_refresh_success_workflow(
        self,
        seeded_migrator,
        metrics_collector,
        mock_jira_client,
        caplog,
    ) -> None:
        """Test complete workflow with stale mapping that refreshes successfully."""
        # Mock successful Jira response for refresh
        mock_jira_client.get.return_value.status_code = 200
        mock_jira_client.get.return_value.json.return_value = [
            {
                "name": "jane.smith",
                "displayName": "Jane Smith (Updated)",
                "emailAddress": "jane.smith@company.com",
                "active": True,
                "accountId": "jane-account-123",
            },
        ]

        with (
            patch.object(seeded_migrator, "_save_enhanced_mappings"),
            patch.object(
                seeded_migrator,
                "_validate_refreshed_user",
                return_value={"is_valid": True},
            ),
            caplog.at_level(logging.DEBUG),
        ):
            # Execute staleness check with auto-refresh
            mapping = seeded_migrator.get_mapping_with_staleness_check(
                "jane.smith",
                auto_refresh=True,
            )

        # Verify successful refresh
        assert mapping is not None
        assert mapping["metadata"]["jira_display_name"] == "Jane Smith (Updated)"
        assert mapping["metadata"]["refresh_success"] is True

        # Verify staleness detection metrics
        assert metrics_collector.get_counter("staleness_detected_total") == 1
        assert (
            metrics_collector.get_tagged_counter(
                "staleness_detected_total",
                {"reason": "expired", "username": "jane.smith"},
            )
            == 1
        )

        # Verify refresh success metrics
        assert metrics_collector.get_counter("staleness_refreshed_total") == 1
        assert (
            metrics_collector.get_tagged_counter(
                "staleness_refreshed_total",
                {
                    "success": "true",
                    "username": "jane.smith",
                    "trigger": "auto_refresh",
                },
            )
            == 1
        )

        # Verify logging for complete workflow
        debug_logs = [
            record.message for record in caplog.records if record.levelname == "DEBUG"
        ]
        assert any("Staleness detected for jane.smith" in log for log in debug_logs)
        assert any("Attempting automatic refresh" in log for log in debug_logs)
        assert any(
            "Successfully refreshed mapping for jane.smith" in log for log in debug_logs
        )

    def test_end_to_end_stale_refresh_failure_workflow(
        self,
        seeded_migrator,
        metrics_collector,
        mock_jira_client,
        caplog,
    ) -> None:
        """Test complete workflow with stale mapping that fails to refresh."""
        # Mock failed Jira response (user not found)
        mock_jira_client.get.return_value.status_code = 404

        with (
            patch.object(seeded_migrator, "_save_enhanced_mappings"),
            caplog.at_level(logging.DEBUG),
        ):
            # Execute staleness check with auto-refresh
            mapping = seeded_migrator.get_mapping_with_staleness_check(
                "bob.wilson",
                auto_refresh=True,
            )

        # Verify refresh failure
        assert mapping is None

        # Verify staleness detection metrics
        assert metrics_collector.get_counter("staleness_detected_total") == 1
        assert (
            metrics_collector.get_tagged_counter(
                "staleness_detected_total",
                {"reason": "expired", "username": "bob.wilson"},
            )
            == 1
        )

        # Verify refresh failure metrics
        assert metrics_collector.get_counter("staleness_refreshed_total") == 1
        assert (
            metrics_collector.get_tagged_counter(
                "staleness_refreshed_total",
                {
                    "success": "false",
                    "username": "bob.wilson",
                    "trigger": "auto_refresh",
                },
            )
            == 1
        )

        # Verify logging for failed workflow
        debug_logs = [
            record.message for record in caplog.records if record.levelname == "DEBUG"
        ]
        assert any(
            "Failed to refresh mapping for bob.wilson" in log for log in debug_logs
        )

    def test_end_to_end_cache_miss_workflow(
        self,
        seeded_migrator,
        metrics_collector,
        test_jira_issue,
        caplog,
    ) -> None:
        """Test complete workflow with cache miss for unknown user."""
        with caplog.at_level(logging.DEBUG):
            # Execute migration with user not in cache
            work_package_data = {"jira_key": "TEST-123"}
            result = seeded_migrator.migrate_user_associations(
                test_jira_issue,
                work_package_data,
                preserve_creator_via_rails=False,
            )

        # Verify cache miss handling
        assert result["status"] == "success"  # Should continue despite cache miss
        assert len(result["warnings"]) > 0  # Should have warnings for unmapped users

        # Verify cache miss metrics for alice.brown (watcher)
        assert metrics_collector.get_counter("staleness_detected_total") >= 1
        cache_miss_count = metrics_collector.get_tagged_counter(
            "staleness_detected_total",
            {"reason": "missing", "username": "alice.brown"},
        )
        assert cache_miss_count == 1

        # Verify cache miss logging
        debug_logs = [
            record.message for record in caplog.records if record.levelname == "DEBUG"
        ]
        assert any("Cache miss for alice.brown" in log for log in debug_logs)

    def test_end_to_end_fallback_skip_workflow(
        self,
        seeded_migrator,
        metrics_collector,
        mock_jira_client,
        caplog,
    ) -> None:
        """Test complete workflow with skip fallback strategy."""
        # Mock Jira response for inactive user
        mock_jira_client.get.return_value.status_code = 200
        mock_jira_client.get.return_value.json.return_value = [
            {
                "name": "bob.wilson",
                "displayName": "Bob Wilson (Inactive)",
                "emailAddress": "bob.wilson@company.com",
                "active": False,  # Inactive user
                "accountId": "bob-account-789",
            },
        ]

        with (
            patch.object(seeded_migrator, "_save_enhanced_mappings"),
            patch.object(
                seeded_migrator,
                "_validate_refreshed_user",
                return_value={"is_valid": False, "reason": "user_inactive"},
            ),
            caplog.at_level(logging.DEBUG),
        ):
            # Execute refresh that will trigger fallback
            mapping = seeded_migrator.refresh_user_mapping("bob.wilson")

        # Verify fallback execution (returns None for skip strategy)
        assert mapping is None

        # Verify fallback metrics
        assert metrics_collector.get_counter("mapping_fallback_total") == 1
        assert (
            metrics_collector.get_tagged_counter(
                "mapping_fallback_total",
                {"fallback_strategy": "skip", "reason": "user_inactive"},
            )
            == 1
        )

        # Verify fallback logging
        warning_logs = [
            record.message for record in caplog.records if record.levelname == "WARNING"
        ]
        assert any(
            "Skipping user mapping for bob.wilson" in log for log in warning_logs
        )

    def test_end_to_end_fallback_create_placeholder_workflow(
        self,
        mock_jira_client,
        mock_op_client,
        metrics_collector,
        caplog,
    ) -> None:
        """Test complete workflow with create_placeholder fallback strategy."""
        with patch(
            "src.utils.enhanced_user_association_migrator.config.migration_config",
            {
                "mapping": {
                    "refresh_interval": "1h",
                    "fallback_strategy": "create_placeholder",  # Different strategy
                },
            },
        ):
            with patch(
                "src.utils.enhanced_user_association_migrator.config.get_path",
            ) as mock_path:
                mock_path.return_value.exists.return_value = False

                migrator = EnhancedUserAssociationMigrator(
                    jira_client=mock_jira_client,
                    op_client=mock_op_client,
                    user_mapping={},
                    metrics_collector=metrics_collector,
                )

        # Mock Jira response for validation failure
        jira_user_data = {
            "name": "problem.user",
            "displayName": "Problem User",
            "emailAddress": "problem@company.com",
            "active": False,
            "accountId": "problem-account-999",
        }

        current_mapping = {"jira_username": "problem.user", "metadata": {}}

        with (
            patch.object(migrator, "_save_enhanced_mappings"),
            caplog.at_level(logging.DEBUG),
        ):
            # Execute create_placeholder fallback
            result = migrator._execute_create_placeholder_fallback(
                "problem.user",
                "validation_failed",
                current_mapping,
                jira_user_data,
            )

        # Verify placeholder creation
        assert result is not None
        assert result["mapping_status"] == "placeholder"
        assert result["metadata"]["is_placeholder"] is True
        assert result["metadata"]["needs_manual_review"] is True

        # Verify fallback metrics
        assert metrics_collector.get_counter("mapping_fallback_total") == 1
        assert (
            metrics_collector.get_tagged_counter(
                "mapping_fallback_total",
                {
                    "fallback_strategy": "create_placeholder",
                    "reason": "validation_failed",
                },
            )
            == 1
        )

    def test_end_to_end_batch_refresh_workflow(
        self,
        seeded_migrator,
        metrics_collector,
        mock_jira_client,
        caplog,
    ) -> None:
        """Test complete batch refresh workflow with mixed success/failure."""

        # Mock mixed Jira responses
        def mock_jira_response(url):
            response = Mock()
            if "jane.smith" in url:
                response.status_code = 200
                response.json.return_value = [
                    {
                        "name": "jane.smith",
                        "displayName": "Jane Smith (Updated)",
                        "active": True,
                        "accountId": "jane-123",
                    },
                ]
            else:  # bob.wilson
                response.status_code = 404
            return response

        mock_jira_client.get.side_effect = lambda url: mock_jira_response(url)

        with (
            patch.object(seeded_migrator, "_save_enhanced_mappings"),
            patch.object(
                seeded_migrator,
                "_validate_refreshed_user",
                return_value={"is_valid": True},
            ),
            caplog.at_level(logging.DEBUG),
        ):
            # Execute batch refresh for stale users
            results = seeded_migrator.batch_refresh_stale_mappings(
                ["jane.smith", "bob.wilson"],
            )

        # Verify batch results
        assert results["refresh_attempted"] == 2
        assert results["refresh_successful"] == 1  # jane.smith
        assert results["refresh_failed"] == 1  # bob.wilson

        # Verify metrics for both users
        assert metrics_collector.get_counter("staleness_refreshed_total") == 2

        # Verify individual success/failure metrics
        success_count = metrics_collector.get_tagged_counter(
            "staleness_refreshed_total",
            {
                "success": "true",
                "username": "jane.smith",
                "trigger": "batch_refresh",
                "attempts": "1",
            },
        )
        assert success_count == 1

        failure_count = metrics_collector.get_tagged_counter(
            "staleness_refreshed_total",
            {
                "success": "false",
                "username": "bob.wilson",
                "trigger": "batch_refresh",
                "attempts": "1",
            },
        )
        assert failure_count == 1

    def test_end_to_end_migration_workflow_with_all_scenarios(
        self,
        seeded_migrator,
        metrics_collector,
        mock_jira_client,
        test_jira_issue,
        caplog,
    ) -> None:
        """Test complete migration workflow exercising all user scenarios in one test."""

        # Mock Jira responses for different scenarios
        def mock_jira_response(url):
            response = Mock()
            if "jane.smith" in url:  # Stale user - successful refresh
                response.status_code = 200
                response.json.return_value = [
                    {
                        "name": "jane.smith",
                        "displayName": "Jane Smith (Refreshed)",
                        "active": True,
                        "accountId": "jane-refreshed-123",
                    },
                ]
            elif (
                "bob.wilson" in url or "alice.brown" in url
            ):  # Stale user - refresh failure
                response.status_code = 404
            else:
                response.status_code = 404
            return response

        mock_jira_client.get.side_effect = lambda url: mock_jira_response(url)

        with (
            patch.object(seeded_migrator, "_save_enhanced_mappings"),
            patch.object(
                seeded_migrator,
                "_validate_refreshed_user",
                return_value={"is_valid": True},
            ),
            caplog.at_level(logging.DEBUG),
        ):
            # Execute complete migration workflow
            work_package_data = {"jira_key": "TEST-123"}
            result = seeded_migrator.migrate_user_associations(
                test_jira_issue,
                work_package_data,
                preserve_creator_via_rails=False,
            )

        # Verify migration completed successfully despite mixed scenarios
        assert result["status"] == "success"
        assert work_package_data["assigned_to_id"] == 123  # john.doe (fresh)
        assert work_package_data["author_id"] == 456  # jane.smith (refreshed)
        # Watchers will have warnings but migration continues

        # Verify comprehensive metrics collection
        staleness_total = metrics_collector.get_counter("staleness_detected_total")
        refresh_total = metrics_collector.get_counter("staleness_refreshed_total")

        assert staleness_total >= 3  # jane.smith, bob.wilson, alice.brown
        assert refresh_total >= 2  # jane.smith (success), bob.wilson (failure)

        # Verify metrics summary provides complete picture
        summary = metrics_collector.get_summary()
        assert "staleness_detected_total" in summary["metric_names"]
        assert "staleness_refreshed_total" in summary["metric_names"]

        # Verify logging captures all workflow stages
        debug_logs = [
            record.message for record in caplog.records if record.levelname == "DEBUG"
        ]
        assert any("Cache hit for john.doe" in log for log in debug_logs)  # Fresh
        assert any(
            "Staleness detected for jane.smith" in log for log in debug_logs
        )  # Stale
        assert any("Cache miss for" in log for log in debug_logs)  # Missing

    def test_end_to_end_metrics_persistence_and_aggregation(
        self,
        seeded_migrator,
        metrics_collector,
    ) -> None:
        """Test that metrics persist correctly across multiple operations and aggregate properly."""
        # Execute multiple operations to build up metrics
        seeded_migrator.check_and_handle_staleness("missing1", raise_on_stale=False)
        seeded_migrator.check_and_handle_staleness("missing2", raise_on_stale=False)
        seeded_migrator.check_and_handle_staleness(
            "jane.smith",
            raise_on_stale=False,
        )  # Stale

        # Verify metrics aggregation
        assert metrics_collector.get_counter("staleness_detected_total") == 3

        # Verify tagged counter aggregation by reason
        missing_total = metrics_collector.get_tagged_counter(
            "staleness_detected_total",
            {"reason": "missing", "username": "missing1"},
        ) + metrics_collector.get_tagged_counter(
            "staleness_detected_total",
            {"reason": "missing", "username": "missing2"},
        )
        assert missing_total == 2

        expired_total = metrics_collector.get_tagged_counter(
            "staleness_detected_total",
            {"reason": "expired", "username": "jane.smith"},
        )
        assert expired_total == 1

        # Verify metrics summary aggregates correctly
        summary = metrics_collector.get_summary()
        assert summary["total_count"] == 3
        assert (
            len(summary["tagged_counters"]["staleness_detected_total"]) == 3
        )  # 3 unique tag combinations

    def test_end_to_end_error_recovery_and_resilience(
        self,
        seeded_migrator,
        metrics_collector,
        mock_jira_client,
        caplog,
    ) -> None:
        """Test end-to-end resilience when components fail but workflow continues."""
        # Simulate various failures
        mock_jira_client.get.side_effect = Exception("Network error")

        # Broken metrics (should not affect core functionality)
        broken_metrics = Mock(spec=MetricsCollector)
        broken_metrics.increment_counter.side_effect = Exception(
            "Metrics system failure",
        )
        original_metrics = seeded_migrator.metrics_collector
        seeded_migrator.metrics_collector = broken_metrics

        try:
            with caplog.at_level(logging.DEBUG):
                # Should continue despite both Jira and metrics failures
                mapping = seeded_migrator.get_mapping_with_staleness_check(
                    "jane.smith",
                    auto_refresh=True,
                )

            # Core functionality should work despite failures
            assert mapping is None  # Failed refresh, but no exception

            # Verify error logging
            error_logs = [
                record.message
                for record in caplog.records
                if record.levelname in ["ERROR", "WARNING"]
            ]
            assert len(error_logs) > 0  # Should log the failure

        finally:
            # Restore original metrics collector
            seeded_migrator.metrics_collector = original_metrics

        # Verify that after restoring metrics, everything works normally
        mapping = seeded_migrator.check_and_handle_staleness(
            "john.doe",
            raise_on_stale=False,
        )
        assert mapping is not None  # Fresh mapping should work
        assert (
            metrics_collector.get_counter("staleness_detected_total") == 0
        )  # No staleness for fresh user
