# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The importer accepts a readers file only from the inventory of its report (ADR-009, step 6)."""

import copy
import json

import pytest

from src.application.components.level_readers import load_readers
from src.application.components.security_level_projects_migration import LevelProjectError
from src.restricted_issues import READERS_FORMAT

pytestmark = pytest.mark.unit
SHA = "b" * 64
REPORT = {
    "archive_sha256": SHA,
    "levels": [
        {"project": "APP", "level_id": "10000", "static_readers": 2},
        {"project": "APP", "level_id": "10001", "static_readers": 0},
    ],
    "archived": [
        {"key": "APP-3", "level_id": "10000", "class": "per_issue", "extra_readers": 1},
        {"key": "APP-4", "level_id": "10000", "class": "unexplained", "extra_readers": 1},
    ],
}
READERS = {
    "format": READERS_FORMAT,
    "archive_sha256": SHA,
    "levels": [
        {"project": "APP", "level_id": "10000", "static_readers": ["reader.one", "reader.two"], "per_issue": {}},
        {"project": "APP", "level_id": "10001", "static_readers": [], "per_issue": {}},
    ],
}
READERS["levels"][0]["per_issue"] = {"APP-3": ["guest.reader"]}


@pytest.fixture
def readers_file(tmp_path, monkeypatch):
    def _write(value, mode=0o600):
        path = tmp_path / "readers.json"
        path.write_text(json.dumps(value))
        path.chmod(mode)
        monkeypatch.setattr("src.config.migration_config", {"security_level_readers": str(path)})
        return path

    return _write


def test_loads_static_and_extra_readers_of_the_report_inventory(readers_file):
    readers_file(READERS)
    readers = load_readers(REPORT)
    assert readers.static == {"APP:10000": {"reader.one", "reader.two"}, "APP:10001": frozenset()}
    assert readers.extra == {"APP-3": {"guest.reader"}}


def test_refuses_a_readers_file_other_users_can_read(readers_file):
    readers_file(READERS, mode=0o640)
    with pytest.raises(LevelProjectError, match="readable by other users"):
        load_readers(REPORT)


def test_refuses_a_symbolic_link_at_the_configured_path(readers_file, monkeypatch):
    target = readers_file(READERS)
    link = target.with_name("link.json")
    link.symlink_to(target)
    monkeypatch.setattr("src.config.migration_config", {"security_level_readers": str(link)})
    with pytest.raises(LevelProjectError, match="Cannot read the readers file"):
        load_readers(REPORT)


def test_refuses_a_missing_setting(monkeypatch):
    monkeypatch.setattr("src.config.migration_config", {})
    with pytest.raises(LevelProjectError, match="not configured"):
        load_readers(REPORT)


def _changed(change):
    value = copy.deepcopy(READERS)
    change(value)
    return value


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (_changed(lambda v: v.update(archive_sha256="c" * 64)), "another inventory"),
        (_changed(lambda v: v.update(format="j2o-restricted-report/2")), "Not a readers file"),
        (_changed(lambda v: v["levels"].pop()), "lacks levels"),
        (_changed(lambda v: v["levels"].append(dict(v["levels"][1]))), "not in the report"),
        (_changed(lambda v: v["levels"][1].update(level_id="10009")), "not in the report"),
        (_changed(lambda v: v["levels"][0]["static_readers"].pop()), "Static readers of APP:10000 differ"),
        (_changed(lambda v: v["levels"][0]["static_readers"].append("reader.one")), "Malformed readers"),
        (_changed(lambda v: v["levels"][0]["per_issue"].update({"APP-4": ["x"]})), "Extra readers of APP:10000"),
        (_changed(lambda v: v["levels"][0]["per_issue"].update({"APP-3": []})), "Extra readers of APP:10000"),
        (_changed(lambda v: v["levels"][0]["per_issue"].pop("APP-3")), "Extra readers of APP:10000"),
        (_changed(lambda v: v["levels"][1].update(per_issue=[])), "Malformed readers"),
    ],
)
def test_refuses_a_readers_file_that_does_not_match_the_report(readers_file, value, message):
    readers_file(value)
    with pytest.raises(LevelProjectError, match=message):
        load_readers(REPORT)


def test_refusals_never_name_a_reader(readers_file):
    readers_file(_changed(lambda v: v["levels"][0]["static_readers"].pop()))
    with pytest.raises(LevelProjectError) as caught:
        load_readers(REPORT)
    assert "reader." not in str(caught.value)
