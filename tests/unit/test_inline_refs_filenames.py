# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The inline reference rewrite points Markdown targets at the attachment's API path."""

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
  # The names arrive newest first, as the script orders them; IDs count from 1.
  def initialize(names) = @names = names
  def order(*) = self
  def pluck(*) = @names.each_with_index.map { |name, index| [index + 1, name] }
end
module API
  module V3
    module Utilities
      module PathHelper
        module ApiV3Path
          def self.attachment_content(id) = "/api/v3/attachments/#{id}/content"
        end
      end
    end
  end
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


def test_a_reference_an_earlier_run_wrote_is_repaired():
    # The form inline_refs wrote until now; OpenProject 17.9.7 renders it neither as image nor as link.
    counts, text = _rewrite(
        "Shot: ![x](attachment:screen.png) and [log](attachment:run.log)", ["screen.png", "run.log"]
    )
    assert counts == {"updated": 1, "failed": 0, "digests": {}}
    assert text == "Shot: ![x](/api/v3/attachments/1/content) and [log](/api/v3/attachments/2/content)"


def test_a_bare_name_and_a_jira_url_become_the_api_path():
    counts, text = _rewrite(
        "![](screen.png) [x](https://jira.example/secure/attachment/9/report.final.pdf)",
        ["screen.png", "report.final.pdf"],
    )
    assert counts["updated"] == 1
    assert text == "![](/api/v3/attachments/1/content) [x](/api/v3/attachments/2/content)"


def test_a_dot_in_the_name_matches_only_a_dot():
    counts, text = _rewrite("[x](https://jira.example/a/reportXfinal.pdf)", ["report.final.pdf"])
    assert counts == {"updated": 0, "failed": 0, "digests": {}}
    assert text == "[x](https://jira.example/a/reportXfinal.pdf)"


def test_regex_characters_in_a_name_match_literally():
    counts, text = _rewrite("Plan: [p](https://jira.example/a/plan+v2[1].png) end", ["plan+v2[1].png"])
    assert counts == {"updated": 1, "failed": 0, "digests": {}}
    assert text == "Plan: [p](/api/v3/attachments/1/content) end"


def test_a_name_in_prose_is_not_a_link_target():
    counts, text = _rewrite("Compare (screen.png) with the old one.", ["screen.png"])
    assert counts["updated"] == 0
    assert text == "Compare (screen.png) with the old one."


def test_the_newest_attachment_wins_regardless_of_case():
    # OpenProject resolves a name case-insensitively to the newest attachment; the first name is the newest.
    _counts, text = _rewrite("![x](SCREEN.png)", ["Screen.PNG", "screen.png"])
    assert text == "![x](/api/v3/attachments/1/content)"


def test_a_rewritten_text_stays_unchanged_on_the_next_run():
    _counts, once = _rewrite("![x](attachment:screen.png)", ["screen.png"])
    counts, twice = _rewrite(once, ["screen.png"])
    assert (counts["updated"], twice) == (0, once)


def test_an_api_path_stays_even_for_an_attachment_named_content():
    counts, text = _rewrite("![x](/api/v3/attachments/2/content)", ["content", "screen.png"])
    assert (counts["updated"], text) == (0, "![x](/api/v3/attachments/2/content)")


def test_a_name_matches_under_unicode_case_folding():
    # /i folds ß to SS; the lookup folds the same way instead of failing the work package.
    counts, text = _rewrite("![x](STRASSE.png)", ["straße.png"])
    assert counts == {"updated": 1, "failed": 0, "digests": {}}
    assert text == "![x](/api/v3/attachments/1/content)"


def test_a_folded_name_in_the_text_finds_the_attachment():
    counts, text = _rewrite("![x](straße.png)", ["STRASSE.png"])
    assert counts == {"updated": 1, "failed": 0, "digests": {}}
    assert text == "![x](/api/v3/attachments/1/content)"
