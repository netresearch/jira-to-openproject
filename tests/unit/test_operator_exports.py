# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json
from types import SimpleNamespace

import pytest

from scripts import cache_jira_issues, get_complete_custom_fields


def test_field_export_rejects_truncated_history_and_preserves_commas():
    prefix = "J2O_TEST"
    field = {"name": "Gebühr, intern", "possible_values": ["Finance, Europe", "Ω"]}
    line = f"{prefix} {json.dumps(field)}"
    read = get_complete_custom_fields._read_fields
    assert read("old-token END 100\n" + line, prefix) is None
    assert read(line + f"\n{prefix} END 1", prefix) == [field]
    assert read(f"{prefix} END 0", prefix) == []
    with pytest.raises(ValueError, match="truncated"):
        read(line + f"\n{prefix} END 2", prefix)


def test_partial_jira_fetch_preserves_previous_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "var" / "data"
    data.mkdir(parents=True)
    (data / "work_package_mapping.json").write_text(json.dumps({"55": {"jira_key": "TEST-1"}, "TEST-2": {}}))
    cache = data / "jira_issues_cache.json"
    cache.write_text('{"previous":1}')
    partial = {"TEST-1": SimpleNamespace(raw={"key": "TEST-1"})}
    mock = SimpleNamespace(batch_get_issues=lambda requested: partial)
    monkeypatch.setattr(cache_jira_issues, "JiraClient", lambda: mock)
    assert cache_jira_issues.main() == 1
    assert json.loads(cache.read_text()) == {"previous": 1}
    partial["TEST-2"] = {"key": "TEST-2"}
    assert cache_jira_issues.main() == 0
    assert json.loads(cache.read_text()) == {key: {"key": key} for key in partial}


def test_json_export_preserves_destination_on_failure_and_rejects_link(tmp_path):
    from scripts.export_json_file import write_json_export

    target = tmp_path / "original.json"
    target.write_text("original")
    link = tmp_path / "export.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="regular export"):
        write_json_export(link, {})
    with pytest.raises(TypeError):
        write_json_export(target, object())
    assert target.read_text() == "original"
    assert set(tmp_path.iterdir()) == {link, target}
