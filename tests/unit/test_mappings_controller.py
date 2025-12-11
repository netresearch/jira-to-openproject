import json
from pathlib import Path

import pytest

from src.mappings.mappings import Mappings


@pytest.mark.unit
def test_mappings_set_and_get_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: point data dir to tmp
    data_dir = tmp_path / "var" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    m = Mappings(data_dir=data_dir)

    # Act: set a few mappings
    project_map = {"PRJ": {"openproject_id": 123}}
    user_map = {"alice": {"openproject_id": 7}}
    status_map = {"Open": {"openproject_id": 1}}

    m.set_mapping("project", project_map)
    m.set_mapping("user", user_map)
    m.set_mapping("status", status_map)

    # Assert in-memory
    assert m.get_mapping("project") == project_map
    assert m.get_mapping("user") == user_map
    assert m.get_mapping("status") == status_map

    # Assert on-disk
    assert json.loads((data_dir / Mappings.PROJECT_MAPPING_FILE).read_text()) == project_map
    assert json.loads((data_dir / Mappings.USER_MAPPING_FILE).read_text()) == user_map
    assert json.loads((data_dir / Mappings.STATUS_MAPPING_FILE).read_text()) == status_map


@pytest.mark.unit
def test_mappings_loading_existing_files(tmp_path: Path) -> None:
    # Arrange: create preexisting mapping files
    data_dir = tmp_path / "var" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / Mappings.PROJECT_MAPPING_FILE).write_text(json.dumps({"A": {"openproject_id": 1}}))
    (data_dir / Mappings.USER_MAPPING_FILE).write_text(json.dumps({"u": {"openproject_id": 2}}))
    (data_dir / Mappings.ISSUE_TYPE_MAPPING_FILE).write_text(json.dumps({"Bug": {"openproject_id": 3}}))
    (data_dir / Mappings.STATUS_MAPPING_FILE).write_text(json.dumps({"Open": {"openproject_id": 4}}))
    (data_dir / Mappings.LINK_TYPE_MAPPING_FILE).write_text(json.dumps({"blocks": {"openproject_id": 5}}))
    (data_dir / Mappings.CUSTOM_FIELD_MAPPING_FILE).write_text(json.dumps({"CF1": {"openproject_id": 6}}))
    (data_dir / Mappings.ISSUE_TYPE_ID_MAPPING_FILE).write_text(json.dumps({"Bug": 11}))

    # Act: construct controller
    m = Mappings(data_dir=data_dir)

    # Assert: loaded into memory
    assert m.project_mapping["A"]["openproject_id"] == 1
    assert m.user_mapping["u"]["openproject_id"] == 2
    assert m.issue_type_mapping["Bug"]["openproject_id"] == 3
    assert m.status_mapping["Open"]["openproject_id"] == 4
    assert m.link_type_mapping["blocks"]["openproject_id"] == 5
    assert m.custom_field_mapping["CF1"]["openproject_id"] == 6
    assert m.issue_type_id_mapping["Bug"] == 11


@pytest.mark.unit
def test_has_mapping_and_get_helpers(tmp_path: Path) -> None:
    data_dir = tmp_path / "var" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    m = Mappings(data_dir=data_dir)
    assert not m.has_mapping("project")

    m.set_mapping("project", {"X": {"openproject_id": 9}})
    assert m.has_mapping("project")
    assert m.get_op_project_id("X") == 9
