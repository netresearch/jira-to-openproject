#!/usr/bin/env python3
"""Profiling script for file transfer chain test.

Run with: python test_with_profile.py
"""

import cProfile
import io
import os
import pstats
import time
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv


# Load environment variables from .env files
def load_environment_configuration() -> None:
    """Load environment variables from .env files based on execution context.

    Following the project's configuration loading order:
    - .env (base config for all environments)
    - .env.local (local development overrides, if present)
    - .env.test (test-specific config)
    - .env.test.local (local test overrides, if present)
    """
    # Always load base configuration
    load_dotenv(".env")

    # In test mode, load both .env.local and .env.test
    # with test having higher precedence
    if Path(".env.local").exists():
        load_dotenv(".env.local", override=True)

    # Always load .env.test for testing
    if Path(".env.test").exists():
        load_dotenv(".env.test", override=True)

    # Load test-specific local overrides if they exist
    if Path(".env.test.local").exists():
        load_dotenv(".env.test.local", override=True)


# Load environment configuration
load_environment_configuration()

# Set TEST_INTEGRATION=true to run the tests
os.environ["TEST_INTEGRATION"] = "true"


# Set default values for required environment variables if not already set
def set_default_env(key: str, default: str) -> None:
    """Set default value for environment variable if not already set."""
    if key not in os.environ:
        os.environ[key] = default


# Force mock mode to avoid actual network connections
if "J2O_TEST_MOCK_MODE" not in os.environ:
    os.environ["J2O_TEST_MOCK_MODE"] = "true"


@contextmanager
def timing(_description: str) -> Generator[None, None, None]:
    """Context manager for timing code blocks."""
    start = time.time()
    yield
    time.time() - start


def run_tests_with_profiling() -> None:
    """Run tests with profiling and timing."""
    # Import the test module
    with timing("Import test module"):
        from tests.integration import test_file_transfer_chain

    # Create the test suite
    with timing("Create test suite"):
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(test_file_transfer_chain)

    # Run the tests with profiling
    with timing("Run tests with profiling"):
        profile = cProfile.Profile()
        profile.enable()
        unittest.TextTestRunner(verbosity=2).run(suite)
        profile.disable()

    # Print profiling stats sorted by cumulative time
    s = io.StringIO()
    ps = pstats.Stats(profile, stream=s).sort_stats("cumulative")
    ps.print_stats(30)  # Print top 30 functions by cumulative time

    # Print profiling stats sorted by total time
    s = io.StringIO()
    ps = pstats.Stats(profile, stream=s).sort_stats("time")
    ps.print_stats(30)  # Print top 30 functions by total time

    # Save profiling results to file
    ps.dump_stats("profile_results.prof")


if __name__ == "__main__":
    # Set up timing for the entire process
    with timing("Total execution"):
        run_tests_with_profiling()
