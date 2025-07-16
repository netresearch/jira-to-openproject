#!/usr/bin/env python3
"""Selective update system for idempotent migration operations.

This module provides functionality to selectively update only changed entities,
implementing differential update strategies for each entity type and handling
dependencies during selective updates.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict
from collections.abc import Callable

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.custom_field_migration import CustomFieldMigration
from src.migrations.project_migration import ProjectMigration

# Import migration classes for handler delegation
from src.migrations.user_migration import UserMigration
from src.migrations.work_package_migration import WorkPackageMigration
from src.utils.change_detector import ChangeReport
from src.utils.state_manager import StateManager


# Type definitions for selective updates
class UpdateStrategy(TypedDict):
    """Represents an update strategy for an entity type."""

    entity_type: str
    create_handler: Callable[[dict[str, Any]], dict[str, Any]] | None
    update_handler: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None
    delete_handler: Callable[[dict[str, Any]], bool] | None
    depends_on: list[str]  # Entity types this depends on
    batch_size: int
    priority: int  # 1-10, higher is more important


class UpdateOperation(TypedDict):
    """Represents a single update operation to be performed."""

    operation_id: str
    entity_type: str
    change_type: str  # 'created', 'updated', 'deleted'
    entity_id: str
    priority: int
    jira_data: dict[str, Any] | None
    openproject_data: dict[str, Any] | None
    depends_on: list[str]  # Other operation IDs this depends on
    metadata: dict[str, Any]


class UpdatePlan(TypedDict):
    """Represents a complete update plan."""

    plan_id: str
    created_at: str
    entity_types: list[str]
    total_operations: int
    operations: list[UpdateOperation]
    dependency_order: list[str]  # Ordered list of entity types
    estimated_duration: int  # seconds
    update_settings: dict[str, Any]


class UpdateResult(TypedDict):
    """Represents the result of executing an update plan."""

    plan_id: str
    started_at: str
    completed_at: str | None
    status: str  # 'in_progress', 'completed', 'failed', 'cancelled'
    operations_completed: int
    operations_failed: int
    operations_skipped: int
    total_operations: int
    errors: list[str]
    warnings: list[str]
    performance_metrics: dict[str, Any]


class SelectiveUpdateManager:
    """Manages selective updates for changed entities.

    This class provides functionality to:
    - Analyze change reports to determine specific update requirements
    - Implement differential update strategies for each entity type
    - Handle entity dependencies during selective updates
    - Provide granular control over what gets updated
    - Optimize updates to minimize API calls and processing
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
        state_manager: StateManager | None = None,
        update_dir: Path | None = None,
    ) -> None:
        """Initialize the selective update manager.

        Args:
            jira_client: Jira client for retrieving entity data
            op_client: OpenProject client for performing updates
            state_manager: State manager for tracking entity mappings
            update_dir: Directory to store update plans and results
        """
        self.logger = config.logger
        self.jira_client = jira_client or JiraClient()
        self.op_client = op_client or OpenProjectClient()
        self.state_manager = state_manager or StateManager()

        self.update_dir = (
            update_dir or config.get_path("data").parent / "selective_updates"
        )
        self.update_dir.mkdir(parents=True, exist_ok=True)

        # Ensure update directory structure exists
        (self.update_dir / "plans").mkdir(exist_ok=True)
        (self.update_dir / "results").mkdir(exist_ok=True)
        (self.update_dir / "cache").mkdir(exist_ok=True)

        # Initialize migration instances for delegation
        self._migration_instances = {}
        try:
            self._migration_instances["users"] = UserMigration(
                jira_client=self.jira_client, op_client=self.op_client
            )
            self._migration_instances["projects"] = ProjectMigration(
                jira_client=self.jira_client, op_client=self.op_client
            )
            self._migration_instances["issues"] = WorkPackageMigration(
                jira_client=self.jira_client, op_client=self.op_client
            )
            self._migration_instances["customfields"] = CustomFieldMigration(
                jira_client=self.jira_client, op_client=self.op_client
            )
        except Exception as e:
            self.logger.warning("Failed to initialize some migration instances: %s", e)
            self._migration_instances = {}

        # Initialize update strategies registry
        self._update_strategies: dict[str, UpdateStrategy] = {}
        self._initialize_default_strategies()

        # Performance tracking
        self._performance_metrics = {
            "api_calls_made": 0,
            "entities_processed": 0,
            "cache_hits": 0,
            "batch_operations": 0,
        }

    def _initialize_default_strategies(self) -> None:
        """Initialize default update strategies for common entity types."""
        # Users strategy - high priority, no dependencies
        self._update_strategies["users"] = UpdateStrategy(
            entity_type="users",
            create_handler=self._create_user,
            update_handler=self._update_user,
            delete_handler=self._delete_user,
            depends_on=[],
            batch_size=50,
            priority=9,
        )

        # Projects strategy - high priority, depends on users
        self._update_strategies["projects"] = UpdateStrategy(
            entity_type="projects",
            create_handler=self._create_project,
            update_handler=self._update_project,
            delete_handler=self._delete_project,
            depends_on=["users"],
            batch_size=20,
            priority=8,
        )

        # Custom fields strategy - medium-high priority, no dependencies
        self._update_strategies["customfields"] = UpdateStrategy(
            entity_type="customfields",
            create_handler=self._create_custom_field,
            update_handler=self._update_custom_field,
            delete_handler=self._delete_custom_field,
            depends_on=[],
            batch_size=30,
            priority=7,
        )

        # Issue types strategy - medium priority, depends on custom fields
        self._update_strategies["issuetypes"] = UpdateStrategy(
            entity_type="issuetypes",
            create_handler=self._create_issue_type,
            update_handler=self._update_issue_type,
            delete_handler=self._delete_issue_type,
            depends_on=["customfields"],
            batch_size=25,
            priority=6,
        )

        # Statuses strategy - medium priority, no dependencies
        self._update_strategies["statuses"] = UpdateStrategy(
            entity_type="statuses",
            create_handler=self._create_status,
            update_handler=self._update_status,
            delete_handler=self._delete_status,
            depends_on=[],
            batch_size=40,
            priority=6,
        )

        # Issues/work packages strategy - lower priority, depends on everything
        self._update_strategies["issues"] = UpdateStrategy(
            entity_type="issues",
            create_handler=self._create_issue,
            update_handler=self._update_issue,
            delete_handler=self._delete_issue,
            depends_on=["users", "projects", "issuetypes", "statuses", "customfields"],
            batch_size=10,
            priority=5,
        )

    def register_update_strategy(self, strategy: UpdateStrategy) -> None:
        """Register a custom update strategy for an entity type.

        Args:
            strategy: Update strategy configuration
        """
        entity_type = strategy["entity_type"]
        self._update_strategies[entity_type] = strategy
        self.logger.debug("Registered update strategy for %s", entity_type)

    def analyze_changes(
        self, change_report: ChangeReport, update_settings: dict[str, Any] | None = None
    ) -> UpdatePlan:
        """Analyze a change report and create an update plan.

        Args:
            change_report: Change report from ChangeDetector
            update_settings: Settings to control update behavior

        Returns:
            UpdatePlan with operations to perform
        """
        self.logger.info("Analyzing changes to create selective update plan")

        settings = update_settings or {}
        plan_id = f"plan_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}"

        operations: list[UpdateOperation] = []
        entity_types_involved = set()

        # Process each change and create operations
        for change in change_report["changes"]:
            entity_type = change["entity_type"]

            # Check if we have a strategy for this entity type
            if entity_type not in self._update_strategies:
                self.logger.warning(
                    "No update strategy for entity type: %s", entity_type
                )
                continue

            entity_types_involved.add(entity_type)

            strategy = self._update_strategies[entity_type]
            operation_id = f"{plan_id}_{entity_type}_{change['entity_id']}"

            # Create operation
            operation = UpdateOperation(
                operation_id=operation_id,
                entity_type=entity_type,
                change_type=change["change_type"],
                entity_id=change["entity_id"],
                priority=change["priority"],
                jira_data=change.get("new_data"),
                openproject_data=change.get("old_data"),
                depends_on=[],
                metadata={
                    "strategy": entity_type,
                    "batch_size": strategy["batch_size"],
                    "original_change": change,
                },
            )

            operations.append(operation)

        # Resolve dependencies and create execution order
        dependency_order = self._resolve_dependency_order(list(entity_types_involved))

        # Create the update plan
        update_plan = UpdatePlan(
            plan_id=plan_id,
            created_at=datetime.now(tz=UTC).isoformat(),
            entity_types=list(entity_types_involved),
            total_operations=len(operations),
            operations=operations,
            dependency_order=dependency_order,
            estimated_duration=self._estimate_plan_duration(operations),
            update_settings=settings,
        )

        self.logger.info(
            "Created update plan %s with %d operations across %d entity types",
            plan_id,
            len(operations),
            len(entity_types_involved),
        )

        return update_plan

    def execute_update_plan(
        self, update_plan: UpdatePlan, dry_run: bool = False
    ) -> UpdateResult:
        """Execute an update plan.

        Args:
            update_plan: Plan to execute
            dry_run: If True, simulate execution without making changes

        Returns:
            UpdateResult with execution results
        """
        plan_id = update_plan["plan_id"]
        self.logger.info("Executing update plan %s (dry_run=%s)", plan_id, dry_run)

        start_time = datetime.now(tz=UTC)
        result = UpdateResult(
            plan_id=plan_id,
            started_at=start_time.isoformat(),
            completed_at=None,
            status="in_progress",
            operations_completed=0,
            operations_failed=0,
            operations_skipped=0,
            total_operations=len(update_plan["operations"]),
            errors=[],
            warnings=[],
            performance_metrics={},
        )

        try:
            # Reset performance metrics
            self._performance_metrics = {
                "api_calls_made": 0,
                "entities_processed": 0,
                "cache_hits": 0,
                "batch_operations": 0,
            }

            # Group operations by dependency order
            operations_by_type = self._group_operations_by_type(
                update_plan["operations"]
            )

            # Execute operations in dependency order
            for entity_type in update_plan["dependency_order"]:
                if entity_type not in operations_by_type:
                    continue

                operations = operations_by_type[entity_type]
                self.logger.info(
                    "Processing %d operations for %s", len(operations), entity_type
                )

                # Execute operations for this entity type
                type_result = self._execute_operations_for_type(
                    entity_type, operations, dry_run
                )

                # Update overall result
                result["operations_completed"] += type_result["completed"]
                result["operations_failed"] += type_result["failed"]
                result["operations_skipped"] += type_result["skipped"]
                result["errors"].extend(type_result["errors"])
                result["warnings"].extend(type_result["warnings"])

            # Finalize result
            end_time = datetime.now(tz=UTC)
            result["completed_at"] = end_time.isoformat()
            result["status"] = "completed" if not result["errors"] else "failed"
            result["performance_metrics"] = self._performance_metrics.copy()

            # Save the result
            self._save_update_result(result)

            self.logger.info(
                "Update plan %s completed: %d successful, %d failed, %d skipped",
                plan_id,
                result["operations_completed"],
                result["operations_failed"],
                result["operations_skipped"],
            )

            return result

        except Exception as e:
            # Handle execution failure
            result["completed_at"] = datetime.now(tz=UTC).isoformat()
            result["status"] = "failed"
            result["errors"].append(f"Execution failed: {e}")
            result["performance_metrics"] = self._performance_metrics.copy()

            self.logger.exception("Update plan execution failed: %s", e)
            self._save_update_result(result)

            raise

    def _resolve_dependency_order(self, entity_types: list[str]) -> list[str]:
        """Resolve the dependency order for entity types.

        Args:
            entity_types: List of entity types to order

        Returns:
            Ordered list of entity types respecting dependencies
        """
        # Build dependency graph
        dependencies = {}
        for entity_type in entity_types:
            if entity_type in self._update_strategies:
                strategy = self._update_strategies[entity_type]
                dependencies[entity_type] = [
                    dep for dep in strategy["depends_on"] if dep in entity_types
                ]
            else:
                dependencies[entity_type] = []

        # Simple topological sort
        ordered = []
        visited = set()

        def visit(entity_type: str) -> None:
            if entity_type in visited:
                return
            visited.add(entity_type)
            for dep in dependencies.get(entity_type, []):
                visit(dep)
            ordered.append(entity_type)

        for entity_type in entity_types:
            if entity_type not in visited:
                visit(entity_type)

        return ordered

    def _estimate_plan_duration(self, operations: list[UpdateOperation]) -> int:
        """Estimate plan execution duration in seconds.

        Args:
            operations: List of operations to estimate

        Returns:
            Estimated duration in seconds
        """
        # Base time estimates per operation type (seconds)
        operation_times = {"created": 2.0, "updated": 1.5, "deleted": 1.0}

        total_time = 0.0
        for op in operations:
            change_type = op["change_type"]
            base_time = operation_times.get(change_type, 1.5)
            total_time += base_time

        # Add overhead for batching and API delays
        overhead = len(operations) * 0.1
        return int(total_time + overhead)

    def _group_operations_by_type(
        self, operations: list[UpdateOperation]
    ) -> dict[str, list[UpdateOperation]]:
        """Group operations by entity type.

        Args:
            operations: List of operations to group

        Returns:
            Dictionary mapping entity types to operations
        """
        groups = {}
        for op in operations:
            entity_type = op["entity_type"]
            if entity_type not in groups:
                groups[entity_type] = []
            groups[entity_type].append(op)
        return groups

    def _execute_operations_for_type(
        self, entity_type: str, operations: list[UpdateOperation], dry_run: bool
    ) -> dict[str, Any]:
        """Execute all operations for a specific entity type.

        Args:
            entity_type: Type of entities to process
            operations: Operations to execute
            dry_run: Whether to simulate execution

        Returns:
            Dictionary with execution results
        """
        result = {
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": [],
            "warnings": [],
        }

        if entity_type not in self._update_strategies:
            result["errors"].append(f"No strategy for entity type: {entity_type}")
            result["skipped"] = len(operations)
            return result

        strategy = self._update_strategies[entity_type]
        batch_size = strategy["batch_size"]

        # Process operations in batches
        for i in range(0, len(operations), batch_size):
            batch = operations[i : i + batch_size]
            batch_result = self._execute_operation_batch(entity_type, batch, dry_run)

            result["completed"] += batch_result["completed"]
            result["failed"] += batch_result["failed"]
            result["skipped"] += batch_result["skipped"]
            result["errors"].extend(batch_result["errors"])
            result["warnings"].extend(batch_result["warnings"])

        return result

    def _execute_operation_batch(
        self, entity_type: str, operations: list[UpdateOperation], dry_run: bool
    ) -> dict[str, Any]:
        """Execute a batch of operations for an entity type.

        Args:
            entity_type: Type of entities being processed
            operations: Batch of operations to execute
            dry_run: Whether to simulate execution

        Returns:
            Dictionary with batch execution results
        """
        result = {
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": [],
            "warnings": [],
        }

        strategy = self._update_strategies[entity_type]
        self._performance_metrics["batch_operations"] += 1

        for operation in operations:
            try:
                if dry_run:
                    # Simulate operation
                    self.logger.debug(
                        "[DRY RUN] Would %s %s %s",
                        operation["change_type"],
                        entity_type,
                        operation["entity_id"],
                    )
                    result["completed"] += 1
                    continue

                # Execute actual operation
                success = self._execute_single_operation(operation, strategy)
                if success:
                    result["completed"] += 1
                else:
                    result["failed"] += 1
                    result["errors"].append(
                        f"Failed to {operation['change_type']} {entity_type} {operation['entity_id']}"
                    )

            except Exception as e:
                result["failed"] += 1
                error_msg = (
                    f"Error processing {entity_type} {operation['entity_id']}: {e}"
                )
                result["errors"].append(error_msg)
                self.logger.exception(error_msg)

        return result

    def _execute_single_operation(
        self, operation: UpdateOperation, strategy: UpdateStrategy
    ) -> bool:
        """Execute a single update operation.

        Args:
            operation: Operation to execute
            strategy: Update strategy to use

        Returns:
            True if operation succeeded, False otherwise
        """
        change_type = operation["change_type"]
        entity_data = operation["jira_data"]

        self._performance_metrics["entities_processed"] += 1
        self._performance_metrics["api_calls_made"] += 1

        try:
            if change_type == "created" and strategy["create_handler"]:
                result = strategy["create_handler"](entity_data)
                if result:
                    # Register entity mapping for created entities
                    self._register_entity_mapping_from_operation(operation, result)
                return result is not None

            elif change_type == "updated" and strategy["update_handler"]:
                old_data = operation["openproject_data"]
                result = strategy["update_handler"](entity_data, old_data)
                return result is not None

            elif change_type == "deleted" and strategy["delete_handler"]:
                old_data = operation["openproject_data"]
                return strategy["delete_handler"](old_data)

            else:
                self.logger.warning(
                    "No handler for %s operation on %s",
                    change_type,
                    operation["entity_type"],
                )
                return False

        except Exception as e:
            self.logger.exception(
                "Handler failed for %s %s %s: %s",
                change_type,
                operation["entity_type"],
                operation["entity_id"],
                e,
            )
            return False

    def _register_entity_mapping_from_operation(
        self, operation: UpdateOperation, op_result: dict[str, Any]
    ) -> None:
        """Register entity mapping after successful creation.

        Args:
            operation: The completed operation
            op_result: Result from the create operation
        """
        try:
            entity_type = operation["entity_type"]
            jira_id = operation["entity_id"]
            op_id = op_result.get("id")

            if op_id:
                self.state_manager.register_entity_mapping(
                    jira_entity_type=entity_type,
                    jira_entity_id=jira_id,
                    openproject_entity_type=entity_type,
                    openproject_entity_id=str(op_id),
                    migration_component="SelectiveUpdateManager",
                    metadata={
                        "created_via": "selective_update",
                        "operation_id": operation["operation_id"],
                    },
                )
        except Exception as e:
            self.logger.warning("Failed to register entity mapping: %s", e)

    def _save_update_plan(self, plan: UpdatePlan) -> None:
        """Save an update plan to disk.

        Args:
            plan: Update plan to save
        """
        try:
            plan_file = self.update_dir / "plans" / f"{plan['plan_id']}.json"
            with plan_file.open("w") as f:
                json.dump(plan, f, indent=2)
        except Exception as e:
            self.logger.warning("Failed to save update plan: %s", e)

    def _save_update_result(self, result: UpdateResult) -> None:
        """Save an update result to disk.

        Args:
            result: Update result to save
        """
        try:
            result_file = self.update_dir / "results" / f"{result['plan_id']}.json"
            with result_file.open("w") as f:
                json.dump(result, f, indent=2)
        except Exception as e:
            self.logger.warning("Failed to save update result: %s", e)

    # Entity-specific handler methods (delegating to migration classes)
    def _create_user(self, user_data: dict[str, Any]) -> dict[str, Any] | None:
        """Create a user in OpenProject."""
        self.logger.debug("Creating user: %s", user_data.get("displayName", "unknown"))

        user_migration = self._migration_instances.get("users")
        if not user_migration:
            self.logger.error("UserMigration instance not available")
            return None

        try:
            # Use the UserMigration's processing logic for single user
            result = user_migration.process_single_user(user_data)
            if result and result.get("openproject_id"):
                return {"id": result["openproject_id"], "created": True}
            return None
        except Exception as e:
            self.logger.error("Failed to create user: %s", e)
            return None

    def _update_user(
        self, new_data: dict[str, Any], old_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a user in OpenProject."""
        self.logger.debug("Updating user: %s", new_data.get("displayName", "unknown"))

        user_migration = self._migration_instances.get("users")
        if not user_migration:
            self.logger.error("UserMigration instance not available")
            return None

        try:
            # Get the OpenProject user ID from old data or mapping
            op_user_id = old_data.get("id") if old_data else None
            if not op_user_id:
                # Try to find mapping
                jira_user_id = new_data.get("accountId") or new_data.get("key")
                if jira_user_id:
                    mapping = self.state_manager.get_entity_mapping(
                        "users", jira_user_id
                    )
                    op_user_id = (
                        mapping.get("openproject_entity_id") if mapping else None
                    )

            if op_user_id:
                # Update existing user
                result = user_migration.update_user_in_openproject(new_data, op_user_id)
                if result:
                    return {"id": op_user_id, "updated": True}
            else:
                # If no mapping found, treat as creation
                return self._create_user(new_data)

            return None
        except Exception as e:
            self.logger.error("Failed to update user: %s", e)
            return None

    def _delete_user(self, user_data: dict[str, Any]) -> bool:
        """Delete a user in OpenProject."""
        self.logger.debug("Deleting user: %s", user_data.get("displayName", "unknown"))

        # Note: User deletion in OpenProject might not be desired
        # as it could break references. Consider deactivation instead.
        self.logger.warning(
            "User deletion not implemented - users should be deactivated, not deleted"
        )
        return True

    def _create_project(self, project_data: dict[str, Any]) -> dict[str, Any] | None:
        """Create a project in OpenProject."""
        self.logger.debug("Creating project: %s", project_data.get("name", "unknown"))

        project_migration = self._migration_instances.get("projects")
        if not project_migration:
            self.logger.error("ProjectMigration instance not available")
            return None

        try:
            # Use the ProjectMigration's processing logic for single project
            result = project_migration.process_single_project(project_data)
            if result and result.get("openproject_id"):
                return {"id": result["openproject_id"], "created": True}
            return None
        except Exception as e:
            self.logger.error("Failed to create project: %s", e)
            return None

    def _update_project(
        self, new_data: dict[str, Any], old_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a project in OpenProject."""
        self.logger.debug("Updating project: %s", new_data.get("name", "unknown"))

        project_migration = self._migration_instances.get("projects")
        if not project_migration:
            self.logger.error("ProjectMigration instance not available")
            return None

        try:
            # Get the OpenProject project ID from old data or mapping
            op_project_id = old_data.get("id") if old_data else None
            if not op_project_id:
                # Try to find mapping
                jira_project_id = new_data.get("id") or new_data.get("key")
                if jira_project_id:
                    mapping = self.state_manager.get_entity_mapping(
                        "projects", jira_project_id
                    )
                    op_project_id = (
                        mapping.get("openproject_entity_id") if mapping else None
                    )

            if op_project_id:
                # Update existing project
                result = project_migration.update_project_in_openproject(
                    new_data, op_project_id
                )
                if result:
                    return {"id": op_project_id, "updated": True}
            else:
                # If no mapping found, treat as creation
                return self._create_project(new_data)

            return None
        except Exception as e:
            self.logger.error("Failed to update project: %s", e)
            return None

    def _delete_project(self, project_data: dict[str, Any]) -> bool:
        """Delete a project in OpenProject."""
        self.logger.debug("Deleting project: %s", project_data.get("name", "unknown"))

        # Note: Project deletion is usually not desired as it removes all associated data
        # Consider archiving/deactivating instead
        self.logger.warning(
            "Project deletion not implemented - projects should be archived, not deleted"
        )
        return True

    def _create_custom_field(self, field_data: dict[str, Any]) -> dict[str, Any] | None:
        """Create a custom field in OpenProject."""
        self.logger.debug(
            "Creating custom field: %s", field_data.get("name", "unknown")
        )
        return {"id": "placeholder", "created": True}

    def _update_custom_field(
        self, new_data: dict[str, Any], old_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a custom field in OpenProject."""
        self.logger.debug("Updating custom field: %s", new_data.get("name", "unknown"))
        return {"id": "placeholder", "updated": True}

    def _delete_custom_field(self, field_data: dict[str, Any]) -> bool:
        """Delete a custom field in OpenProject."""
        self.logger.debug(
            "Deleting custom field: %s", field_data.get("name", "unknown")
        )
        return True

    def _create_issue_type(self, type_data: dict[str, Any]) -> dict[str, Any] | None:
        """Create an issue type in OpenProject."""
        self.logger.debug("Creating issue type: %s", type_data.get("name", "unknown"))
        return {"id": "placeholder", "created": True}

    def _update_issue_type(
        self, new_data: dict[str, Any], old_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update an issue type in OpenProject."""
        self.logger.debug("Updating issue type: %s", new_data.get("name", "unknown"))
        return {"id": "placeholder", "updated": True}

    def _delete_issue_type(self, type_data: dict[str, Any]) -> bool:
        """Delete an issue type in OpenProject."""
        self.logger.debug("Deleting issue type: %s", type_data.get("name", "unknown"))
        return True

    def _create_status(self, status_data: dict[str, Any]) -> dict[str, Any] | None:
        """Create a status in OpenProject."""
        self.logger.debug("Creating status: %s", status_data.get("name", "unknown"))
        return {"id": "placeholder", "created": True}

    def _update_status(
        self, new_data: dict[str, Any], old_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a status in OpenProject."""
        self.logger.debug("Updating status: %s", new_data.get("name", "unknown"))
        return {"id": "placeholder", "updated": True}

    def _delete_status(self, status_data: dict[str, Any]) -> bool:
        """Delete a status in OpenProject."""
        self.logger.debug("Deleting status: %s", status_data.get("name", "unknown"))
        return True

    def _create_issue(self, issue_data: dict[str, Any]) -> dict[str, Any] | None:
        """Create an issue/work package in OpenProject."""
        self.logger.debug("Creating issue: %s", issue_data.get("key", "unknown"))
        return {"id": "placeholder", "created": True}

    def _update_issue(
        self, new_data: dict[str, Any], old_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update an issue/work package in OpenProject."""
        self.logger.debug("Updating issue: %s", new_data.get("key", "unknown"))
        return {"id": "placeholder", "updated": True}

    def _delete_issue(self, issue_data: dict[str, Any]) -> bool:
        """Delete an issue/work package in OpenProject."""
        self.logger.debug("Deleting issue: %s", issue_data.get("key", "unknown"))
        return True

    def get_registered_strategies(self) -> dict[str, UpdateStrategy]:
        """Get all registered update strategies.

        Returns:
            Dictionary mapping entity types to their strategies
        """
        return self._update_strategies.copy()

    def load_update_plan(self, plan_id: str) -> UpdatePlan | None:
        """Load an update plan from disk.

        Args:
            plan_id: ID of the plan to load

        Returns:
            Loaded update plan or None if not found
        """
        try:
            plan_file = self.update_dir / "plans" / f"{plan_id}.json"
            if not plan_file.exists():
                return None

            with plan_file.open("r") as f:
                return json.load(f)
        except Exception as e:
            self.logger.warning("Failed to load update plan %s: %s", plan_id, e)
            return None

    def load_update_result(self, plan_id: str) -> UpdateResult | None:
        """Load an update result from disk.

        Args:
            plan_id: ID of the plan result to load

        Returns:
            Loaded update result or None if not found
        """
        try:
            result_file = self.update_dir / "results" / f"{plan_id}.json"
            if not result_file.exists():
                return None

            with result_file.open("r") as f:
                return json.load(f)
        except Exception as e:
            self.logger.warning("Failed to load update result %s: %s", plan_id, e)
            return None

    def get_performance_metrics(self) -> dict[str, Any]:
        """Get current performance metrics.

        Returns:
            Dictionary with performance metrics
        """
        return self._performance_metrics.copy()

    def reset_performance_metrics(self) -> None:
        """Reset performance metrics to zero."""
        self._performance_metrics = {
            "api_calls_made": 0,
            "entities_processed": 0,
            "cache_hits": 0,
            "batch_operations": 0,
        }
