#!/usr/bin/env python3
"""
Comprehensive test for 10 NRS issues including known problematic ones.
Uses existing migration infrastructure with enhanced validation and logging.
"""

import json
import logging
import sys
import subprocess
import time
from pathlib import Path
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Test issues - including known problematic ones from Bug #10
TEST_ISSUES = [
    "NRS-171",  # From previous successful tests
    "NRS-182",  # From previous successful tests
    "NRS-191",  # From previous successful tests
    "NRS-198",  # From previous successful tests
    "NRS-204",  # From previous successful tests
    "NRS-42",   # Known Bug #10 issue (due_date 3 days before start_date)
    "NRS-59",   # Known Bug #10 issue (due_date 1 day before start_date)
    "NRS-66",   # Known Bug #10 issue (due_date 9 days before start_date)
    "NRS-982",  # Known Bug #10 issue (due_date 10 days before start_date)
    "NRS-4003", # Known Bug #10 issue (due_date 2 YEARS before start_date - extreme case)
]

def run_migration():
    """
    Run migration for test issues using existing infrastructure.
    """
    logger.info("="*80)
    logger.info("COMPREHENSIVE 10-ISSUE MIGRATION TEST")
    logger.info(f"Test issues: {', '.join(TEST_ISSUES)}")
    logger.info(f"Start time: {datetime.now().isoformat()}")
    logger.info("="*80)

    # Update config to filter for test issues
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    logger.info(f"Using config: {config_path}")

    # Run migration with specific issue filter
    cmd = [
        "python",
        "-m", "src.main",
        "--components", "work_packages",
        "--no-backup",
        "--force",
    ]

    # Create temporary JQL filter file for test issues
    jql = f"project = NRS AND key in ({', '.join(TEST_ISSUES)})"
    logger.info(f"JQL Filter: {jql}")

    # Set environment variable for JQL filter
    import os
    os.environ["J2O_TEST_JQL_FILTER"] = jql

    logger.info(f"Executing: {' '.join(cmd)}")
    logger.info("Migration started - monitoring actively (no background execution)")

    start_time = time.time()

    try:
        # Run with active monitoring (not in background)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )

        # Stream output in real-time
        for line in process.stdout:
            print(line, end='')
            sys.stdout.flush()

        # Wait for completion
        return_code = process.wait()

        duration = time.time() - start_time
        logger.info(f"\nMigration completed in {duration:.1f}s with return code: {return_code}")

        return return_code == 0

    except Exception as e:
        logger.error(f"Migration failed with exception: {e}")
        return False


def analyze_results():
    """
    Analyze migration results and provide detailed report.
    """
    logger.info("\n" + "="*80)
    logger.info("ANALYZING RESULTS")
    logger.info("="*80)

    results_dir = Path(__file__).parent.parent / "var" / "results"
    data_dir = Path(__file__).parent.parent / "var" / "data"

    # Find most recent result file
    result_files = sorted(results_dir.glob("migration_results_*.json"))
    if not result_files:
        logger.error("No result files found!")
        return False

    latest_result = result_files[-1]
    logger.info(f"Latest result file: {latest_result}")

    with open(latest_result) as f:
        results = json.load(f)

    # Extract work package results
    wp_results = results.get("components", {}).get("work_packages", {})
    logger.info(f"\nWork Packages Component:")
    logger.info(f"  Success: {wp_results.get('success')}")
    logger.info(f"  Total Created: {wp_results.get('data', {}).get('total_created', 0)}")
    logger.info(f"  Total Issues: {wp_results.get('data', {}).get('total_issues', 0)}")

    # Find most recent bulk result files
    bulk_files = sorted(data_dir.glob("bulk_result_NRS_*.json"))
    if bulk_files:
        logger.info(f"\nBulk Result Files: {len(bulk_files)} found")

        # Analyze last 3 bulk files
        for bulk_file in bulk_files[-3:]:
            with open(bulk_file) as f:
                bulk_data = json.load(f)

            result = bulk_data.get("result", {})
            logger.info(f"\n  {bulk_file.name}:")
            logger.info(f"    Created: {result.get('created_count', 0)}")
            logger.info(f"    Errors: {result.get('error_count', 0)}")

            # Show error details
            errors = result.get("errors", [])
            if errors:
                logger.info(f"    Error Details:")
                for err in errors[:5]:  # Show first 5 errors
                    logger.info(f"      - Index {err.get('index')}: {err.get('errors')}")

    # Check for created work packages
    logger.info("\nChecking created work packages...")
    try:
        # Use OpenProject client to check
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from src.clients.openproject_client import OpenProjectClient
        from src.config import config

        op_client = OpenProjectClient(config=config.openproject_config)

        # Query for work packages with our test issue keys
        for issue_key in TEST_ISSUES:
            result = op_client.execute_large_query_to_json_file(
                f"WorkPackage.joins(:custom_values).where(custom_values: {{value: '{issue_key}'}}).pluck(:id, :subject)",
                timeout=30,
            )
            if result:
                logger.info(f"  ✅ {issue_key}: Found WP {result}")
            else:
                logger.warning(f"  ❌ {issue_key}: Not found")
    except Exception as e:
        logger.error(f"Error checking work packages: {e}")

    return True


def main():
    """
    Main execution flow.
    """
    logger.info("Starting comprehensive migration test...")

    # Run migration
    success = run_migration()

    # Analyze results
    analyze_results()

    if success:
        logger.info("\n✅ Test completed successfully")
        return 0
    else:
        logger.error("\n❌ Test failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
