#!/usr/bin/env python3
"""Comprehensive tests for EntityTypeRegistry system.

Tests the registry-based entity type resolution system that replaces
brittle string matching with robust, fail-fast behavior.
"""

import logging
from threading import Thread
from unittest.mock import Mock, patch

import pytest

from src.migrations.base_migration import (
    BaseMigration,
    EntityTypeRegistry,
    register_entity_types,
)


# Test Fixtures
@pytest.fixture(autouse=True)
def clean_registry():
    """Automatically clean EntityTypeRegistry before and after each test.

    This prevents state leakage between tests since the registry uses
    class-level dictionaries that persist across test runs.
    """
    EntityTypeRegistry.clear_registry()
    yield
    EntityTypeRegistry.clear_registry()


@pytest.fixture
def mock_logger():
    """Mock logger for testing warning behavior."""
    with patch("src.migrations.base_migration.logging.getLogger") as mock_get_logger:
        mock_logger_instance = Mock()
        mock_get_logger.return_value = mock_logger_instance
        yield mock_logger_instance


# Mock Migration Classes for Testing
class MockUserMigration(BaseMigration):
    """Mock migration class for user-related entities."""

    def __init__(self) -> None:
        # Mock initialization to avoid real dependencies
        import logging

        self.logger = logging.getLogger(self.__class__.__name__)


class MockProjectMigration(BaseMigration):
    """Mock migration class for project-related entities."""

    def __init__(self) -> None:
        # Mock initialization to avoid real dependencies
        self.logger = Mock()


class MockWorkPackageMigration(BaseMigration):
    """Mock migration class for work package entities."""

    def __init__(self) -> None:
        # Mock initialization to avoid real dependencies
        self.logger = Mock()


class NotAMigration:
    """Class that doesn't inherit from BaseMigration for error testing."""


class TestEntityTypeRegistry:
    """Test suite for EntityTypeRegistry class methods."""

    def test_register_and_resolve_success(self) -> None:
        """Test basic registration and resolution workflow."""
        # Arrange
        entity_types = ["users", "user_accounts"]
        EntityTypeRegistry.register(MockUserMigration, entity_types)

        # Act
        resolved_type = EntityTypeRegistry.resolve(MockUserMigration)

        # Assert
        assert resolved_type == "users"  # First type is primary

    def test_register_multiple_classes(self) -> None:
        """Test registering multiple migration classes."""
        # Arrange & Act
        EntityTypeRegistry.register(MockUserMigration, ["users", "accounts"])
        EntityTypeRegistry.register(MockProjectMigration, ["projects"])
        EntityTypeRegistry.register(
            MockWorkPackageMigration,
            ["work_packages", "issues"],
        )

        # Assert
        assert EntityTypeRegistry.resolve(MockUserMigration) == "users"
        assert EntityTypeRegistry.resolve(MockProjectMigration) == "projects"
        assert EntityTypeRegistry.resolve(MockWorkPackageMigration) == "work_packages"

    def test_get_supported_types_returns_copy(self) -> None:
        """Test that get_supported_types returns immutable copy."""
        # Arrange
        entity_types = ["projects", "portfolios"]
        EntityTypeRegistry.register(MockProjectMigration, entity_types)

        # Act
        supported_types = EntityTypeRegistry.get_supported_types(MockProjectMigration)
        supported_types.append("malicious_addition")  # Try to mutate

        # Assert
        original_types = EntityTypeRegistry.get_supported_types(MockProjectMigration)
        assert original_types == ["projects", "portfolios"]
        assert "malicious_addition" not in original_types

    def test_get_supported_types_preserves_order(self) -> None:
        """Test that get_supported_types preserves registration order."""
        # Arrange
        entity_types = ["work_packages", "issues", "tickets", "tasks"]
        EntityTypeRegistry.register(MockWorkPackageMigration, entity_types)

        # Act
        supported_types = EntityTypeRegistry.get_supported_types(
            MockWorkPackageMigration,
        )

        # Assert
        assert supported_types == entity_types

    def test_get_class_for_type_reverse_lookup(self) -> None:
        """Test reverse lookup from entity type to migration class."""
        # Arrange
        EntityTypeRegistry.register(MockUserMigration, ["users", "accounts"])
        EntityTypeRegistry.register(MockProjectMigration, ["projects"])

        # Act & Assert
        assert EntityTypeRegistry.get_class_for_type("users") is MockUserMigration
        assert EntityTypeRegistry.get_class_for_type("accounts") is MockUserMigration
        assert EntityTypeRegistry.get_class_for_type("projects") is MockProjectMigration
        assert EntityTypeRegistry.get_class_for_type("nonexistent") is None

    def test_get_all_registered_types(self) -> None:
        """Test getting all registered types across all classes."""
        # Arrange
        EntityTypeRegistry.register(MockUserMigration, ["users", "accounts"])
        EntityTypeRegistry.register(
            MockProjectMigration,
            ["projects", "accounts"],
        )  # Duplicate
        EntityTypeRegistry.register(MockWorkPackageMigration, ["work_packages"])

        # Act
        all_types = EntityTypeRegistry.get_all_registered_types()

        # Assert
        assert all_types == {"users", "accounts", "projects", "work_packages"}

    def test_clear_registry(self) -> None:
        """Test registry clearing functionality."""
        # Arrange
        EntityTypeRegistry.register(MockUserMigration, ["users"])
        EntityTypeRegistry.register(MockProjectMigration, ["projects"])

        # Verify registration exists
        assert EntityTypeRegistry.get_all_registered_types() == {"users", "projects"}

        # Act
        EntityTypeRegistry.clear_registry()

        # Assert
        assert EntityTypeRegistry.get_all_registered_types() == set()

        with pytest.raises(ValueError, match="is not registered"):
            EntityTypeRegistry.resolve(MockUserMigration)


