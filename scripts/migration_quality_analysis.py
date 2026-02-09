#!/usr/bin/env python3
"""Migration Quality Analysis - Compare Jira issues with OpenProject work packages.

This script samples Jira issues and compares them with their migrated OP counterparts
to identify data quality issues and gaps in the migration.
"""

import json
import random
import sys
from pathlib import Path
from datetime import datetime
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient


def load_mappings() -> dict[str, Any]:
    """Load all relevant mapping files."""
    data_dir = Path("var/data")
    mappings = {}

    mapping_files = [
        "work_package_mapping.json",
        "user_mapping.json",
        "priority_mapping.json",
        "project_mapping.json",
        "status_mapping.json",
    ]

    for fname in mapping_files:
        fpath = data_dir / fname
        if fpath.exists():
            with open(fpath) as f:
                mappings[fname.replace(".json", "")] = json.load(f)

    return mappings


def sample_jira_issues(jira_client: JiraClient, sample_size: int = 100) -> list[dict]:
    """Sample random Jira issues from various projects."""
    print(f"Sampling {sample_size} random Jira issues...")

    # Get all project keys from mapping
    mappings = load_mappings()
    wp_mapping = mappings.get("work_package_mapping", {})

    # Get unique project keys from WP mapping
    project_keys = set()
    for entry in wp_mapping.values():
        if "project_key" in entry:
            project_keys.add(entry["project_key"])

    print(f"Found {len(project_keys)} unique projects in mapping")

    # Sample issues across projects
    sampled_issues = []
    jira_ids = list(wp_mapping.keys())

    if len(jira_ids) < sample_size:
        sample_ids = jira_ids
    else:
        sample_ids = random.sample(jira_ids, sample_size)

    # Get full issue data from Jira
    batch_size = 50
    for i in range(0, len(sample_ids), batch_size):
        batch = sample_ids[i:i+batch_size]
        jira_keys = [wp_mapping[jid]["jira_key"] for jid in batch if jid in wp_mapping]

        if jira_keys:
            jql = f"key in ({','.join(jira_keys)})"
            try:
                issues = jira_client.jira.search_issues(
                    jql,
                    maxResults=len(jira_keys),
                    fields="*all",
                    expand="changelog"
                )
                for issue in issues:
                    sampled_issues.append({
                        "jira_id": issue.id,
                        "jira_key": issue.key,
                        "fields": {
                            "summary": issue.fields.summary,
                            "priority": getattr(issue.fields.priority, "name", None) if issue.fields.priority else None,
                            "priority_id": getattr(issue.fields.priority, "id", None) if issue.fields.priority else None,
                            "assignee": getattr(issue.fields.assignee, "name", None) if issue.fields.assignee else None,
                            "assignee_display": getattr(issue.fields.assignee, "displayName", None) if issue.fields.assignee else None,
                            "reporter": getattr(issue.fields.reporter, "name", None) if issue.fields.reporter else None,
                            "reporter_display": getattr(issue.fields.reporter, "displayName", None) if issue.fields.reporter else None,
                            "status": getattr(issue.fields.status, "name", None) if issue.fields.status else None,
                            "issuetype": getattr(issue.fields.issuetype, "name", None) if issue.fields.issuetype else None,
                            "created": issue.fields.created,
                            "updated": issue.fields.updated,
                            "project_key": issue.fields.project.key,
                            "description": issue.fields.description[:200] if issue.fields.description else None,
                        }
                    })
            except Exception as e:
                print(f"  Error fetching batch: {e}")

    print(f"Successfully sampled {len(sampled_issues)} issues from Jira")
    return sampled_issues


def get_op_work_packages(op_client: OpenProjectClient, jira_issues: list[dict], mappings: dict) -> dict[str, dict]:
    """Get corresponding OpenProject work packages."""
    print("Fetching corresponding OpenProject work packages...")

    wp_mapping = mappings.get("work_package_mapping", {})
    op_work_packages = {}

    # Build jira_id to op_id mapping
    jira_id_to_op_id = {}
    for jira_id, entry in wp_mapping.items():
        if "openproject_id" in entry:
            jira_id_to_op_id[jira_id] = entry["openproject_id"]

    # Fetch work packages from OP
    for issue in jira_issues:
        jira_id = str(issue["jira_id"])
        jira_key = issue["jira_key"]

        if jira_id in jira_id_to_op_id:
            op_id = jira_id_to_op_id[jira_id]
            try:
                wp = op_client.get_work_package(op_id)
                if wp:
                    op_work_packages[jira_key] = {
                        "op_id": op_id,
                        "subject": wp.get("subject"),
                        "priority": wp.get("_links", {}).get("priority", {}).get("title"),
                        "assignee": wp.get("_links", {}).get("assignee", {}).get("title"),
                        "author": wp.get("_links", {}).get("author", {}).get("title"),
                        "status": wp.get("_links", {}).get("status", {}).get("title"),
                        "type": wp.get("_links", {}).get("type", {}).get("title"),
                        "created_at": wp.get("createdAt"),
                        "updated_at": wp.get("updatedAt"),
                        "project": wp.get("_links", {}).get("project", {}).get("title"),
                        "description": (wp.get("description", {}) or {}).get("raw", "")[:200] if wp.get("description") else None,
                    }
            except Exception as e:
                print(f"  Error fetching WP {op_id} for {jira_key}: {e}")

    print(f"Fetched {len(op_work_packages)} work packages from OpenProject")
    return op_work_packages


