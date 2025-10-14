#!/usr/bin/env python3
"""Quick data QA summary for Jira → OpenProject migrations.

The script inspects cached migration artifacts (under ``var/data`` by default)
and reports basic per-project counts for Jira issues, migrated work packages,
and downloaded attachments. It is intended for post-run validation alongside the
standard migration logs.

Usage
-----

```
python scripts/data_qa.py --data-dir var/data --projects SRVAC OTHER
```

This relies on the JSON snapshots produced by the migration components
(``work_package_mapping.json``, ``jira_projects.json``) and the attachment
staging directory. If the files are missing, the script reports what it can and
continues.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from src import config as global_config


def load_json(path: Path) -> Any:
    """Safely load JSON if the path exists."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic output
        print(f"⚠️  Failed to parse JSON in {path}: {exc}")
    return None


def collect_projects(mapping: dict[str, Any]) -> dict[str, int]:
    """Return per-project counts from a work-package mapping."""
    counts: dict[str, int] = defaultdict(int)
    for jira_key, entry in mapping.items():
        key = entry.get("jira_key", jira_key) if isinstance(entry, dict) else jira_key
        if not isinstance(key, str) or "-" not in key:
            continue
        project_key = key.split("-", 1)[0].upper()
        counts[project_key] += 1
    return counts


def iter_projects(requested: Iterable[str] | None, discovered: Iterable[str]) -> list[str]:
    """Return the list of projects to report on."""
    discovered_norm = sorted({p.upper() for p in discovered})
    if not requested:
        return discovered_norm
    requested_norm = {p.upper() for p in requested}
    return [p for p in discovered_norm if p in requested_norm] or list(requested_norm)


