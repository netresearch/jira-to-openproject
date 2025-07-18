#!/usr/bin/env python3
"""Test zero duration validation in the actual migrator implementation."""

import sys
sys.path.insert(0, 'src')

from unittest.mock import Mock, patch
from utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator

def test_zero_duration_validation():
    """Test that the migrator rejects zero duration values."""
    
    # Create a minimal migrator instance with mocked dependencies
    with patch('utils.enhanced_user_association_migrator.logging'):
        migrator = EnhancedUserAssociationMigrator(
            jira_client=Mock(),
            openproject_client=Mock(),
            user_mapping={},
            config_path="dummy"
        )
        
        # Test zero duration rejection
        try:
            result = migrator._parse_duration("0s")
            print(f"❌ FAILED: Expected ValueError for '0s', but got {result}")
            return False
        except ValueError as e:
            print(f"✅ SUCCESS: '0s' correctly rejected with: {e}")
            
        # Test negative duration rejection  
        try:
            result = migrator._parse_duration("-1s")
            print(f"❌ FAILED: Expected ValueError for '-1s', but got {result}")
            return False
        except ValueError as e:
            print(f"✅ SUCCESS: '-1s' correctly rejected with: {e}")
            
        # Test valid positive duration
        try:
            result = migrator._parse_duration("1s")
            print(f"✅ SUCCESS: '1s' correctly accepted as {result}")
            return True
        except ValueError as e:
            print(f"❌ FAILED: '1s' should be valid but got: {e}")
            return False

if __name__ == "__main__":
    print("=== Testing Zero Duration Validation ===")
    success = test_zero_duration_validation()
    if success:
        print("✅ Zero duration validation works correctly!")
    else:
        print("❌ Zero duration validation failed!")
        sys.exit(1) 