#!/usr/bin/env python3
"""Run a minimal mock migration sequence for selected components."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import patch

from src import config
from src.migration import run_migration


class FixtureMappings:
    """Minimal mapping fixture for mock migrations."""

    def __init__(self) -> None:
        self.user_mapping = {
            "mock-user": {
                "jira_key": "mock-user",
                "jira_name": "Mock User",
                "jira_email": "mock.user@example.com",
                "jira_display_name": "Mock User",
                "openproject_id": 5001,
                "openproject_login": "mock.user",
                "openproject_email": "mock.user@example.com",
                "matched_by": "manual",
            },
        }
        self.project_mapping = {
            "E2E": {
                "jira_key": "E2E",
                "jira_name": "Mock Project",
                "openproject_id": 6001,
                "openproject_identifier": "mock-project",
                "openproject_name": "Mock Project",
            },
        }
        self.issue_type_mapping = {
            "Task": {
                "jira_id": "1",
                "jira_name": "Task",
                "openproject_id": 7001,
                "matched_by": "manual",
            },
        }
        self.issue_type_id_mapping = {"1": 7001}
        self.status_mapping = {
            "Open": {"openproject_id": 8001, "openproject_name": "Open"},
        }
        self.priority_mapping = {
            "Normal": {"openproject_id": 9002},
        }
        self.custom_field_mapping = {
            "E2E Custom Field": {"openproject_id": 9001, "field_format": "string"},
        }
        self.work_package_mapping = {
            "E2E-WP-1": {
                "jira_id": "10000",
                "jira_key": "E2E-WP-1",
                "project_key": "E2E",
                "openproject_id": 10001,
            },
        }

    def get_mapping(self, name: str):
        return getattr(self, f"{name}_mapping", {})

    def get_all_mappings(self):
        return {
            "user_mapping": self.user_mapping,
            "project_mapping": self.project_mapping,
            "issue_type_mapping": self.issue_type_mapping,
            "issue_type_id_mapping": self.issue_type_id_mapping,
            "status_mapping": self.status_mapping,
            "priority_mapping": self.priority_mapping,
            "custom_field_mapping": self.custom_field_mapping,
            "work_package_mapping": self.work_package_mapping,
        }

    def has_mapping(self, name: str) -> bool:
        return bool(self.get_mapping(name))


async def run_mock_migration(components: Iterable[str]) -> None:
    os.environ.setdefault("J2O_USE_MOCK_APIS", "true")

    attachment_dir = Path(config.migration_config.get("attachment_path") or (config.get_path("data") / "attachments"))
    attachment_dir.mkdir(parents=True, exist_ok=True)

    config.migration_config["no_backup"] = True
    config.migration_config["force"] = True
    config.migration_config["attachment_path"] = attachment_dir.as_posix()

    config.reset_mappings()
    config._mappings = FixtureMappings()  # type: ignore[attr-defined]

    with patch(
        "src.migrations.attachments_migration.AttachmentsMigration._download_attachment",
        lambda self, url, dest: dest.write_bytes(b"mock attachment content") or dest,
    ):
        result = await run_migration(
            components=list(components),
            no_confirm=True,
        )

    print("Overall status:", result.overall["status"])
    for name, component_result in result.components.items():
        print(name, "success" if component_result.success else "failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mock migration components with in-memory mappings")
    parser.add_argument("--components", nargs="*", default=["work_packages", "attachments", "attachment_provenance"], help="Components to execute")
    args = parser.parse_args()

    asyncio.run(run_mock_migration(args.components))


if __name__ == "__main__":
    main()