def summarize(data_dir: Path, projects: list[str] | None) -> None:
    """Print QA summary for the requested projects."""

    wp_mapping = load_json(data_dir / "work_package_mapping.json") or {}
    jira_projects = load_json(data_dir / "jira_projects.json") or []
    project_mapping = load_json(data_dir / "project_mapping.json") or {}
    op_projects = load_json(data_dir / "openproject_projects.json") or []

    wp_counts = collect_projects(wp_mapping) if isinstance(wp_mapping, dict) else {}
    jira_counts = defaultdict(int)
    if isinstance(jira_projects, list):
        for project in jira_projects:
            key = project.get("key") if isinstance(project, dict) else None
            if key:
                jira_counts[key.upper()] += project.get("issueCount", 0) or 0

    modules_by_project_id: dict[int, list[str]] = {}
    if isinstance(op_projects, list):
        for entry in op_projects:
            try:
                pid = int(entry.get("id"))
            except Exception:
                continue
            modules = entry.get("enabled_modules") or []
            if isinstance(modules, list):
                modules_by_project_id[pid] = sorted(str(m) for m in modules)

    lead_info: dict[str, dict[str, Any]] = {}
    if isinstance(project_mapping, dict):
        for key, entry in project_mapping.items():
            if not isinstance(entry, dict):
                continue
            lead_info[key.upper()] = {
                "lead": entry.get("jira_lead"),
                "lead_display": entry.get("jira_lead_display"),
                "op_project_id": entry.get("openproject_id"),
            }

    start_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"with_start": 0, "total": 0})
    if isinstance(wp_mapping, dict):
        for record in wp_mapping.values():
            if not isinstance(record, dict):
                continue
            project_key = record.get("project_key") or record.get("jira_project_key")
            if not project_key:
                continue
            key_upper = str(project_key).upper()
            start_stats[key_upper]["total"] += 1
            if record.get("start_date"):
                start_stats[key_upper]["with_start"] += 1

    attachment_dir = data_dir / "attachments"
    attachment_total = sum(1 for _ in attachment_dir.glob("**/*") if _.is_file()) if attachment_dir.exists() else 0
    group_mapping = load_json(data_dir / "group_mapping.json") or {}
    total_groups = len(group_mapping) if isinstance(group_mapping, dict) else 0
    role_backed = (
        sum(1 for entry in group_mapping.values() if isinstance(entry, dict) and entry.get("role_backed"))
        if isinstance(group_mapping, dict)
        else 0
    )

    known_projects = wp_counts.keys() or jira_counts.keys() or lead_info.keys()
    projects_to_show = iter_projects(projects, known_projects)

    print(f"📁 Data directory: {data_dir}")
    print(f"📦 Work package mappings: {len(wp_counts)} project(s)")
    if attachment_total:
        print(f"📎 Local attachments staged: {attachment_total}")
    if total_groups:
        print(f"👥 Group mappings cached: {total_groups} (role-backed: {role_backed})")
    print()

    for project in projects_to_show:
        upper = project.upper()
        wp_total = wp_counts.get(project, 0)
        jira_total = jira_counts.get(project, 0)
        info = lead_info.get(upper, {})
        op_project_id_raw = info.get("op_project_id")
        try:
            op_project_id = int(op_project_id_raw) if op_project_id_raw else None
        except Exception:
            op_project_id = None
        modules_entry_found = bool(
            op_project_id is not None and op_project_id in modules_by_project_id
        )
        modules = modules_by_project_id.get(op_project_id, []) if modules_entry_found else []
        start_info = start_stats.get(upper, {"with_start": 0, "total": 0})
        project_warnings: list[str] = []

        print(f"Project {project}:")
        if jira_total:
            print(f"  • Jira issues (from cache): {jira_total}")
        else:
            print("  • Jira issues (from cache): <unknown>")
        print(f"  • Migrated work packages: {wp_total}")

        lead_display = info.get("lead_display") or info.get("lead")
        if lead_display:
            lead_login = info.get("lead")
            if lead_login and lead_login != lead_display:
                print(f"  • Jira lead: {lead_display} ({lead_login})")
            else:
                print(f"  • Jira lead: {lead_display}")
        elif info.get("lead"):
            print(f"  • Jira lead: {info['lead']}")

        if modules:
            print(f"  • Enabled modules: {', '.join(modules)}")
        elif op_project_id is not None:
            warning = (
                "OpenProject module snapshot missing; rerun 'uv run --active --no-cache "
                "python -m src.main migrate --components projects' to refresh caches."
            )
            if modules_entry_found:
                warning = (
                    "No enabled modules captured in snapshot; confirm project migration ran "
                    "and OpenProject modules are enabled."
                )
            project_warnings.append(warning)

        total_start = start_info["total"]
        if total_start:
            with_start = start_info["with_start"]
            pct = (with_start / total_start) * 100
            print(f"  • Work packages with start date: {with_start}/{total_start} ({pct:.1f}%)")
        else:
            print("  • Work packages with start date: n/a")
        if wp_total and start_info["with_start"] == 0:
            print(
                "  • Work packages lack start dates (likely no Jira transition ever entered an 'In Progress' category).",
            )

        if attachment_dir.exists():
            project_files = list(attachment_dir.glob(f"{project}-*"))
            if project_files:
                print(f"  • Attachment files staged: {len(project_files)}")

        for warning in project_warnings:
            print(f"  ⚠️ {warning}")

        print()

    checkpoint_path = data_dir.parent / ".migration_checkpoints.db"
    if checkpoint_path.exists():
        try:
            with sqlite3.connect(checkpoint_path) as conn:
                total = conn.execute("SELECT COUNT(*) FROM migration_checkpoints").fetchone()[0]
                completed = conn.execute(
                    "SELECT COUNT(*) FROM migration_checkpoints WHERE status='completed'",
                ).fetchone()[0]
                latest = conn.execute(
                    "SELECT MAX(updated_at) FROM migration_checkpoints WHERE updated_at IS NOT NULL",
                ).fetchone()[0]
            print("Checkpoint store summary:")
            print(f"  • Entries: {total} (completed: {completed})")
            if latest:
                print(f"  • Last update: {latest}")
            print()
        except sqlite3.DatabaseError as exc:
            print(f"⚠️  Failed to read checkpoint store {checkpoint_path}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarise migrated data counts from cached artefacts")
    parser.add_argument("--data-dir", type=Path, default=global_config.get_path("data"), help="Path to migration data cache")
    parser.add_argument("--projects", nargs="*", help="Specific Jira project keys to report on")
    args = parser.parse_args()

    summarize(args.data_dir, args.projects)


if __name__ == "__main__":
    main()