class TestEntityTypeRegistryErrorHandling:
    """Test suite for EntityTypeRegistry error conditions."""

    def test_register_with_empty_entity_types_raises_error(self) -> None:
        """Test registration with empty entity types list."""
        with pytest.raises(ValueError, match="must support at least one entity type"):
            EntityTypeRegistry.register(MockUserMigration, [])

    def test_register_with_none_entity_types_raises_error(self) -> None:
        """Test registration with None entity types."""
        with pytest.raises(ValueError, match="must support at least one entity type"):
            EntityTypeRegistry.register(MockUserMigration, None)

    def test_register_non_base_migration_class_raises_error(self) -> None:
        """Test registration of non-BaseMigration class."""
        with pytest.raises(ValueError, match="must inherit from BaseMigration"):
            EntityTypeRegistry.register(NotAMigration, ["some_type"])

    def test_register_with_none_class_raises_error(self) -> None:
        """Test registration with None class."""
        with pytest.raises(ValueError, match="Migration class cannot be None"):
            EntityTypeRegistry.register(None, ["some_type"])

    def test_resolve_unregistered_class_raises_error(self) -> None:
        """Test resolving unregistered migration class."""
        with pytest.raises(
            ValueError,
            match="is not registered with EntityTypeRegistry",
        ):
            EntityTypeRegistry.resolve(MockUserMigration)

    def test_resolve_none_class_raises_error(self) -> None:
        """Test resolving None class."""
        with pytest.raises(ValueError, match="Migration class cannot be None"):
            EntityTypeRegistry.resolve(None)

    def test_get_supported_types_unregistered_class_raises_error(self) -> None:
        """Test get_supported_types for unregistered class."""
        with pytest.raises(
            ValueError,
            match="is not registered with EntityTypeRegistry",
        ):
            EntityTypeRegistry.get_supported_types(MockUserMigration)

    def test_register_duplicate_entity_type_logs_warning(self, caplog) -> None:
        """Test warning when registering duplicate entity types."""
        # Arrange
        EntityTypeRegistry.register(MockUserMigration, ["shared_type"])

        # Act
        with caplog.at_level(logging.WARNING):
            EntityTypeRegistry.register(MockProjectMigration, ["shared_type"])

        # Assert
        assert "Entity type 'shared_type' is supported by multiple classes" in caplog.text
        assert "MockUserMigration and MockProjectMigration" in caplog.text

        # Last registration wins
        assert EntityTypeRegistry.get_class_for_type("shared_type") is MockProjectMigration


