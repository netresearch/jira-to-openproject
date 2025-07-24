"""Mock time utilities for test performance optimization."""

import time
from unittest.mock import patch
from typing import Optional


class MockTime:
    """Mock time utilities for faster tests."""
    
    def __init__(self):
        self.current_time = 0.0
        self.sleep_calls = []
    
    def time(self):
        """Mock time.time() that returns controlled time."""
        return self.current_time
    
    def sleep(self, seconds: float):
        """Mock time.sleep() that advances time without real delay."""
        self.sleep_calls.append(seconds)
        self.current_time += seconds
    
    def advance_time(self, seconds: float):
        """Manually advance the mock time."""
        self.current_time += seconds


def patch_time_sleep():
    """Patch time.sleep to do nothing for faster tests."""
    return patch('time.sleep', lambda x: None)


def patch_time_module():
    """Patch the entire time module for controlled testing."""
    mock_time = MockTime()
    
    def mock_time_func():
        return mock_time.time()
    
    def mock_sleep_func(seconds: float):
        mock_time.sleep(seconds)
    
    patches = [
        patch('time.time', mock_time_func),
        patch('time.sleep', mock_sleep_func),
    ]
    
    return patches, mock_time 