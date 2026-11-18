#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migration Quality Analysis - Compare Jira issues with OpenProject work packages.

This script samples Jira issues and compares them with their migrated OP counterparts
to identify data quality issues and gaps in the migration.
"""

import json
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient


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
        "issue_type_mapping.json",
    ]

    for fname in mapping_files:
        fpath = data_dir / fname
        if fpath.exists():
            with open(fpath) as f:
                mappings[fname.replace(".json", "")] = json.load(f)

    return mappings


def _sample_record(issue) -> dict:
    fields = issue.fields
    values = {
        name: getattr(getattr(fields, name, None), "name", None)
        for name in ("priority", "assignee", "reporter", "status", "issuetype")
    }
    values.update(
        {name + "_id": getattr(getattr(fields, name, None), "id", None) for name in ("priority", "status", "issuetype")}
    )
    return {"jira_id": str(issue.id), "jira_key": issue.key, "fields": {**values, "created": fields.created}}


def sample_jira_issues(jira_client: JiraClient, sample_size: int = 100) -> list[dict]:
    """Build the sample from mapped Jira issues and keep the fields compared by the audit."""
    entries = load_mappings().get("work_package_mapping", {})
    keys = list(
        dict.fromkeys(entry.get("jira_key") or key for key, entry in entries.items() if isinstance(entry, dict))
    )
    selected = random.sample(keys, min(len(keys), sample_size))
    result = []
    for offset in range(0, len(selected), 50):
        batch = selected[offset : offset + 50]
        issues = jira_client.batch_get_issues(batch)
        missing = set(batch).difference(issues)
        if missing:
            msg = f"Jira did not return {len(missing)} sampled issues"
            raise ValueError(msg)
        result.extend(_sample_record(issues[key]) for key in batch)
    print(f"Sampled {len(result)} mapped Jira issues")
    return result


_TARGET_METRICS = """
packages = WorkPackage.where(id: input_data).includes(:priority, :status, :type, :author, :assigned_to)
rows = packages.map do |wp|
  { id: wp.id, priority: wp.priority&.name, priority_id: wp.priority_id,
    assignee: wp.assigned_to&.name, assignee_id: wp.assigned_to_id,
    author: wp.author&.name, author_id: wp.author_id, status: wp.status&.name, status_id: wp.status_id,
    type: wp.type&.name, type_id: wp.type_id, created_at: wp.created_at&.iso8601 }
end
puts $j2o_start_marker
puts JSON.generate(rows)
puts $j2o_end_marker
"""


def get_op_work_packages(op_client: OpenProjectClient, jira_issues: list[dict], mappings: dict) -> dict[str, dict]:
    lookup = mappings.get("work_package_mapping", {})
    keys_by_id = {}
    for issue in jira_issues:
        entry = lookup.get(str(issue["jira_id"])) or lookup.get(issue["jira_key"]) or {}
        if op_id := entry.get("openproject_id"):
            number = int(op_id)
            if number in keys_by_id and keys_by_id[number] != issue["jira_key"]:
                msg = f"Multiple sampled issues map to OpenProject work package {number}"
                raise ValueError(msg)
            keys_by_id[number] = issue["jira_key"]
    if not keys_by_id:
        return {}
    result = op_client.execute_script_with_data(_TARGET_METRICS, list(keys_by_id))
    if result.get("status") != "success" or not isinstance(result.get("data"), list):
        msg = "Could not retrieve the sampled OpenProject work packages"
        raise RuntimeError(msg)
    return {keys_by_id[row["id"]]: row for row in result["data"]}


def _matches_field(source, actual, mapping, target_id) -> bool:
    """Use the mapped OpenProject identity when present, otherwise compare exact names."""
    if isinstance(mapping, dict):
        expected_id = mapping.get("openproject_id")
        source = mapping.get("openproject_login") or mapping.get("openproject_name") or source
    else:
        expected_id = mapping
    if expected_id is not None and target_id is not None:
        return str(expected_id) == str(target_id)
    return str(source).casefold() == str(actual).casefold()


def _record_comparison(stats: dict, differences: list, *, key: str, field: str, source, target, matches: bool) -> None:
    if not source:
        return
    if target and matches:
        stats["matches"] += 1
        return
    stats["mismatches" if target else "missing"] += 1
    differences.append({"field": field, "jira": source, "op": target})
    if len(stats["examples"]) < 5:
        stats["examples"].append({"key": key, "jira": source, "op": target})


_FIELD_PAIRS = (
    ("priority", "priority", "priority", "priority_mapping"),
    ("assignee", "assignee", "assignee", "user_mapping"),
    ("reporter", "reporter", "author", "user_mapping"),
    ("status", "status", "status", "status_mapping"),
    ("type", "issuetype", "type", "issue_type_mapping"),
)


def _compare_sample(issue: dict, target: dict, mappings: dict, stats: dict) -> list:
    fields = issue["fields"]
    key = issue["jira_key"]
    differences = []
    for metric, source_field, target_field, map_name in _FIELD_PAIRS:
        source = fields.get(source_field)
        actual = target.get(target_field)
        lookup = mappings.get(map_name, {})
        mapping = lookup.get(str(fields.get(source_field + "_id"))) or lookup.get(source)
        matches = _matches_field(source, actual, mapping, target.get(target_field + "_id"))
        _record_comparison(
            stats[metric], differences, key=key, field=metric, source=source, target=actual, matches=matches
        )
    created = fields.get("created") or ""
    target_created = target.get("created_at") or ""
    _record_comparison(
        stats["timestamps"],
        differences,
        key=key,
        field="created_at",
        source=created,
        target=target_created,
        matches=created[:10] == target_created[:10],
    )
    return differences


def analyze_differences(jira_issues: list[dict], op_work_packages: dict[str, dict], mappings: dict) -> dict:
    """Compare mapped identities, field values and creation dates for the supplied sample."""
    stats = {
        name: {"matches": 0, "mismatches": 0, "missing": 0, "examples": []}
        for name in ("priority", "assignee", "reporter", "timestamps", "status", "type")
    }
    matched = 0
    details = []
    for issue in jira_issues:
        key = issue["jira_key"]
        if key not in op_work_packages:
            continue
        matched += 1
        differences = _compare_sample(issue, op_work_packages[key], mappings, stats)
        if differences:
            details.append({"jira_key": key, "differences": differences})
    return {
        "total_sampled": len(jira_issues),
        "matched_in_op": matched,
        "missing_in_op": len(jira_issues) - matched,
        "field_analysis": stats,
        "detailed_issues": details,
    }


def generate_report(analysis: dict) -> str:
    """Generate a human-readable report."""
    report = [
        "=" * 80,
        "MIGRATION QUALITY ANALYSIS REPORT",
        "=" * 80,
        f"\nGenerated: {datetime.now().isoformat()}",
        f"\nSample Size: {analysis['total_sampled']}",
        f"Matched in OP: {analysis['matched_in_op']}",
        f"Missing in OP: {analysis['missing_in_op']}",
        "\n" + "-" * 80,
        "FIELD-BY-FIELD ANALYSIS",
        "-" * 80,
    ]

    for field, stats in analysis["field_analysis"].items():
        report.extend(
            [
                f"\n{field.upper()}:",
                f"  Matches: {stats['matches']}",
                f"  Mismatches: {stats['mismatches']}",
            ]
        )
        if "missing" in stats:
            report.append(f"  Missing in OP: {stats['missing']}")
        if stats["examples"]:
            report.append("  Examples of issues:")
            for ex in stats["examples"][:3]:
                report.append(f"    - {ex}")

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
