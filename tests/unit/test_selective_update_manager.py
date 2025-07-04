#!/usr/bin/env python3
"""Unit tests for SelectiveUpdateManager."""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.utils.change_detector import ChangeReport
from src.utils.selective_update_manager import (
    SelectiveUpdateManager,
    UpdateOperation,
    UpdatePlan,
    UpdateResult,
    UpdateStrategy,
)
from src.utils.state_manager import StateManager


class TestSelectiveUpdateManager:
    """Test cases for SelectiveUpdateManager."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    @pytest.fixture
    def mock_clients(self):
        """Create mock clients."""
        jira_client = Mock(spec=JiraClient)
        op_client = Mock(spec=OpenProjectClient)
        return jira_client, op_client

    @pytest.fixture
    def mock_state_manager(self):
        """Create mock state manager."""
        return Mock(spec=StateManager)

    @pytest.fixture
    def manager(self, temp_dir, mock_clients, mock_state_manager):
        """Create SelectiveUpdateManager instance for testing."""
        jira_client, op_client = mock_clients
        return SelectiveUpdateManager(
            jira_client=jira_client,
            op_client=op_client,
            state_manager=mock_state_manager,
            update_dir=temp_dir
        )

    @pytest.fixture
    def sample_change_report(self):
        """Create a sample change report for testing."""
        return ChangeReport(
            entity_type="users",
            created_at=datetime.now(tz=UTC).isoformat(),
            jira_count=10,
            openproject_count=8,
            changes=[
                {
                    "entity_type": "users",
                    "entity_id": "user1",
                    "change_type": "created",
                    "priority": 8,
                    "new_data": {"displayName": "John Doe", "email": "john@example.com"},
                    "old_data": None,
                    "metadata": {"source": "jira"}
                },
                {
                    "entity_type": "users",
                    "entity_id": "user2",
                    "change_type": "updated",
                    "priority": 6,
                    "new_data": {"displayName": "Jane Smith", "email": "jane@example.com"},
                    "old_data": {"displayName": "Jane Doe", "email": "jane@example.com"},
                    "metadata": {"source": "jira"}
                },
                {
                    "entity_type": "projects",
                    "entity_id": "proj1",
                    "change_type": "created",
                    "priority": 7,
                    "new_data": {"name": "Test Project", "key": "TEST"},
                    "old_data": None,
                    "metadata": {"source": "jira"}
                }
            ],
            metadata={"total_changes": 3}
        )

    def test_initialization(self, temp_dir, mock_clients, mock_state_manager):
        """Test SelectiveUpdateManager initialization."""
        jira_client, op_client = mock_clients

        manager = SelectiveUpdateManager(
            jira_client=jira_client,
            op_client=op_client,
            state_manager=mock_state_manager,
            update_dir=temp_dir
        )

        assert manager.jira_client == jira_client
        assert manager.op_client == op_client
        assert manager.state_manager == mock_state_manager
        assert manager.update_dir == temp_dir

        # Check directory structure was created
        assert (temp_dir / "plans").exists()
        assert (temp_dir / "results").exists()
        assert (temp_dir / "cache").exists()

        # Check default strategies were initialized
        assert "users" in manager._update_strategies
        assert "projects" in manager._update_strategies
        assert "customfields" in manager._update_strategies

    def test_default_strategies_initialization(self, manager):
        """Test that default update strategies are properly initialized."""
        strategies = manager.get_registered_strategies()

        # Check users strategy
        assert "users" in strategies
        users_strategy = strategies["users"]
        assert users_strategy["entity_type"] == "users"
        assert users_strategy["priority"] == 9
        assert users_strategy["batch_size"] == 50
        assert users_strategy["depends_on"] == []
        assert users_strategy["create_handler"] is not None
        assert users_strategy["update_handler"] is not None
        assert users_strategy["delete_handler"] is not None

        # Check projects strategy
        assert "projects" in strategies
        projects_strategy = strategies["projects"]
        assert projects_strategy["entity_type"] == "projects"
        assert projects_strategy["priority"] == 8
        assert projects_strategy["depends_on"] == ["users"]

        # Check customfields strategy
        assert "customfields" in strategies
        customfields_strategy = strategies["customfields"]
        assert customfields_strategy["entity_type"] == "customfields"
        assert customfields_strategy["priority"] == 7
        assert customfields_strategy["depends_on"] == []

    def test_register_update_strategy(self, manager):
        """Test registering a custom update strategy."""
        custom_strategy = UpdateStrategy(
            entity_type="issues",
            create_handler=lambda x: {"id": "test", "created": True},
            update_handler=lambda x, y: {"id": "test", "updated": True},
            delete_handler=lambda x: True,
            depends_on=["projects", "users"],
            batch_size=25,
            priority=5
        )

        manager.register_update_strategy(custom_strategy)

        strategies = manager.get_registered_strategies()
        assert "issues" in strategies
        assert strategies["issues"] == custom_strategy

    def test_analyze_changes(self, manager, sample_change_report):
        """Test analyzing changes to create an update plan."""
        update_plan = manager.analyze_changes(sample_change_report)

        assert isinstance(update_plan, dict)
        assert "plan_id" in update_plan
        assert update_plan["plan_id"].startswith("plan_")
        assert update_plan["total_operations"] == 3
        assert len(update_plan["operations"]) == 3
        assert set(update_plan["entity_types"]) == {"users", "projects"}

        # Check dependency order (users should come before projects)
        assert update_plan["dependency_order"] == ["users", "projects"]

        # Check operations were created correctly
        operations = update_plan["operations"]
        user_ops = [op for op in operations if op["entity_type"] == "users"]
        project_ops = [op for op in operations if op["entity_type"] == "projects"]

        assert len(user_ops) == 2  # user1 created, user2 updated
        assert len(project_ops) == 1  # proj1 created

        # Check operation details
        create_user_op = next(op for op in user_ops if op["change_type"] == "created")
        assert create_user_op["entity_id"] == "user1"
        assert create_user_op["priority"] == 8
        assert create_user_op["jira_data"]["displayName"] == "John Doe"

    def test_analyze_changes_no_strategies(self, manager, sample_change_report):
        """Test analyzing changes when no strategies are registered for some entity types."""
        # Remove all strategies except users
        manager._update_strategies = {
            "users": manager._update_strategies["users"]
        }

        update_plan = manager.analyze_changes(sample_change_report)

        # Should only include operations for entities with strategies
        assert update_plan["total_operations"] == 2  # Only user operations
        assert set(update_plan["entity_types"]) == {"users"}
        operations = update_plan["operations"]
        assert all(op["entity_type"] == "users" for op in operations)

    def test_resolve_dependency_order(self, manager):
        """Test dependency order resolution."""
        # Add custom strategies with dependencies
        manager.register_update_strategy(UpdateStrategy(
            entity_type="issues",
            create_handler=None,
            update_handler=None,
            delete_handler=None,
            depends_on=["projects", "customfields"],
            batch_size=10,
            priority=5
        ))

        manager.register_update_strategy(UpdateStrategy(
            entity_type="statuses",
            create_handler=None,
            update_handler=None,
            delete_handler=None,
            depends_on=[],
            batch_size=20,
            priority=6
        ))

        entity_types = ["issues", "projects", "users", "customfields", "statuses"]
        order = manager._resolve_dependency_order(entity_types)

        # Check that dependencies are respected
        users_idx = order.index("users")
        projects_idx = order.index("projects")
        customfields_idx = order.index("customfields")
        issues_idx = order.index("issues")

        assert users_idx < projects_idx  # users before projects
        assert projects_idx < issues_idx  # projects before issues
        assert customfields_idx < issues_idx  # customfields before issues

    def test_estimate_plan_duration(self, manager):
        """Test plan duration estimation."""
        operations = [
            UpdateOperation(
                operation_id="op1",
                entity_type="users",
                change_type="created",
                entity_id="user1",
                priority=8,
                jira_data={},
                openproject_data=None,
                depends_on=[],
                metadata={}
            ),
            UpdateOperation(
                operation_id="op2",
                entity_type="users",
                change_type="updated",
                entity_id="user2",
                priority=6,
                jira_data={},
                openproject_data={},
                depends_on=[],
                metadata={}
            ),
            UpdateOperation(
                operation_id="op3",
                entity_type="users",
                change_type="deleted",
                entity_id="user3",
                priority=4,
                jira_data=None,
                openproject_data={},
                depends_on=[],
                metadata={}
            )
        ]

        duration = manager._estimate_plan_duration(operations)

        # Expected: 2.0 (created) + 1.5 (updated) + 1.0 (deleted) + 0.3 (overhead) = 4.8 -> 4
        assert isinstance(duration, int)
        assert duration >= 4  # Should be at least base time

    def test_execute_update_plan_dry_run(self, manager, sample_change_report):
        """Test executing an update plan in dry-run mode."""
        update_plan = manager.analyze_changes(sample_change_report)

        result = manager.execute_update_plan(update_plan, dry_run=True)

        assert isinstance(result, dict)
        assert result["plan_id"] == update_plan["plan_id"]
        assert result["status"] == "completed"
        assert result["operations_completed"] == 3  # All operations should succeed in dry-run
        assert result["operations_failed"] == 0
        assert result["operations_skipped"] == 0
        assert result["total_operations"] == 3
        assert len(result["errors"]) == 0

    def test_execute_update_plan_actual(self, manager, sample_change_report):
        """Test executing an update plan with actual operations."""
        update_plan = manager.analyze_changes(sample_change_report)

        result = manager.execute_update_plan(update_plan, dry_run=False)

        assert result["plan_id"] == update_plan["plan_id"]
        assert result["status"] == "completed"
        assert result["operations_completed"] == 3
        assert result["operations_failed"] == 0
        assert result["total_operations"] == 3

    def test_execute_operation_batch_error_handling(self, manager):
        """Test error handling during operation batch execution."""
        # Create a strategy with a failing handler
        def failing_handler(data):
            raise Exception("Test error")

        strategy = UpdateStrategy(
            entity_type="test",
            create_handler=failing_handler,
            update_handler=None,
            delete_handler=None,
            depends_on=[],
            batch_size=10,
            priority=5
        )

        # Register the strategy first
        manager.register_update_strategy(strategy)

        operations = [
            UpdateOperation(
                operation_id="op1",
                entity_type="test",
                change_type="created",
                entity_id="test1",
                priority=5,
                jira_data={"name": "test"},
                openproject_data=None,
                depends_on=[],
                metadata={}
            )
        ]

        result = manager._execute_operations_for_type("test", operations, dry_run=False)

        # The operation should fail but be handled gracefully
        assert result["failed"] == 1
        assert result["completed"] == 0
        assert len(result["errors"]) > 0

    def test_save_and_load_update_plan(self, manager, sample_change_report):
        """Test saving and loading update plans."""
        update_plan = manager.analyze_changes(sample_change_report)
        plan_id = update_plan["plan_id"]

        # Save the plan
        manager._save_update_plan(update_plan)

        # Load the plan
        loaded_plan = manager.load_update_plan(plan_id)

        assert loaded_plan is not None
        assert loaded_plan["plan_id"] == plan_id
        assert loaded_plan["total_operations"] == update_plan["total_operations"]
        assert loaded_plan["entity_types"] == update_plan["entity_types"]

    def test_save_and_load_update_result(self, manager, temp_dir):
        """Test saving and loading update results."""
        result = UpdateResult(
            plan_id="test_plan",
            started_at=datetime.now(tz=UTC).isoformat(),
            completed_at=datetime.now(tz=UTC).isoformat(),
            status="completed",
            operations_completed=5,
            operations_failed=0,
            operations_skipped=0,
            total_operations=5,
            errors=[],
            warnings=[],
            performance_metrics={"test": "value"}
        )

        # Save the result
        manager._save_update_result(result)

        # Load the result
        loaded_result = manager.load_update_result("test_plan")

        assert loaded_result is not None
        assert loaded_result["plan_id"] == "test_plan"
        assert loaded_result["operations_completed"] == 5
        assert loaded_result["status"] == "completed"

    def test_load_nonexistent_plan(self, manager):
        """Test loading a non-existent update plan."""
        result = manager.load_update_plan("nonexistent_plan")
        assert result is None

    def test_load_nonexistent_result(self, manager):
        """Test loading a non-existent update result."""
        result = manager.load_update_result("nonexistent_plan")
        assert result is None

    def test_performance_metrics(self, manager):
        """Test performance metrics tracking."""
        initial_metrics = manager.get_performance_metrics()
        assert isinstance(initial_metrics, dict)
        assert "api_calls_made" in initial_metrics
        assert "entities_processed" in initial_metrics

        # Reset metrics
        manager.reset_performance_metrics()
        reset_metrics = manager.get_performance_metrics()
        assert reset_metrics["api_calls_made"] == 0
        assert reset_metrics["entities_processed"] == 0

    def test_entity_mapping_registration(self, manager, mock_state_manager):
        """Test entity mapping registration after successful operations."""
        operation = UpdateOperation(
            operation_id="op1",
            entity_type="users",
            change_type="created",
            entity_id="user1",
            priority=8,
            jira_data={"displayName": "John Doe"},
            openproject_data=None,
            depends_on=[],
            metadata={}
        )

        op_result = {"id": 123, "created": True}

        manager._register_entity_mapping_from_operation(operation, op_result)

        # Verify state manager was called to register mapping
        mock_state_manager.register_entity_mapping.assert_called_once()
        call_args = mock_state_manager.register_entity_mapping.call_args[1]

        assert call_args["jira_entity_type"] == "users"
        assert call_args["jira_entity_id"] == "user1"
        assert call_args["openproject_entity_type"] == "users"
        assert call_args["openproject_entity_id"] == "123"
        assert call_args["migration_component"] == "SelectiveUpdateManager"

    def test_placeholder_handlers(self, manager):
        """Test that placeholder handlers work correctly."""
        user_data = {"displayName": "Test User", "email": "test@example.com"}

        # Test create handler
        result = manager._create_user(user_data)
        assert result is not None
        assert result["created"] is True

        # Test update handler
        old_data = {"displayName": "Old Name"}
        result = manager._update_user(user_data, old_data)
        assert result is not None
        assert result["updated"] is True

        # Test delete handler
        result = manager._delete_user(user_data)
        assert result is True

        # Test other entity types
        project_data = {"name": "Test Project"}
        assert manager._create_project(project_data) is not None
        assert manager._update_project(project_data, {}) is not None
        assert manager._delete_project(project_data) is True

        field_data = {"name": "Test Field"}
        assert manager._create_custom_field(field_data) is not None
        assert manager._update_custom_field(field_data, {}) is not None
        assert manager._delete_custom_field(field_data) is True

    def test_empty_change_report(self, manager):
        """Test handling empty change reports."""
        empty_report = ChangeReport(
            entity_type="users",
            created_at=datetime.now(tz=UTC).isoformat(),
            jira_count=0,
            openproject_count=0,
            changes=[],
            metadata={}
        )

        update_plan = manager.analyze_changes(empty_report)

        assert update_plan["total_operations"] == 0
        assert len(update_plan["operations"]) == 0
        assert len(update_plan["entity_types"]) == 0

        # Executing empty plan should succeed
        result = manager.execute_update_plan(update_plan, dry_run=False)
        assert result["status"] == "completed"
        assert result["operations_completed"] == 0

    def test_update_settings_propagation(self, manager, sample_change_report):
        """Test that update settings are properly propagated."""
        custom_settings = {
            "batch_mode": True,
            "retry_count": 3,
            "custom_field": "value"
        }

        update_plan = manager.analyze_changes(sample_change_report, custom_settings)

        assert update_plan["update_settings"] == custom_settings
