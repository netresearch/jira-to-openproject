#!/usr/bin/env python3
"""Enhanced test helper script for improved developer testing experience.

This script provides convenient shortcuts and enhanced functionality for running tests
with better developer experience, including smart defaults, quick commands, and
integrated reporting.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def print_banner(title: str) -> None:
    """Print a formatted banner for better visibility."""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_success(message: str) -> None:
    """Print a success message."""
    print(f"✅ {message}")


def print_error(message: str) -> None:
    """Print an error message."""
    print(f"❌ {message}")


def print_info(message: str) -> None:
    """Print an info message."""
    print(f"ℹ️  {message}")


def run_quick_tests() -> int:
    """Run a quick subset of tests for rapid feedback."""
    print_banner("Quick Test Suite")
    print_info("Running unit tests only for rapid feedback...")

    cmd = [
        "python",
        "-m",
        "pytest",
        "tests/unit",
        "-x",  # Stop on first failure
        "--tb=short",  # Short traceback format
        "-q",  # Quiet output
    ]

    start_time = time.time()
    result = subprocess.call(cmd)
    duration = time.time() - start_time

    if result == 0:
        print_success(f"Quick tests passed in {duration:.1f}s")
    else:
        print_error(f"Quick tests failed after {duration:.1f}s")

    return result


def run_smoke_tests() -> int:
    """Run smoke tests to verify basic functionality."""
    print_banner("Smoke Test Suite")
    print_info("Running critical path tests...")

    cmd = [
        "python",
        "-m",
        "pytest",
        "-m",
        "unit and not slow",
        "-x",
        "--tb=short",
        "-v",
    ]

    start_time = time.time()
    result = subprocess.call(cmd)
    duration = time.time() - start_time

    if result == 0:
        print_success(f"Smoke tests passed in {duration:.1f}s")
    else:
        print_error(f"Smoke tests failed after {duration:.1f}s")

    return result


def run_comprehensive_tests() -> int:
    """Run the full comprehensive test suite with coverage."""
    print_banner("Comprehensive Test Suite")
    print_info("Running all tests with coverage reporting...")

    # Ensure reports directory exists
    os.makedirs("reports", exist_ok=True)

    cmd = [
        "python",
        "-m",
        "pytest",
        "--cov=src",
        "--cov-report=html:reports/coverage",
        "--cov-report=term",
        "--cov-report=xml:reports/coverage.xml",
        "--junitxml=reports/junit.xml",
        "-v",
    ]

    start_time = time.time()
    result = subprocess.call(cmd)
    duration = time.time() - start_time

    if result == 0:
        print_success(f"All tests passed in {duration:.1f}s")
        print_info("Coverage report: reports/coverage/index.html")
        print_info("JUnit report: reports/junit.xml")
    else:
        print_error(f"Tests failed after {duration:.1f}s")

    return result


def run_specific_module(module: str) -> int:
    """Run tests for a specific module."""
    print_banner(f"Testing Module: {module}")

    # Try to find tests for the module
    possible_paths = [
        f"tests/unit/test_{module}.py",
        f"tests/unit/{module}/",
        f"tests/functional/test_{module}.py",
        f"tests/functional/{module}/",
    ]

    test_path = None
    for path in possible_paths:
        if Path(path).exists():
            test_path = path
            break

    if not test_path:
        print_error(f"No tests found for module '{module}'")
        print_info("Available test files:")
        for test_file in Path("tests").rglob("test_*.py"):
            print(f"  {test_file}")
        return 1

    print_info(f"Running tests in {test_path}")

    cmd = [
        "python",
        "-m",
        "pytest",
        test_path,
        "-v",
        "--tb=short",
    ]

    return subprocess.call(cmd)


def run_failed_tests() -> int:
    """Re-run only the tests that failed in the last run."""
    print_banner("Re-running Failed Tests")
    print_info("Running tests that failed in the last run...")

    cmd = [
        "python",
        "-m",
        "pytest",
        "--lf",  # Last failed
        "-v",
        "--tb=short",
    ]

    return subprocess.call(cmd)


def run_changed_tests() -> int:
    """Run tests related to changed files (requires git)."""
    print_banner("Testing Changed Files")
    print_info("Running tests related to changed files...")

    # Get changed files from git
    try:
        changed_files = (
            subprocess.check_output(["git", "diff", "--name-only", "HEAD~1"], text=True)
            .strip()
            .split("\n")
        )

        # Filter for Python files in src/
        src_files = [
            f for f in changed_files if f.startswith("src/") and f.endswith(".py")
        ]

        if not src_files:
            print_info("No changed source files found")
            return 0

        print_info(f"Changed files: {', '.join(src_files)}")

        # Run tests with file names as keywords
        keywords = " or ".join([Path(f).stem for f in src_files])

        cmd = [
            "python",
            "-m",
            "pytest",
            "-k",
            keywords,
            "-v",
            "--tb=short",
        ]

        return subprocess.call(cmd)

    except subprocess.CalledProcessError:
        print_error("Git not available or no changes detected")
        return 1


def run_performance_tests() -> int:
    """Run performance and slow tests."""
    print_banner("Performance Test Suite")
    print_info("Running slow and performance tests...")

    cmd = [
        "python",
        "-m",
        "pytest",
        "-m",
        "slow",
        "-v",
        "--tb=short",
        "--durations=10",  # Show 10 slowest tests
    ]

    return subprocess.call(cmd)


def setup_test_environment() -> int:
    """Set up the test environment and dependencies."""
    print_banner("Test Environment Setup")

    steps = [
        ("Installing test dependencies", ["pip", "install", "-e", ".[test,dev]"]),
        ("Installing pre-commit hooks", ["pre-commit", "install"]),
        ("Creating reports directory", ["mkdir", "-p", "reports"]),
    ]

    for description, cmd in steps:
        print_info(f"{description}...")
        result = subprocess.call(cmd)
        if result != 0:
            print_error(f"Failed: {description}")
            return result
        print_success(f"Completed: {description}")

    print_success("Test environment setup complete!")
    return 0


def clean_test_artifacts() -> int:
    """Clean up test artifacts and cache files."""
    print_banner("Cleaning Test Artifacts")

    artifacts = [
        ".pytest_cache",
        "__pycache__",
        "*.pyc",
        ".coverage",
        "reports/coverage",
        "reports/junit.xml",
    ]

    for artifact in artifacts:
        print_info(f"Cleaning {artifact}...")
        if artifact.startswith("."):
            subprocess.call(
                ["find", ".", "-name", artifact, "-exec", "rm", "-rf", "{}", "+"]
            )
        else:
            subprocess.call(["find", ".", "-name", artifact, "-delete"])

    print_success("Test artifacts cleaned!")
    return 0


def main() -> int:
    """Main entry point for the test helper."""
    parser = argparse.ArgumentParser(
        description="Enhanced test helper for improved developer experience",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s quick          # Run quick unit tests for rapid feedback
  %(prog)s smoke          # Run smoke tests (critical path)
  %(prog)s full           # Run comprehensive test suite with coverage
  %(prog)s module config  # Run tests for the config module
  %(prog)s failed         # Re-run only failed tests from last run
  %(prog)s changed        # Run tests for changed files (requires git)
  %(prog)s perf           # Run performance tests
  %(prog)s setup          # Set up test environment
  %(prog)s clean          # Clean test artifacts

Test Types:
  quick      - Fast unit tests only (~30s)
  smoke      - Critical path tests (~2-3min)
  full       - Complete suite with coverage (~5-10min)
  module     - Tests for specific module
  failed     - Re-run failed tests from last run
  changed    - Tests for git-changed files
  perf       - Performance and slow tests
        """,
    )

    parser.add_argument(
        "command",
        choices=[
            "quick",
            "smoke",
            "full",
            "module",
            "failed",
            "changed",
            "perf",
            "setup",
            "clean",
        ],
        help="Test command to run",
    )

    parser.add_argument(
        "module", nargs="?", help="Module name (required for 'module' command)"
    )

    args = parser.parse_args()

    if args.command == "quick":
        return run_quick_tests()
    elif args.command == "smoke":
        return run_smoke_tests()
    elif args.command == "full":
        return run_comprehensive_tests()
    elif args.command == "module":
        if not args.module:
            print_error("Module name is required for 'module' command")
            return 1
        return run_specific_module(args.module)
    elif args.command == "failed":
        return run_failed_tests()
    elif args.command == "changed":
        return run_changed_tests()
    elif args.command == "perf":
        return run_performance_tests()
    elif args.command == "setup":
        return setup_test_environment()
    elif args.command == "clean":
        return clean_test_artifacts()
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
