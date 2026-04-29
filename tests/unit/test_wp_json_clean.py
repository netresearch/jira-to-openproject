import json
from pathlib import Path

import pytest


def _validate(path: Path) -> None:
    with path.open("r", encoding="utf-8") as f:
        arr = json.load(f)
    for idx, item in enumerate(arr if isinstance(arr, list) else [arr]):
        assert isinstance(item, dict), f"{path} entry #{idx} is not an object"
        assert "_links" not in item, f"{path} entry #{idx} unexpectedly contains _links"


def test_clean_work_package_json_passes_validation(tmp_path: Path) -> None:
    """A well-formed work_packages_*.json payload (no _links) must pass."""
    payload = [
        {"subject": "Task A", "description": "..."},
        {"subject": "Task B", "description": "..."},
    ]
    f = tmp_path / "work_packages_clean.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    _validate(f)


def test_work_package_json_with_links_is_rejected(tmp_path: Path) -> None:
    """If a _links field ever sneaks in, the validator must trip."""
    payload = [{"subject": "Task", "_links": {"self": "..."}}]
    f = tmp_path / "work_packages_dirty.json"
    f.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AssertionError, match="_links"):
        _validate(f)