def analyze_differences(jira_issues: list[dict], op_work_packages: dict[str, dict], mappings: dict) -> dict:
    """Analyze differences between Jira issues and OP work packages."""
    print("\nAnalyzing differences...")

    user_mapping = mappings.get("user_mapping", {})
    priority_mapping = mappings.get("priority_mapping", {})

    analysis = {
        "total_sampled": len(jira_issues),
        "matched_in_op": 0,
        "missing_in_op": 0,
        "field_analysis": {
            "priority": {"matches": 0, "mismatches": 0, "missing": 0, "examples": []},
            "assignee": {"matches": 0, "mismatches": 0, "missing": 0, "examples": []},
            "reporter": {"matches": 0, "mismatches": 0, "missing": 0, "examples": []},
            "timestamps": {"matches": 0, "mismatches": 0, "examples": []},
            "status": {"matches": 0, "mismatches": 0, "examples": []},
            "type": {"matches": 0, "mismatches": 0, "examples": []},
        },
        "detailed_issues": []
    }

    for issue in jira_issues:
        jira_key = issue["jira_key"]
        jira_fields = issue["fields"]

        if jira_key not in op_work_packages:
            analysis["missing_in_op"] += 1
            continue

        analysis["matched_in_op"] += 1
        op_wp = op_work_packages[jira_key]

        issue_details = {
            "jira_key": jira_key,
            "differences": []
        }

        # Priority analysis
        jira_priority = jira_fields.get("priority")
        op_priority = op_wp.get("priority")
        if jira_priority and op_priority:
            if jira_priority.lower() == op_priority.lower():
                analysis["field_analysis"]["priority"]["matches"] += 1
            else:
                analysis["field_analysis"]["priority"]["mismatches"] += 1
                issue_details["differences"].append({
                    "field": "priority",
                    "jira": jira_priority,
                    "op": op_priority
                })
                if len(analysis["field_analysis"]["priority"]["examples"]) < 5:
                    analysis["field_analysis"]["priority"]["examples"].append({
                        "key": jira_key,
                        "jira": jira_priority,
                        "op": op_priority
                    })
        elif jira_priority and not op_priority:
            analysis["field_analysis"]["priority"]["missing"] += 1

        # Assignee analysis
        jira_assignee = jira_fields.get("assignee")
        op_assignee = op_wp.get("assignee")
        if jira_assignee:
            # Look up expected OP user
            user_entry = user_mapping.get(jira_assignee, {})
            expected_op_login = user_entry.get("openproject_login", "")

            if op_assignee:
                if expected_op_login and expected_op_login.lower() in op_assignee.lower():
                    analysis["field_analysis"]["assignee"]["matches"] += 1
                else:
                    analysis["field_analysis"]["assignee"]["mismatches"] += 1
                    issue_details["differences"].append({
                        "field": "assignee",
                        "jira": jira_assignee,
                        "expected_op": expected_op_login,
                        "actual_op": op_assignee
                    })
            else:
                analysis["field_analysis"]["assignee"]["missing"] += 1
                if len(analysis["field_analysis"]["assignee"]["examples"]) < 5:
                    analysis["field_analysis"]["assignee"]["examples"].append({
                        "key": jira_key,
                        "jira": jira_assignee,
                        "op": "NONE"
                    })

        # Reporter/Author analysis
        jira_reporter = jira_fields.get("reporter")
        op_author = op_wp.get("author")
        if jira_reporter:
            user_entry = user_mapping.get(jira_reporter, {})
            expected_op_login = user_entry.get("openproject_login", "")

            if op_author:
                if expected_op_login and expected_op_login.lower() in op_author.lower():
                    analysis["field_analysis"]["reporter"]["matches"] += 1
                elif "system" in op_author.lower() or "admin" in op_author.lower():
                    analysis["field_analysis"]["reporter"]["mismatches"] += 1
                    issue_details["differences"].append({
                        "field": "reporter/author",
                        "jira": jira_reporter,
                        "expected_op": expected_op_login,
                        "actual_op": op_author
                    })
                    if len(analysis["field_analysis"]["reporter"]["examples"]) < 5:
                        analysis["field_analysis"]["reporter"]["examples"].append({
                            "key": jira_key,
                            "jira": jira_reporter,
                            "op": op_author
                        })
                else:
                    analysis["field_analysis"]["reporter"]["matches"] += 1
            else:
                analysis["field_analysis"]["reporter"]["missing"] += 1

        # Timestamps analysis
        jira_created = jira_fields.get("created", "")
        op_created = op_wp.get("created_at", "")
        if jira_created and op_created:
            # Check if same day at least
            jira_date = jira_created[:10]
            op_date = op_created[:10]
            if jira_date == op_date:
                analysis["field_analysis"]["timestamps"]["matches"] += 1
            else:
                analysis["field_analysis"]["timestamps"]["mismatches"] += 1
                issue_details["differences"].append({
                    "field": "created_at",
                    "jira": jira_created,
                    "op": op_created
                })
                if len(analysis["field_analysis"]["timestamps"]["examples"]) < 5:
                    analysis["field_analysis"]["timestamps"]["examples"].append({
                        "key": jira_key,
                        "jira_created": jira_created,
                        "op_created": op_created
                    })

        if issue_details["differences"]:
            analysis["detailed_issues"].append(issue_details)

    return analysis


