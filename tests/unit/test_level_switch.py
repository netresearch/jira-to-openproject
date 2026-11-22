# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The level path is on by default once an inventory is configured; assignees must be readers (ADR-012)."""

from pathlib import Path

import pytest
import yaml

from src.application.components import level_issues
from src.application.components.level_access import Audience
from src.application.components.registry import DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES
from tests.unit import test_level_work_packages as wp
from tests.unit.test_level_work_packages import LEVELS, UNRESTRICTED, issue
from tests.unit.test_level_work_packages import level_issues as level_search
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    return level_work_package_setup


def test_the_shipped_configuration_switches_the_level_path_on():
    migration = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())["migration"]
    assert migration["security_level_work_packages"] is True
    assert "security_level_report" not in migration


@pytest.mark.parametrize("sequence", [DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]])
def test_the_full_profile_creates_the_sub_projects_and_grants_access(sequence):
    assert sequence.index("projects") < sequence.index("security_level_projects")
    assert sequence.index("security_level_projects") < sequence.index("work_packages_skeleton")
    assert sequence.index("admin_schemes") < sequence.index("security_level_access")


def test_without_a_configured_report_the_switch_leaves_level_issues_out(level_setup):
    import src.config as cfg

    def setup(**kw):
        result = level_setup(**kw)
        del cfg.migration_config["security_level_report"]
        return result

    outcome = wp.skeleton(setup, reports=False, unrestricted=[issue(1)], levels=level_search())
    assert cfg.migration_config["security_level_work_packages"] is True
    assert level_issues.enabled() is False
    assert outcome.result.success, outcome.result.error
    assert wp.searches(outcome.jira) == [UNRESTRICTED, UNRESTRICTED]
    assert outcome.created == {("Issue 1", 7)}


def _with_assignees(level_setup, monkeypatch, *, readers_file):
    """Run the skeleton with an assignee per issue: APP-10 a reader (41), APP-11 not (99)."""
    import src.config as cfg

    level_setup()
    if readers_file:
        audience = Audience(members={"APP:10000": frozenset({41, 42})})
        monkeypatch.setattr(level_issues, "staging_audience", lambda mappings, report, edition: audience)
        cfg.migration_config["security_level_readers"] = "readers.json"
    assignees = {"APP-1": 99, "APP-10": 41, "APP-11": 99}
    jira, op = wp.fake_jira([issue(1)], level_search()), wp.FakeOpenProject()
    migration = wp.WorkPackageSkeletonMigration(jira_client=jira, op_client=op)
    migration._get_j2o_origin_key_cf_id = lambda: 5
    migration._build_skeleton_payload = lambda source, project_id, _cf: {
        "subject": source.fields.summary,
        "project_id": project_id,
        "_jira_key": source.key,
        "_jira_id": str(source.id),
        "assigned_to_id": assignees[source.key],
    }
    result = migration.run()
    assert wp.searches(jira) == [UNRESTRICTED, UNRESTRICTED, LEVELS, LEVELS]
    # The batch writer strips the Jira key; the subject names the issue.
    return result, {p["subject"].replace("Issue ", "APP-"): p.get("assigned_to_id") for p in op.payloads}


def test_an_assignee_who_is_no_reader_is_not_assigned(level_setup, monkeypatch):
    result, assigned = _with_assignees(level_setup, monkeypatch, readers_file=True)
    assert result.success, result.error
    # The ordinary work package keeps its assignee; the level ones only a reader.
    assert assigned == {"APP-1": 99, "APP-10": 41, "APP-11": None}
    assert result.details["security_level"]["assignees_not_readers"] == 1


def test_without_a_readers_file_no_level_work_package_gets_an_assignee(level_setup, monkeypatch):
    # Before the grant the sub-project has no member, so no assignee is known to be a reader.
    result, assigned = _with_assignees(level_setup, monkeypatch, readers_file=False)
    assert result.success, result.error
    assert assigned == {"APP-1": 99, "APP-10": None, "APP-11": None}
    assert result.details["security_level"]["assignees_not_readers"] == 2
