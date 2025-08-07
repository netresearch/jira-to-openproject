#!/usr/bin/env python3
"""Simple test for the error recovery system."""

import sys
import tempfile
from pathlib import Path

# Add the src directory to the path
sys.path.insert(0, str(Path(__file__).parent / "src"))

try:
    from utils.error_recovery import (  # noqa: F401
        ErrorRecoverySystem,
        MigrationCheckpoint,
    )

    print("✓ Successfully imported ErrorRecoverySystem")
except ImportError as e:
    print(f"✗ Failed to import ErrorRecoverySystem: {e}")
    sys.exit(1)


def test_error_recovery_system() -> bool | None:
    """Test the error recovery system."""
    print("\nTesting ErrorRecoverySystem...")

    # Create a temporary database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        # Create error recovery system
        error_recovery_system = ErrorRecoverySystem(db_path=str(db_path))
        print("✓ ErrorRecoverySystem created successfully")

        # Test migration status (should be empty initially)
        status = error_recovery_system.get_migration_status("test_migration")
        print(f"✓ Migration status retrieved: {status}")

        # Test resume migration (should be empty initially)
        resume_list = error_recovery_system.resume_migration("test_migration")
        print(f"✓ Resume migration returned {len(resume_list)} entities")

        # Test execute with recovery
        def test_function(x, y):
            return x + y

        result = error_recovery_system.execute_with_recovery(
            migration_id="test_migration",
            checkpoint_type="test",
            entity_id="test_entity",
            func=test_function,
            x=5,
            y=3,
        )
        print(f"✓ Execute with recovery returned: {result}")

        print("\n🎉 All tests passed!")
        return True

    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False
    finally:
        # Clean up
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    success = test_error_recovery_system()
    sys.exit(0 if success else 1)
