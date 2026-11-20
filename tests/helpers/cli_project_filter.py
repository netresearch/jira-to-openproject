# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Apply ``--jira-project-filter`` through the real argument parser and config update."""

import pytest

from src import config
from src.main import _argument_parser


def apply_cli_project_filter(monkeypatch: pytest.MonkeyPatch, keys: str) -> None:
    """Handle the arguments of ``j2o migrate --profile full --jira-project-filter <keys>``.

    The config sections the update writes are restored after the test.
    """
    monkeypatch.setattr(config, "_cli_args", {})
    monkeypatch.setitem(config._config_loader.config["jira"], "projects", [])
    monkeypatch.setitem(config.jira_config, "projects", [])
    monkeypatch.delitem(config.migration_config, "jira_project_filter", raising=False)
    args = _argument_parser().parse_args(["migrate", "--profile", "full", "--jira-project-filter", keys])
    config.update_from_cli_args(args)
