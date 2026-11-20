# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Select real Jira issues for the local comment-permission acceptance test.

The script only sends GET requests. It writes the private fixture outside the checkout,
because the fixture holds copies of real comments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

CASES = ("ordinary", "group", "role", "security")
CHECKOUT = Path(__file__).resolve().parents[2]


class ReadOnlyJira:
    """Bearer-authenticated GET client for one Jira origin."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, **params: Any) -> Any:
        response = self.session.get(self.url + path, params=params, timeout=(10, 180), allow_redirects=False)
        response.raise_for_status()
        return response.json()


def comments(jira: ReadOnlyJira, key: str) -> list[dict]:
    """Read every comment of one issue with its visibility."""
    rows: list[dict] = []
    while True:
        page = jira.get(f"/rest/api/2/issue/{key}/comment", startAt=len(rows), maxResults=100)
        rows.extend(page["comments"])
        if not page["comments"] or len(rows) >= page["total"]:
            return rows


def audience(comment: dict) -> str:
    """Name the restriction kind of one comment."""
    visibility = comment.get("visibility")
    return visibility["type"] if visibility else "public"


def classify(issue: dict, rows: list[dict]) -> str | None:
    """Return the acceptance case an issue can represent, if any."""
    if not rows:
        return None
    if issue["fields"].get("security"):
        return "security"
    kinds = {audience(row) for row in rows}
    if kinds == {"public"}:
        return "ordinary"
    for kind in ("role", "group"):
        if kind in kinds and kinds <= {kind, "public"}:
            return kind
    return None


def rank(rows: list[dict]) -> tuple[bool, int]:
    """Prefer issues where both directions are visible, then the fewest copied comments."""
    return ("public" not in {audience(row) for row in rows}, len(rows))


def scan(jira: ReadOnlyJira, projects: list[str]) -> tuple[int, dict[str, tuple]]:
    """Pick the smallest issue per case across the given projects."""
    best: dict[str, tuple] = {}
    scanned = 0
    for project in projects:
        start = 0
        while True:
            page = jira.get(
                "/rest/api/2/search",
                jql=f'project = "{project}" ORDER BY key ASC',
                fields="comment,security",
                startAt=start,
                maxResults=100,
            )
            for issue in page["issues"]:
                embedded = issue["fields"]["comment"]
                rows = embedded["comments"]
                if embedded["total"] > len(rows):
                    rows = comments(jira, issue["key"])
                case = classify(issue, rows)
                if case and (case not in best or (rank(rows), issue["key"]) < best[case][0]):
                    best[case] = ((rank(rows), issue["key"]), issue["key"])
            start += len(page["issues"])
            scanned += len(page["issues"])
            if not page["issues"] or start >= page["total"]:
                break
    return scanned, best


def describe(jira: ReadOnlyJira, key: str) -> dict:
    """Collect the fields needed to recreate the issue and its comments."""
    issue = jira.get(f"/rest/api/2/issue/{key}", fields="summary,issuetype,security,project")
    fields = issue["fields"]
    return {
        "key": issue["key"],
        "id": issue["id"],
        "project": fields["project"]["key"],
        "summary": fields["summary"],
        "issuetype": fields["issuetype"]["name"],
        "security": (fields.get("security") or {}).get("name"),
        "comments": [
            {
                "id": row["id"],
                "body": row["body"],
                "author": (row.get("author") or {}).get("displayName"),
                "created": row.get("created"),
                "visibility": row.get("visibility"),
            }
            for row in comments(jira, key)
        ],
    }


def private_output(path: Path) -> Path:
    """Refuse destinations inside the checkout and create the file owner-only."""
    path = path.absolute()
    if path.resolve().is_relative_to(CHECKOUT):
        msg = "Write the company fixture outside the checkout"
        raise SystemExit(msg)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. The bearer token comes from J2O_SAMPLE_JIRA_TOKEN."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jira", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclude-prefix", action="append", default=[], help="Skip project keys with this prefix")
    parser.add_argument("--exclude-project", action="append", default=[])
    parser.add_argument("--project", action="append", default=[], help="Scan only these projects")
    parser.add_argument("--issue", action="append", default=[], help="Pin a case, e.g. role=ABC-12")
    args = parser.parse_args(argv)
    output = private_output(args.output)
    jira = ReadOnlyJira(args.jira, os.environ["J2O_SAMPLE_JIRA_TOKEN"])
    projects = args.project or sorted(row["key"] for row in jira.get("/rest/api/2/project"))
    projects = [
        key
        for key in projects
        if key not in args.exclude_project and not any(key.startswith(prefix) for prefix in args.exclude_prefix)
    ]
    pinned = dict(item.split("=", 1) for item in args.issue)
    scanned, best = scan(jira, projects) if set(CASES) - set(pinned) else (0, {})
    selected = {case: pinned.get(case) or best.get(case, (None, None))[1] for case in CASES}
    missing = [case for case, key in selected.items() if not key]
    if missing:
        sys.stderr.write(f"No real issue found for: {', '.join(missing)}\n")
        return 1
    fixture = {
        "format": "j2o-company-comment-sample/1",
        "source": jira.url,
        "selected_at": datetime.now(UTC).isoformat(),
        "scanned_projects": len(projects),
        "scanned_issues": scanned,
        "cases": {case: describe(jira, key) for case, key in selected.items()},
    }
    for case, data in fixture["cases"].items():
        if classify({"fields": {"security": data["security"]}}, data["comments"]) != case:
            sys.stderr.write(f"Pinned issue does not represent the {case} case\n")
            return 1
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(fixture, stream, ensure_ascii=False, indent=2)
    summary = {case: [data["project"], len(data["comments"])] for case, data in fixture["cases"].items()}
    sys.stdout.write(json.dumps({"scanned_issues": scanned, "cases": summary}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