class TestRegisterEntityTypesDecorator:
    """Test suite for @register_entity_types decorator."""

    def test_decorator_registers_class_on_definition(self) -> None:
        """Test decorator automatically registers class during definition."""

        # Act: Define class with decorator
        @register_entity_types("decorated_users", "decorated_accounts")
        class DecoratedUserMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        # Assert: No explicit registration call needed
        assert EntityTypeRegistry.resolve(DecoratedUserMigration) == "decorated_users"
        assert EntityTypeRegistry.get_class_for_type("decorated_accounts") is DecoratedUserMigration

    def test_decorator_with_single_entity_type(self) -> None:
        """Test decorator with single entity type."""

        # Act
        @register_entity_types("single_type")
        class SingleTypeMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        # Assert
        assert EntityTypeRegistry.resolve(SingleTypeMigration) == "single_type"
        assert EntityTypeRegistry.get_supported_types(SingleTypeMigration) == [
            "single_type",
        ]

    def test_decorator_with_multiple_entity_types(self) -> None:
        """Test decorator with multiple entity types."""

        # Act
        @register_entity_types("type1", "type2", "type3")
        class MultiTypeMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        # Assert
        assert EntityTypeRegistry.resolve(MultiTypeMigration) == "type1"
        assert EntityTypeRegistry.get_supported_types(MultiTypeMigration) == [
            "type1",
            "type2",
            "type3",
        ]

    def test_decorator_preserves_class_identity(self) -> None:
        """Test decorator returns original class unchanged."""

        # Act
        @register_entity_types("preserved_type")
        class PreservedMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

            def custom_method(self) -> str:
                return "custom_result"

        # Assert
        assert hasattr(PreservedMigration, "custom_method")
        instance = PreservedMigration()
        assert instance.custom_method() == "custom_result"

    def test_decorator_with_no_entity_types_raises_error(self) -> None:
        """Test decorator without entity types raises error during registration."""
        with pytest.raises(ValueError, match="must support at least one entity type"):

            @register_entity_types()
            class EmptyTypeMigration(BaseMigration):
                pass


class TestBaseMigrationIntegration:
    """Test integration between BaseMigration and EntityTypeRegistry."""

    def test_auto_detect_entity_type_success(self) -> None:
        """Test successful entity type auto-detection."""

        # Arrange: Register class using decorator
        @register_entity_types("integration_users", "integration_accounts")
        class IntegrationUserMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        instance = IntegrationUserMigration()

        # Act
        entity_type = instance._auto_detect_entity_type()

        # Assert
        assert entity_type == "integration_users"

    def test_auto_detect_entity_type_unregistered_logs_warning(self, caplog) -> None:
        """Test auto-detection for unregistered class logs warning."""
        # Arrange: Unregistered migration class
        instance = MockUserMigration()

        # Act
        with caplog.at_level(logging.WARNING):
            entity_type = instance._auto_detect_entity_type()

        # Assert
        assert entity_type is None
        assert "MockUserMigration is not registered with EntityTypeRegistry" in caplog.text
        assert "Add @register_entity_types decorator" in caplog.text

    def test_auto_detect_preserves_exception_details(self, caplog) -> None:
        """Test auto-detection preserves original exception details in warning."""
        # Arrange
        instance = MockUserMigration()

        # Act
        with caplog.at_level(logging.WARNING):
            entity_type = instance._auto_detect_entity_type()

        # Assert
        assert entity_type is None
        log_record = caplog.records[0]
        assert "Error:" in log_record.message


class TestEntityTypeRegistryEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_register_same_class_multiple_times(self) -> None:
        """Test registering same class multiple times updates registration."""
        # Arrange & Act
        EntityTypeRegistry.register(MockUserMigration, ["users"])
        EntityTypeRegistry.register(
            MockUserMigration,
            ["users", "accounts", "profiles"],
        )

        # Assert
        assert EntityTypeRegistry.resolve(MockUserMigration) == "users"
        assert EntityTypeRegistry.get_supported_types(MockUserMigration) == [
            "users",
            "accounts",
            "profiles",
        ]

    def test_entity_type_with_special_characters(self) -> None:
        """Test entity types with special characters."""
        # Arrange & Act
        EntityTypeRegistry.register(
            MockUserMigration,
            ["user-accounts", "user_profiles", "user.data"],
        )

        # Assert
        assert EntityTypeRegistry.resolve(MockUserMigration) == "user-accounts"
        assert EntityTypeRegistry.get_class_for_type("user_profiles") is MockUserMigration
        assert EntityTypeRegistry.get_class_for_type("user.data") is MockUserMigration

    def test_entity_type_case_sensitivity(self) -> None:
        """Test entity types are case-sensitive."""
        # Arrange
        EntityTypeRegistry.register(MockUserMigration, ["Users"])
        EntityTypeRegistry.register(MockProjectMigration, ["users"])

        # Act & Assert
        assert EntityTypeRegistry.get_class_for_type("Users") is MockUserMigration
        assert EntityTypeRegistry.get_class_for_type("users") is MockProjectMigration
        assert EntityTypeRegistry.get_class_for_type("USERS") is None

    def test_empty_string_entity_type(self) -> None:
        """Test empty string entity type handling."""
        # This should be allowed as it's a valid string
        EntityTypeRegistry.register(MockUserMigration, [""])
        assert EntityTypeRegistry.resolve(MockUserMigration) == ""
        assert EntityTypeRegistry.get_class_for_type("") is MockUserMigration