def generate_report(analysis: dict) -> str:
    """Generate a human-readable report."""
    report = []
    report.append("=" * 80)
    report.append("MIGRATION QUALITY ANALYSIS REPORT")
    report.append("=" * 80)
    report.append(f"\nGenerated: {datetime.now().isoformat()}")
    report.append(f"\nSample Size: {analysis['total_sampled']}")
    report.append(f"Matched in OP: {analysis['matched_in_op']}")
    report.append(f"Missing in OP: {analysis['missing_in_op']}")

    report.append("\n" + "-" * 80)
    report.append("FIELD-BY-FIELD ANALYSIS")
    report.append("-" * 80)

    for field, stats in analysis["field_analysis"].items():
        report.append(f"\n{field.upper()}:")
        report.append(f"  Matches: {stats['matches']}")
        report.append(f"  Mismatches: {stats['mismatches']}")
        if "missing" in stats:
            report.append(f"  Missing in OP: {stats['missing']}")
        if stats["examples"]:
            report.append("  Examples of issues:")
            for ex in stats["examples"][:3]:
                report.append(f"    - {ex}")

    report.append("\n" + "-" * 80)
    report.append("ROOT CAUSES IDENTIFIED")
    report.append("-" * 80)

    report.append("""
1. SKELETON MIGRATION USES DEFAULT VALUES
   - work_package_skeleton_migration.py lines 391-392:
     priority_id: self._get_default_priority_id()  # Always "Normal"
     author_id: self._get_default_author_id()      # Always admin (ID 1)
   - Line 216: Only fetches minimal fields, NOT priority/assignee/reporter

2. CONTENT MIGRATION DOESN'T UPDATE CORE METADATA
   - work_package_content_migration.py has ZERO references to:
     priority, assignee, author, created_at, updated_at
   - These fields are NEVER migrated from Jira

3. MISSING PROJECTS
   - 8 Jira projects had no OP mapping: BLUG, KRQY, LOOE, MFAG, NEXC, PLASL, PP, STB
   - Their issues (~4323) were skipped entirely
""")

    return "\n".join(report)


def main():
    """Main entry point."""
    print("Migration Quality Analysis")
    print("=" * 50)

    # Initialize clients
    jira_client = JiraClient()
    op_client = OpenProjectClient()

    # Load mappings
    mappings = load_mappings()
    print(f"Loaded mappings: {list(mappings.keys())}")

    # Sample Jira issues
    jira_issues = sample_jira_issues(jira_client, sample_size=100)

    # Get corresponding OP work packages
    op_work_packages = get_op_work_packages(op_client, jira_issues, mappings)

    # Analyze differences
    analysis = analyze_differences(jira_issues, op_work_packages, mappings)

    # Save analysis
    output_dir = Path("var/reports")
    output_dir.mkdir(parents=True, exist_ok=True)

    analysis_file = output_dir / f"migration_quality_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(analysis_file, "w") as f:
        json.dump(analysis, f, indent=2, default=str)
    print(f"\nAnalysis saved to: {analysis_file}")

    # Generate report
    report = generate_report(analysis)
    report_file = output_dir / f"migration_quality_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(report_file, "w") as f:
        f.write(report)
    print(f"Report saved to: {report_file}")

    # Print report
    print("\n" + report)

    return analysis


if __name__ == "__main__":
    main()
