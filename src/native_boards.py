# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Snapshot Jira board layouts as native basic boards in a private OpenProject archive project."""

import argparse
import hashlib
import json
import sys
from collections import defaultdict

from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.native_board_writer import save_layout
from src.infrastructure.openproject.openproject_client import OpenProjectClient


def layout(jira: JiraClient, board: dict, project: str) -> dict:
    """Assign each visible card to its Jira column in the source board's rank order."""
    board_id = int(board["id"])
    configuration = jira.get_board_configuration(board_id)
    columns = configuration["columnConfig"]["columns"]
    positions = {}
    for index, column in enumerate(columns):
        for status in column["statuses"]:
            if status["id"] in positions:
                msg = "A status occurs in multiple board columns"
                raise ValueError(msg)
            positions[status["id"]] = index
    cards: dict[int, list] = defaultdict(list)
    for issue in jira.agile.ranked_board_issues(board_id, project):
        position = positions[issue["fields"]["status"]["id"]]
        cards[position].append({"id": str(issue["id"]), "key": issue["key"]})
    data = {
        "jira": jira.base_url,
        "project": project,
        "board": board_id,
        "name": board["name"],
        "configuration": configuration,
        "cards": [cards[i] for i in range(len(columns))],
    }
    revision = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**data, "revision": revision}


def main(argv: list[str] | None = None) -> None:
    """Materialize project-scoped board snapshots with the configured migration clients."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jira-project", required=True, help="Source Jira project key")
    parser.add_argument("--target-project", required=True, help="Imported OpenProject project identifier")
    parser.add_argument("--archive-project", required=True, help="Private, memberless OpenProject board archive")
    args = parser.parse_args(argv)
    jira, target = JiraClient(), OpenProjectClient()
    for board in jira.get_boards_for_project(args.jira_project):
        data = layout(jira, board, args.jira_project)
        if data != layout(jira, board, args.jira_project):
            msg = "Board changed during capture"
            raise RuntimeError(msg)
        result = save_layout(target, data, args.target_project, args.archive_project)
        sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
