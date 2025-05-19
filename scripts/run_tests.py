#!/usr/bin/env python3
"""Script to run different types of tests with proper configuration."""

import argparse
import os
import subprocess
import sys
from typing import List, Optional


def run_tests(
    test_type: Optional[str] = None,
    test_path: Optional[str] = None,
    verbose: bool = False,
    coverage: bool = False,
    markers: Optional[str] = None,
    keyword: Optional[str] = None,
    junit_report: bool = False,
    parallel: bool = False,
    extra_args: Optional[List[str]] = None,
) -> int:
    """Run tests with the specified configuration.

    Args:
        test_type: Type of tests to run ('unit', 'functional', 'integration', 'end_to_end', or None for all)
        test_path: Specific test path or file to run
        verbose: Whether to show verbose output
        coverage: Whether to generate coverage report
        markers: Pytest markers to include
        keyword: Keyword expression to filter tests
        junit_report: Whether to generate JUnit XML report
        parallel: Whether to run tests in parallel
        extra_args: Additional arguments to pass to pytest

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    cmd = ["python", "-m", "pytest"]

    # Handle test type
    if test_type:
        if test_type == "unit":
            cmd.append("tests/unit")
        elif test_type == "functional":
            cmd.append("tests/functional")
        elif test_type == "integration":
            cmd.append("tests/integration")
        elif test_type == "end_to_end":
            cmd.append("tests/end_to_end")

        # Add marker based on test type
        if not markers:
            cmd.extend(["-m", test_type])

    # Handle specific test path
    if test_path:
        cmd.append(test_path)

    # Handle verbosity
    if verbose:
        cmd.append("-v")

    # Handle markers
    if markers:
        cmd.extend(["-m", markers])

    # Handle keyword expression
    if keyword:
        cmd.extend(["-k", keyword])

    # Handle coverage
    if coverage:
        cmd.extend(["--cov=src", "--cov-report=term", "--cov-report=html:reports/coverage"])

    # Handle JUnit report
    if junit_report:
        cmd.append("--junitxml=reports/junit.xml")

    # Handle parallel execution
    if parallel:
        cmd.append("-xvs")

    # Add additional arguments
    if extra_args:
        cmd.extend(extra_args)

    # Create reports directory if needed
    if coverage or junit_report:
        os.makedirs("reports", exist_ok=True)

    # Print command for clarity
    cmd_str = " ".join(cmd)
    print(f"Running: {cmd_str}")

    # Run tests
    return subprocess.call(cmd)


def main() -> int:
    """Parse command line arguments and run tests.

    Returns:
        int: Exit code
    """
    parser = argparse.ArgumentParser(description="Run different types of tests")

    # Test type
    parser.add_argument(
        "--type",
        choices=["unit", "functional", "integration", "end_to_end", "all"],
        default="all",
        help="Type of tests to run",
    )

    # Test path
    parser.add_argument(
        "--path",
        help="Specific test path or file to run"
    )

    # Verbosity
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show verbose output"
    )

    # Coverage
    parser.add_argument(
        "--cov", "--coverage",
        action="store_true",
        help="Generate coverage report"
    )

    # Markers
    parser.add_argument(
        "-m", "--markers",
        help="Pytest markers to include"
    )

    # Keyword expression
    parser.add_argument(
        "-k", "--keyword",
        help="Keyword expression to filter tests"
    )

    # JUnit report
    parser.add_argument(
        "--junit",
        action="store_true",
        help="Generate JUnit XML report"
    )

    # Parallel execution
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Run tests in parallel"
    )

    # Additional arguments
    parser.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="Additional arguments to pass to pytest"
    )

    args = parser.parse_args()

    # Handle 'all' test type
    test_type = None if args.type == "all" else args.type

    # Run tests
    return run_tests(
        test_type=test_type,
        test_path=args.path,
        verbose=args.verbose,
        coverage=args.cov,
        markers=args.markers,
        keyword=args.keyword,
        junit_report=args.junit,
        parallel=args.parallel,
        extra_args=args.rest
    )


if __name__ == "__main__":
    sys.exit(main())
