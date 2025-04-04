#!/usr/bin/env python3
"""
Test script to check the return type of run_migration function.
"""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

from run_migration import run_migration

# Run a simple migration with dry-run mode
result = run_migration(dry_run=True, components=['users'], no_backup=True)

# Print the result structure
print('Return type:', type(result))
print('Result structure:', result.keys())
print('Components:', result.get('components', {}).keys())
print('Overall:', result.get('overall', {}).keys())
