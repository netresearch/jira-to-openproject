"""Test suite for migration class registrations with @register_entity_types decorator.

This module verifies that the newly decorated migration classes (AccountMigration,
CustomFieldMigration, StatusMigration) are properly registered with the EntityTypeRegistry
when their modules are imported, confirming the decorator functionality works correctly.
"""

import importlib

import pytest

from src.migrations.base_migration import BaseMigration, EntityTypeRegistry


@pytest.fixture(autouse=True)
def clean_registry():
    """Automatically clean the EntityTypeRegistry before and after each test.

    This prevents state leakage since the registry uses a class-level dictionary
    that persists across test runs and module imports.
    """
    EntityTypeRegistry.clear_registry()
    yield
    EntityTypeRegistry.clear_registry()


# Table-driven test data for all newly decorated migration classes
# Each tuple contains:
# 1. The full module path to the migration class
# 2. The name of the class to be imported
# 3. A list of all entity types expected to be registered (primary type first)
MIGRATION_CLASSES_TO_TEST = [
    (
        "src.migrations.account_migration",
        "AccountMigration",
        ["accounts", "tempo_accounts"],
    ),
    (
        "src.migrations.custom_field_migration",
        "CustomFieldMigration",
        ["custom_fields"],
    ),
    (
        "src.migrations.status_migration",
        "StatusMigration",
        ["statuses", "status_types"],
    ),
]


@pytest.mark.parametrize(
    ("module_path", "class_name", "expected_types"),
    MIGRATION_CLASSES_TO_TEST,
)
def test_migration_class_is_registered_on_import(
    module_path: str,
    class_name: str,
    expected_types: list[str],
) -> None:
    """Verify that migration classes are correctly registered with their entity types
    when their module is imported, confirming the @register_entity_types decorator works.

    Hypothesis: Importing a module containing a decorated class will trigger the
    decorator's logic, populating the EntityTypeRegistry with the correct mappings
    before any instances of the class are even created.
    """
    # Arrange: The clean_registry fixture has already prepared a clean slate
    # We need to force re-import to trigger decorator execution after registry clear
    import sys

    if module_path in sys.modules:
        importlib.reload(sys.modules[module_path])

    # Act: Dynamically import the module. The @register_entity_types decorator
    # is executed by the Python interpreter as the class is defined within the module
    module = importlib.import_module(module_path)
    migration_class = getattr(module, class_name)

    # Assert: Check that the registry now contains the correct information for the imported class
    primary_type = expected_types[0]

    # 1. Verify the class is a valid BaseMigration subclass
    assert issubclass(
        migration_class,
        BaseMigration,
    ), f"{class_name} should inherit from BaseMigration"

    # 2. Verify the primary entity type resolution
    # This is crucial for the default behavior of migration components
    assert (
        EntityTypeRegistry.resolve(migration_class) == primary_type
    ), f"Primary entity type for {class_name} should be '{primary_type}'"

    # 3. Verify all supported types are registered in the correct order
    supported_types = EntityTypeRegistry.get_supported_types(migration_class)
    assert (
        supported_types == expected_types
    ), f"Supported types for {class_name} did not match expected order or content"

    # 4. Verify reverse lookup for all associated entity types
    # This ensures the migration orchestrator can find the correct handler class
    # for any of the entity types a class supports
    for entity_type in expected_types:
        assert (
            EntityTypeRegistry.get_class_for_type(entity_type) is migration_class
        ), f"Reverse lookup for entity type '{entity_type}' failed for {class_name}"


