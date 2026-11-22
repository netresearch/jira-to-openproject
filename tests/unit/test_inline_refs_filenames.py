# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The inline reference rewrite matches attachment file names literally."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.components.inline_refs_migration import InlineRefsMigration

HARNESS = r"""
class FakeAttachments
  def initialize(names) = @names = names
  def pluck(_) = @names
end
class FakeWorkPackage
  attr_accessor :description
  def initialize(description, names) = (@description, @names = description, names)
  def attachments = FakeAttachments.new(@names)
  def save! = true
end
class FakeJournals
  def where(*) = self
  def not(*) = self
  def find_each = nil
end
class Journal
  def self.where(*) = FakeJournals.new
end
WPS = {1 => FakeWorkPackage.new(ARGV[0], JSON.parse(ARGV[1]))}
class WorkPackage
  def self.find(id) = WPS.fetch(id)
end
input_data = [{"id" => 1, "expect" => nil}]
"""


def _rewrite(description: str, names: list[str]) -> tuple[dict, str]:
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required to exercise the generated rewrite script")
    op = MagicMock()
    op.execute_script_with_data.return_value = {"status": "success", "data": {"updated": 0, "failed": 0, "digests": {}}}
    InlineRefsMigration(jira_client=MagicMock(), op_client=op)._load(SimpleNamespace(data={"work_package_ids": [1]}))
    script = "require 'json'\n" + HARNESS + op.execute_script_with_data.call_args.args[0]
    script += 'puts "J2O_DESCRIPTION" + WPS[1].description\n'
    out = subprocess.run(
        [ruby, "-e", script, description, json.dumps(names)], capture_output=True, text=True, check=True, timeout=60
    ).stdout
    counts = json.loads(out.split("JSON_OUTPUT_START", 1)[1].split("JSON_OUTPUT_END", 1)[0])
    return counts, out.split("J2O_DESCRIPTION", 1)[1].strip()


def test_a_file_name_with_dots_is_rewritten():
    counts, text = _rewrite(
        "See ![x](https://jira.example/secure/attachment/9/report.final.pdf).", ["report.final.pdf"]
    )
    assert counts == {"updated": 1, "failed": 0, "digests": {}}
    assert text == "See ![x](attachment:report.final.pdf)."


def test_a_dot_in_the_name_matches_only_a_dot():
    counts, text = _rewrite("(https://jira.example/a/reportXfinal.pdf)", ["report.final.pdf"])
    assert counts == {"updated": 0, "failed": 0, "digests": {}}
    assert text == "(https://jira.example/a/reportXfinal.pdf)"


def test_regex_characters_in_a_name_match_literally():
    counts, text = _rewrite("Plan: (https://jira.example/a/plan+v2[1].png) end", ["plan+v2[1].png"])
    assert counts == {"updated": 1, "failed": 0, "digests": {}}
    assert text == "Plan: (attachment:plan+v2[1].png) end"
