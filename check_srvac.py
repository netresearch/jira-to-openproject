#!/usr/bin/env python3
"""Check if SRVAC project exists in Jira."""

from dotenv import load_dotenv
from src.clients.jira_client import JiraClient

load_dotenv()

print("Connecting to Jira...")
jira = JiraClient()

print(f"Searching for SRVAC project...")
projects = jira.get_projects()

srvac = [p for p in projects if p['key'] == 'SRVAC']

if srvac:
    project = srvac[0]
    print(f"\n✓ Found SRVAC project:")
    print(f"  Key: {project['key']}")
    print(f"  Name: {project['name']}")
    print(f"  ID: {project.get('id', 'N/A')}")

    # Try to get issue count
    try:
        issues = jira.search_issues(f'project = SRVAC', max_results=1)
        total = issues.get('total', 0)
        print(f"  Issues: {total}")
    except Exception as e:
        print(f"  Issues: Could not retrieve ({e})")
else:
    print(f"\n✗ SRVAC project not found in Jira")
    print(f"\nAvailable projects containing 'SRV':")
    srv_projects = [p for p in projects if 'SRV' in p['key'].upper()]
    for p in srv_projects[:10]:
        print(f"  - {p['key']}: {p['name']}")
