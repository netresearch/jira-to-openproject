# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import os
import stat

import pytest

from scripts.operator_files import read_operator_text, write_operator_json, write_operator_text


def test_failed_export_preserves_previous_result(tmp_path):
    path = tmp_path / "data.json"
    write_operator_text(path, "previous")
    with pytest.raises(TypeError):
        write_operator_json(path, {"invalid": object()})
    assert read_operator_text(path) == "previous"
    assert list(tmp_path.iterdir()) == [path]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_selected_input_and_output_reject_redirects(tmp_path):
    original = tmp_path / "original"
    original.write_text("existing", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(original)
    with pytest.raises(ValueError):
        read_operator_text(alias)
    with pytest.raises(ValueError):
        write_operator_text(alias, "changed")
    assert original.read_text(encoding="utf-8") == "existing"


def test_incomplete_metadata_batch_leaves_saved_progress_intact(tmp_path, monkeypatch):
    from scripts import cache_jira_metadata

    cache = tmp_path / "metadata.json"
    write_operator_text(cache, '{"ABC-1": {"priority": "High"}}')
    write_operator_json(
        tmp_path / "work_package_mapping.json",
        {"1": {"jira_key": "ABC-1"}, "2": {"jira_key": "ABC-2"}},
    )
    monkeypatch.setattr(cache_jira_metadata, "JiraClient", object)
    monkeypatch.setattr(cache_jira_metadata, "fetch_metadata_batch", lambda client, keys: {})
    with pytest.raises(ValueError, match="incomplete"):
        cache_jira_metadata.cache_all_jira_metadata(tmp_path, cache)
    assert read_operator_text(cache) == '{"ABC-1": {"priority": "High"}}'
