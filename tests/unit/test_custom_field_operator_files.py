# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json
import sys
from pathlib import Path

import pytest

from scripts import convert_rails_to_json, create_custom_fields_json, get_custom_fields_simple, summarize_custom_fields


def test_operator_field_conversion_and_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = tmp_path / "captured.txt"
    exported = tmp_path / "nested" / "fields.json"
    captured.write_text("FIELDS_START\n7: Straße (string)\nFIELDS_END\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["create_custom_fields_json", str(captured), str(exported)])
    assert create_custom_fields_json.main() == 0
    assert json.loads(exported.read_text(encoding="utf-8")) == [{"id": 7, "name": "Straße", "field_format": "string"}]
    assert summarize_custom_fields.summarize_custom_fields(exported)["total_fields"] == 1
    captured.write_text('result = [{id: 8, name: "東京", field_format: "string"}]', encoding="utf-8")
    assert convert_rails_to_json.convert_ruby_to_json(str(captured), str(exported))
    assert json.loads(exported.read_text(encoding="utf-8"))[0]["name"] == "東京"
    assert exported.stat().st_mode & 0o077 == 0


def test_simple_export_contains_only_requested_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    exported = tmp_path / "fields.json"
    monkeypatch.setattr(sys, "argv", ["get_custom_fields_simple", str(exported)])
    field = {
        "id": 1,
        "name": "Account",
        "field_format": "string",
        "type": "WorkPackageCustomField",
        "is_required": False,
        "is_for_all": True,
    }
    monkeypatch.setattr(
        get_custom_fields_simple, "get_complete_custom_fields", lambda: [{**field, "default_value": "extra"}]
    )
    assert get_custom_fields_simple.main() == 0
    assert json.loads(exported.read_text(encoding="utf-8")) == [field]
    assert list(tmp_path.iterdir()) == [exported]


def test_ruby_hash_conversion_preserves_quoted_content_and_handles_hash_rockets(tmp_path: Path) -> None:
    source = tmp_path / "rails.txt"
    destination = tmp_path / "fields.json"
    text = 'vanilla: service https://example.invalid/ says "nil"'
    source.write_text("=> [{:id => 8, name: " + json.dumps(text) + ', "optional"=>nil}]', encoding="utf-8")
    assert convert_rails_to_json.convert_ruby_to_json(str(source), str(destination))
    assert json.loads(destination.read_text(encoding="utf-8")) == [{"id": 8, "name": text, "optional": None}]
