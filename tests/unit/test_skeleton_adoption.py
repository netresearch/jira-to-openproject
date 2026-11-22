# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""A skeleton rerun reuses the work packages a project already holds for its issues (#482)."""

from __future__ import annotations

import json

import pytest

from tests.unit.test_level_work_packages import issue, skeleton
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)


@pytest.fixture
def plain(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    return level_work_package_setup


def _mapping(outcome):
    return outcome.mappings.data.get("work_package") or {}


def test_a_rerun_after_a_lost_mapping_reuses_the_work_package(plain):
    # The case of #482: work_package_mapping.json is gone, OpenProject still holds APP-1 as 700.
    outcome = skeleton(
        plain,
        on=False,
        reports=False,
        unrestricted=[issue(1), issue(2)],
        marked=[[700, "1", "APP-1"]],
    )
    assert outcome.result.success, outcome.result.error
    assert outcome.created == {("Issue 2", 7)}
    assert outcome.result.details["total_adopted"] == 1
    assert outcome.on_disk["1"] == {"jira_key": "APP-1", "openproject_id": 700, "project_key": "APP"}


def test_a_work_package_without_an_origin_id_matches_by_its_origin_key(plain):
    outcome = skeleton(plain, on=False, reports=False, unrestricted=[issue(1)], marked=[[700, None, "APP-1"]])
    assert outcome.created == set()
    assert outcome.result.details["total_adopted"] == 1


def test_an_origin_id_of_another_issue_outweighs_the_same_key(plain):
    outcome = skeleton(plain, on=False, reports=False, unrestricted=[issue(1)], marked=[[700, "99", "APP-1"]])
    assert outcome.created == {("Issue 1", 7)}
    assert outcome.result.details["total_adopted"] == 0


@pytest.mark.parametrize(
    ("marked", "mapping"),
    [
        ([[700, "1", "APP-1"], [701, None, "APP-1"]], {}),
        ([[700, "1", "APP-1"]], {"work_package": {"5": {"jira_key": "APP-5", "openproject_id": 700}}}),
    ],
    ids=["two-work-packages-claim-the-issue", "the-work-package-is-mapped-to-another-issue"],
)
def test_an_unclear_claim_is_neither_adopted_nor_created(plain, monkeypatch, marked, mapping):
    from src.application.components import work_package_skeleton_migration as module

    original = module.WorkPackageSkeletonMigration._load_existing_mapping

    def preload(self):
        original(self)
        self.work_package_mapping.update(json.loads(json.dumps(mapping.get("work_package", {}))))

    monkeypatch.setattr(module.WorkPackageSkeletonMigration, "_load_existing_mapping", preload)
    outcome = skeleton(plain, on=False, reports=False, unrestricted=[issue(1)], marked=marked)
    assert outcome.created == set()
    assert outcome.result.details["total_failed"] == 1
    assert "1" not in _mapping(outcome)


@pytest.mark.parametrize("answer", [{"rows": []}, [[700, 1, "APP-1"]], [["700", "1", "APP-1"]], [[700, "1"]]])
def test_an_unreadable_answer_creates_nothing_in_the_project(plain, answer):
    outcome = skeleton(plain, on=False, reports=False, unrestricted=[issue(1), issue(2)], marked=answer)
    assert outcome.created == set()
    assert outcome.result.details["total_failed"] == 2