class TestEntityTypeRegistryConcurrency:
    """Test concurrency and thread safety."""

    def test_concurrent_registration(self) -> None:
        """Test concurrent registration from multiple threads."""
        # Arrange
        num_threads = 10
        threads = []
        registered_classes = []

        def register_unique_class(thread_id: int) -> None:
            """Register a unique migration class in a thread."""

            @register_entity_types(f"concurrent_type_{thread_id}")
            class ConcurrentMigration(BaseMigration):
                def __init__(self) -> None:
                    self.logger = Mock()
                    self.thread_id = thread_id

            registered_classes.append(ConcurrentMigration)

        # Act: Start all threads
        for i in range(num_threads):
            thread = Thread(target=register_unique_class, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Assert: All registrations successful
        assert len(EntityTypeRegistry.get_all_registered_types()) == num_threads
        assert len(registered_classes) == num_threads

        # Verify each registration
        for i, cls in enumerate(registered_classes):
            entity_type = f"concurrent_type_{i}"
            assert EntityTypeRegistry.get_class_for_type(entity_type) is cls
            assert EntityTypeRegistry.resolve(cls) == entity_type

    def test_concurrent_access_different_operations(self) -> None:
        """Test concurrent access with different registry operations."""
        # Arrange: Pre-register some classes
        EntityTypeRegistry.register(MockUserMigration, ["users"])
        EntityTypeRegistry.register(MockProjectMigration, ["projects"])

        results = []
        exceptions = []

        def read_operations() -> None:
            """Perform read operations concurrently."""
            try:
                # Multiple read operations
                results.append(EntityTypeRegistry.resolve(MockUserMigration))
                results.append(EntityTypeRegistry.get_class_for_type("projects"))
                results.append(len(EntityTypeRegistry.get_all_registered_types()))
                results.append(
                    EntityTypeRegistry.get_supported_types(MockProjectMigration),
                )
            except Exception as e:
                exceptions.append(e)

        def write_operations() -> None:
            """Perform write operations concurrently."""
            try:

                @register_entity_types("concurrent_write")
                class ConcurrentWriteMigration(BaseMigration):
                    def __init__(self) -> None:
                        self.logger = Mock()

                results.append("write_success")
            except Exception as e:
                exceptions.append(e)

        # Act: Start multiple threads with mixed operations
        threads = []
        for _ in range(5):
            read_thread = Thread(target=read_operations)
            write_thread = Thread(target=write_operations)
            threads.extend([read_thread, write_thread])

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        # Assert: No exceptions occurred
        assert len(exceptions) == 0, f"Concurrent operations failed: {exceptions}"
        assert len(results) > 0  # Some operations completed successfully


class TestEntityTypeRegistryRealWorldScenarios:
    """Test realistic usage scenarios."""

    def test_typical_migration_setup(self) -> None:
        """Test typical migration class setup scenario."""

        # Arrange & Act: Define migration classes like in real codebase
        @register_entity_types("users", "user_accounts")
        class UserMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        @register_entity_types("projects")
        class ProjectMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        @register_entity_types("work_packages", "issues")
        class WorkPackageMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        @register_entity_types("issue_types", "work_package_types")
        class IssueTypeMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        # Assert: All migrations registered correctly
        all_types = EntityTypeRegistry.get_all_registered_types()
        expected_types = {
            "users",
            "user_accounts",
            "projects",
            "work_packages",
            "issues",
            "issue_types",
            "work_package_types",
        }
        assert all_types == expected_types

        # Test primary type resolution
        assert EntityTypeRegistry.resolve(UserMigration) == "users"
        assert EntityTypeRegistry.resolve(ProjectMigration) == "projects"
        assert EntityTypeRegistry.resolve(WorkPackageMigration) == "work_packages"
        assert EntityTypeRegistry.resolve(IssueTypeMigration) == "issue_types"

        # Test reverse lookup
        assert EntityTypeRegistry.get_class_for_type("user_accounts") is UserMigration
        assert EntityTypeRegistry.get_class_for_type("issues") is WorkPackageMigration
        assert EntityTypeRegistry.get_class_for_type("work_package_types") is IssueTypeMigration

    def test_migration_orchestrator_usage_pattern(self) -> None:
        """Test how migration orchestrator would use the registry."""

        # Arrange: Register migrations
        @register_entity_types("users")
        class UserMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        @register_entity_types("projects")
        class ProjectMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        # Simulate orchestrator determining migration for entity types
        entity_types_to_migrate = ["users", "projects", "unknown_type"]

        # Act & Assert: Orchestrator can find appropriate migrations
        for entity_type in entity_types_to_migrate:
            migration_class = EntityTypeRegistry.get_class_for_type(entity_type)
            if migration_class:
                instance = migration_class()
                detected_type = instance._auto_detect_entity_type()
                assert detected_type is not None
            else:
                # Handle unknown types gracefully
                assert entity_type == "unknown_type"

    def test_migration_class_inheritance_hierarchy(self) -> None:
        """Test registry works with migration class inheritance."""

        # Arrange: Define inheritance hierarchy
        @register_entity_types("base_entities")
        class BaseMigrationImpl(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        @register_entity_types("specialized_entities")
        class SpecializedMigration(BaseMigrationImpl):
            def __init__(self) -> None:
                super().__init__()

        # Act & Assert: Both classes work independently
        assert EntityTypeRegistry.resolve(BaseMigrationImpl) == "base_entities"
        assert EntityTypeRegistry.resolve(SpecializedMigration) == "specialized_entities"

        # Instances work correctly
        base_instance = BaseMigrationImpl()
        specialized_instance = SpecializedMigration()

        assert base_instance._auto_detect_entity_type() == "base_entities"
        assert specialized_instance._auto_detect_entity_type() == "specialized_entities"
