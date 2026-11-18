#!/usr/bin/env python
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Generate NRS description updates from the local work-package mapping."""

import json
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config
from src.infrastructure.jira.jira_client import JiraClient
from src.utils.markdown_converter import MarkdownConverter


def build_updates(jira_client: JiraClient, targets: dict[str, int]) -> list[dict]:
    """Read every page, including pages shorter than the requested size."""
    converter = MarkdownConverter(work_package_mapping=targets)
    issues = jira_client.stream_all_issues_for_project("NRS", fields="description,key", batch_size=100)
    updates = []
    for issue in issues:
        raw = getattr(issue, "raw", issue)
        key = raw["key"]
        description = raw["fields"].get("description")
        if key not in targets or not description:
            continue
        converted = converter.convert(description, jira_key=key)
        if converted:
            updates.append(
                {"jira_key": key, "wp_id": targets[key], "description": converted, "description_length": len(converted)}
            )
    return updates


def publish_updates(targets: dict[str, int], data_dir: Path) -> int:
    if not targets:
        print("No NRS work packages with a Jira key and OpenProject ID", file=sys.stderr)
        return 1
    output_file = data_dir / "nrs_description_updates.json"
    try:
        updates = build_updates(JiraClient(), targets)
        data_dir.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=data_dir, delete=False) as staged:
            staged_path = Path(staged.name)
            try:
                json.dump(updates, staged, indent=2)
                staged.close()
                staged_path.replace(output_file)
            finally:
                staged_path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"Failed to publish description updates to {output_file}: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {len(updates)} description updates to {output_file}")
    return 0


def main() -> int:
    data_dir = Path(config.get_path("data"))
    path = data_dir / "work_package_mapping.json"
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
        targets = {
            entry["jira_key"]: int(entry["openproject_id"])
            for entry in mapping.values()
            if entry.get("project_key") == "NRS" and entry.get("jira_key") and entry.get("openproject_id")
        }
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        print(f"Cannot read work-package mapping at {path}: {exc}", file=sys.stderr)
        return 1
    return publish_updates(targets, data_dir)


if __name__ == "__main__":
    sys.exit(main())