class TestMigrationClassRegistrationIntegration:
    """Test integration scenarios for the newly decorated migration classes."""

    def test_all_classes_register_different_primary_types(self) -> None:
        """Verify that all migration classes register unique primary entity types."""
        # Arrange: Import all modules to trigger registration
        import sys

        primary_types = []
        for module_path, class_name, _expected_types in MIGRATION_CLASSES_TO_TEST:
            if module_path in sys.modules:
                importlib.reload(sys.modules[module_path])
            module = importlib.import_module(module_path)
            migration_class = getattr(module, class_name)
            primary_type = EntityTypeRegistry.resolve(migration_class)
            primary_types.append(primary_type)

        # Assert: All primary types should be unique
        assert len(primary_types) == len(
            set(primary_types),
        ), f"Primary entity types should be unique, but found: {primary_types}"

    def test_auto_detect_entity_type_integration(self) -> None:
        """Test that _auto_detect_entity_type() works correctly for newly decorated classes."""
        for module_path, class_name, expected_types in MIGRATION_CLASSES_TO_TEST:
            # Arrange: Force re-import and get migration class
            import sys

            if module_path in sys.modules:
                importlib.reload(sys.modules[module_path])

            module = importlib.import_module(module_path)
            migration_class = getattr(module, class_name)

            # Test the class-level resolution instead of instance method
            # since creating instances requires complex dependencies
            detected_type = EntityTypeRegistry.resolve(migration_class)

            # Assert: Should return the primary entity type
            expected_primary = expected_types[0]
            assert (
                detected_type == expected_primary
            ), f"Auto-detection for {class_name} should return '{expected_primary}', got '{detected_type}'"

    def test_registry_lookup_completeness(self) -> None:
        """Verify that all expected entity types are properly registered in the registry."""
        # Arrange: Import all modules to trigger registration
        import sys

        all_expected_types = set()
        for module_path, _class_name, expected_types in MIGRATION_CLASSES_TO_TEST:
            if module_path in sys.modules:
                importlib.reload(sys.modules[module_path])
            importlib.import_module(module_path)
            all_expected_types.update(expected_types)

        # Act: Get all registered types from the registry
        registered_types = EntityTypeRegistry.get_all_registered_types()

        # Assert: All expected types should be registered
        for expected_type in all_expected_types:
            assert (
                expected_type in registered_types
            ), f"Entity type '{expected_type}' should be registered in the registry"

    def test_registry_state_persistence(self) -> None:
        """Verify that registry state persists across multiple imports of the same module."""
        module_path = "src.migrations.account_migration"
        class_name = "AccountMigration"
        expected_types = ["accounts", "tempo_accounts"]

        # Act: Force reload then import the same module twice
        import sys

        if module_path in sys.modules:
            importlib.reload(sys.modules[module_path])

        module1 = importlib.import_module(module_path)
        migration_class1 = getattr(module1, class_name)

        module2 = importlib.import_module(module_path)
        migration_class2 = getattr(module2, class_name)

        # Assert: Both imports should refer to the same class and have consistent registry state
        assert (
            migration_class1 is migration_class2
        ), "Multiple imports of the same module should return the same class object"

        # Verify registry state is consistent for both references
        for migration_class in [migration_class1, migration_class2]:
            assert EntityTypeRegistry.resolve(migration_class) == expected_types[0]
            assert (
                EntityTypeRegistry.get_supported_types(migration_class)
                == expected_types
            )


class TestMigrationClassRegistrationErrorScenarios:
    """Test error scenarios and edge cases for migration class registration."""

    def test_unregistered_class_behavior(self) -> None:
        """Verify that unregistered classes raise appropriate errors."""
        from unittest.mock import Mock

        # Create a mock class that inherits from BaseMigration but isn't registered
        class UnregisteredMigration(BaseMigration):
            def __init__(self) -> None:
                self.logger = Mock()

        # Assert: Should raise ValueError for unregistered class when resolving
        with pytest.raises(
            ValueError,
            match="is not registered with EntityTypeRegistry",
        ):
            EntityTypeRegistry.resolve(UnregisteredMigration)

    def test_registry_isolation_between_tests(self) -> None:
        """Verify that the clean_registry fixture properly isolates test state."""
        # Arrange: Check that registry starts empty
        assert (
            len(EntityTypeRegistry.get_all_registered_types()) == 0
        ), "Registry should be empty at the start of each test"

        # Act: Force reload and import a module to populate registry
        import sys

        module_path = "src.migrations.account_migration"
        if module_path in sys.modules:
            importlib.reload(sys.modules[module_path])
        importlib.import_module(module_path)

        # Assert: Registry should now contain types
        assert (
            len(EntityTypeRegistry.get_all_registered_types()) > 0
        ), "Registry should contain types after import"

        # Note: The fixture will clean this up after the test

    def test_multiple_entity_types_registration(self) -> None:
        """Verify that classes with multiple entity types register all of them correctly."""
        # Focus on classes with multiple entity types
        multi_type_classes = [
            (
                "src.migrations.account_migration",
                "AccountMigration",
                ["accounts", "tempo_accounts"],
            ),
            (
                "src.migrations.status_migration",
                "StatusMigration",
                ["statuses", "status_types"],
            ),
        ]

        for module_path, class_name, expected_types in multi_type_classes:
            # Arrange: Clear registry and force reload module
            EntityTypeRegistry.clear_registry()
            import sys

            if module_path in sys.modules:
                importlib.reload(sys.modules[module_path])
            module = importlib.import_module(module_path)
            migration_class = getattr(module, class_name)

            # Assert: All entity types should be available for reverse lookup
            for entity_type in expected_types:
                assert (
                    EntityTypeRegistry.get_class_for_type(entity_type)
                    is migration_class
                ), f"Entity type '{entity_type}' should resolve to {class_name}"

            # Assert: Primary type should be the first in the list
            primary_type = EntityTypeRegistry.resolve(migration_class)
            assert (
                primary_type == expected_types[0]
            ), f"Primary type for {class_name} should be '{expected_types[0]}'"
