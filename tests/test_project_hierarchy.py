"""
Test script for project hierarchy implementation.
This verifies the proper organization of projects under their parent Tempo companies.
"""

import os
import sys
import json
from typing import Dict, List, Any, Optional

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src import config
from src.migrations.project_migration import ProjectMigration
from src.migrations.company_migration import CompanyMigration

# Set up logging
logger = config.logger


def analyze_project_hierarchy() -> Dict[str, Any]:
    """
    Analyze the project hierarchy to verify proper parent-child relationships.

    Returns:
        Dictionary with analysis results
    """
    logger.info("Analyzing project hierarchy...")

    # Initialize clients
    jira_client = JiraClient()
    op_client = OpenProjectClient()

    # Set up migration classes for analysis
    project_migration = ProjectMigration(jira_client, op_client)
    company_migration = CompanyMigration(jira_client, op_client)

    # Load existing mapping data
    project_mapping = project_migration._load_from_json("project_mapping.json") or {}
    company_mapping = company_migration._load_from_json("company_mapping.json") or {}

    # Get all OpenProject projects
    op_projects = op_client.get_projects()

    # Count projects with parent relationships
    projects_with_parent = []
    projects_without_parent = []

    for project in op_projects:
        project_id = project.get("id")
        project_name = project.get("name", "Unknown")
        parent_link = project.get("_links", {}).get("parent", {}).get("href")

        is_company = False
        for company in company_mapping.values():
            if company.get("openproject_id") == project_id:
                is_company = True
                break

        # Skip company projects
        if is_company:
            continue

        if parent_link:
            parent_id = parent_link.split("/")[-1]
            parent_project = next((p for p in op_projects if str(p.get("id")) == parent_id), None)
            parent_name = parent_project.get("name", "Unknown") if parent_project else "Unknown"

            projects_with_parent.append({
                "id": project_id,
                "name": project_name,
                "parent_id": parent_id,
                "parent_name": parent_name
            })
        else:
            projects_without_parent.append({
                "id": project_id,
                "name": project_name
            })

    # Calculate analysis metrics
    total_projects = len(projects_with_parent) + len(projects_without_parent)
    hierarchy_percentage = (len(projects_with_parent) / total_projects * 100) if total_projects > 0 else 0

    # Find projects that should have a parent but don't
    expected_parent_mapping = {}
    for key, project in project_mapping.items():
        if project.get("parent_id"):
            expected_parent_mapping[project.get("openproject_id")] = project

    missing_parent_relations = []
    for project in projects_without_parent:
        if project["id"] in expected_parent_mapping:
            expected = expected_parent_mapping[project["id"]]
            missing_parent_relations.append({
                "id": project["id"],
                "name": project["name"],
                "expected_parent_id": expected.get("parent_id"),
                "expected_parent_name": expected.get("parent_name")
            })

    # Prepare analysis results
    analysis = {
        "total_projects": total_projects,
        "projects_with_parent": len(projects_with_parent),
        "projects_without_parent": len(projects_without_parent),
        "hierarchy_percentage": hierarchy_percentage,
        "missing_parent_relations": len(missing_parent_relations),
        "missing_parent_details": missing_parent_relations
    }

    # Save analysis to file
    project_migration._save_to_json(analysis, "project_hierarchy_analysis.json")

    # Log summary
    logger.info(f"Project hierarchy analysis:")
    logger.info(f"Total projects: {total_projects}")
    logger.info(f"Projects with parent: {len(projects_with_parent)} ({hierarchy_percentage:.1f}%)")
    logger.info(f"Projects without parent: {len(projects_without_parent)}")
    logger.info(f"Missing parent relations: {len(missing_parent_relations)}")

    return analysis


def run_hierarchy_test():
    """Run the project hierarchy test"""
    logger.info("Running project hierarchy test...")
    analysis = analyze_project_hierarchy()

    # Determine if test passed
    if analysis["missing_parent_relations"] == 0:
        logger.success("✅ Project hierarchy test passed! All expected parent-child relationships are in place.")
    else:
        logger.error(f"❌ Project hierarchy test failed! {analysis['missing_parent_relations']} projects are missing their parent relationship.")
        logger.info("Review project_hierarchy_analysis.json for details on missing relationships.")

    return analysis


if __name__ == "__main__":
    run_hierarchy_test()
