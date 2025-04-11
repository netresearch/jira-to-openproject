#!/usr/bin/env python
"""
Test runner script for the Jira to OpenProject migration tool.
Discovers and runs all tests in the 'tests' directory.
"""
import sys
import os
import unittest
import argparse
import logging

# Add the project root to the Python path so tests can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def run_tests(test_pattern=None, verbose=False, quiet=False):
    """
    Run tests matching the given pattern.

    Args:
        test_pattern (str): Pattern for test discovery (e.g., 'test_user_migration.py')
        verbose (bool): Whether to output verbose test results
        quiet (bool): Whether to suppress log messages during test execution

    Returns:
        bool: True if all tests pass, False otherwise
    """
    # If quiet mode is enabled, temporarily raise the log level
    if quiet:
        # Store the original log levels of all loggers
        original_levels = {}
        for logger_name in logging.root.manager.loggerDict:
            logger = logging.getLogger(logger_name)
            original_levels[logger_name] = logger.level
            logger.setLevel(logging.WARNING)

        # Also set the root logger to WARNING
        root_level = logging.root.level
        logging.root.setLevel(logging.WARNING)

    try:
        # Configure test loader
        loader = unittest.TestLoader()

        # Discover tests
        if test_pattern:
            print(f"Running tests matching pattern: {test_pattern}")

            # Check if the pattern is a specific test case (contains '.py:')
            if ':' in test_pattern:
                file_path, test_name = test_pattern.split(':')

                # Load specific test
                if os.path.isfile(file_path):
                    test_dir = os.path.dirname(file_path)
                    module_name = os.path.basename(file_path).replace('.py', '')

                    # Import the module and find the test
                    sys.path.insert(0, test_dir)
                    module = __import__(module_name)

                    # Try to find the test case or test method
                    for name in dir(module):
                        obj = getattr(module, name)
                        if isinstance(obj, type) and issubclass(obj, unittest.TestCase):
                            if name == test_name or hasattr(obj, test_name):
                                suite = unittest.TestSuite()

                                if name == test_name:
                                    # Add all tests from the test case
                                    suite.addTest(loader.loadTestsFromTestCase(obj))
                                else:
                                    # Add specific test method
                                    suite.addTest(obj(test_name))

                                break
                else:
                    print(f"Test file not found: {file_path}")
                    return False
            else:
                # Pattern is a file or module name
                tests_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'tests'))
                pattern = f"*{test_pattern}*" if not test_pattern.endswith('.py') else test_pattern
                suite = loader.discover(tests_dir, pattern=pattern)
        else:
            # Discover all tests
            tests_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'tests'))
            print(f"Discovering tests in: {tests_dir}")
            suite = loader.discover(tests_dir)

        # Run tests
        verbosity = 2 if verbose else 1
        runner = unittest.TextTestRunner(verbosity=verbosity)
        result = runner.run(suite)

        # Return True if all tests passed
        return result.wasSuccessful()

    finally:
        # Restore original log levels if in quiet mode
        if quiet:
            for logger_name, level in original_levels.items():
                logging.getLogger(logger_name).setLevel(level)
            logging.root.setLevel(root_level)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run tests for the Jira to OpenProject migration tool")
    parser.add_argument("--pattern", "-p", help="Pattern for test discovery (e.g., 'test_user_migration.py')")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress log messages during test execution")

    args = parser.parse_args()

    success = run_tests(args.pattern, args.verbose, args.quiet)

    # Exit with appropriate status code
    sys.exit(0 if success else 1)
