"""Tests for ``scripts/normalize_wp_mapping.py``.

The script is loaded via :mod:`importlib` because the ``scripts/``
directory is not a regular package (no ``__init__.py``). Tests cover:

* end-to-end rewrite of a real fixture file with mixed shapes;
* ``--dry-run`` producing diff output without touching the file;
* graceful no-ops on missing files and malformed JSON.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "normalize_wp_mapping.py"


def _load_script_module() -> ModuleType:
    """Import ``scripts/normalize_wp_mapping.py`` as a module.

    A fresh import per test keeps argparse state clean and ensures the
    module-level ``logging.basicConfig`` call inside ``run`` is
    observable through ``caplog``.
    """
    spec = importlib.util.spec_from_file_location(
        "normalize_wp_mapping_script",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        msg = f"Cannot load script from {SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script_module() -> Iterator[ModuleType]:
    module = _load_script_module()
    try:
        yield module
    finally:
        sys.modules.pop("normalize_wp_mapping_script", None)


def _write_mapping(data_dir: Path, payload: dict[str, object]) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "work_package_mapping.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_run_rewrites_file_with_mixed_shapes(
    tmp_path: Path,
    script_module: ModuleType,
) -> None:
    data_dir = tmp_path / "var" / "data"
    target = _write_mapping(
        data_dir,
        {
            "PROJ-1": {"openproject_id": 10, "openproject_project_id": 5},
            "PROJ-2": 20,
            "PROJ-BAD": "corrupt",
        },
    )
    # Pin a non-default mode so we can assert it survives the rewrite.
    target.chmod(0o640)

    exit_code = script_module.run(["--data-dir", str(data_dir)])

    assert exit_code == 0
    rewritten = json.loads(target.read_text(encoding="utf-8"))
    assert set(rewritten) == {"PROJ-1", "PROJ-2"}
    assert rewritten["PROJ-1"]["openproject_id"] == 10
    assert rewritten["PROJ-1"]["openproject_project_id"] == 5
    assert rewritten["PROJ-2"]["openproject_id"] == 20
    assert rewritten["PROJ-2"]["openproject_project_id"] is None

    assert (target.stat().st_mode & 0o777) == 0o640


def test_dry_run_leaves_file_untouched_but_reports_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    script_module: ModuleType,
) -> None:
    data_dir = tmp_path / "var" / "data"
    target = _write_mapping(data_dir, {"PROJ-1": 7})
    original_text = target.read_text(encoding="utf-8")

    exit_code = script_module.run(["--data-dir", str(data_dir), "--dry-run"])

    assert exit_code == 0
    # File on disk is unchanged byte-for-byte.
    assert target.read_text(encoding="utf-8") == original_text

    captured = capsys.readouterr()
    assert "would update PROJ-1" in captured.out


def test_missing_file_exits_zero(
    tmp_path: Path,
    script_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = tmp_path / "var" / "data"  # never created
    with caplog.at_level("INFO", logger="normalize_wp_mapping"):
        exit_code = script_module.run(["--data-dir", str(data_dir)])

    assert exit_code == 0
    assert any("nothing to normalise" in rec.getMessage() for rec in caplog.records)


def test_malformed_json_exits_zero(
    tmp_path: Path,
    script_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = tmp_path / "var" / "data"
    data_dir.mkdir(parents=True)
    target = data_dir / "work_package_mapping.json"
    target.write_text("{not-json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="normalize_wp_mapping"):
        exit_code = script_module.run(["--data-dir", str(data_dir)])

    assert exit_code == 0
    # File is preserved as-is (we never overwrite malformed input).
    assert target.read_text(encoding="utf-8") == "{not-json"
    assert any("malformed JSON" in rec.getMessage() for rec in caplog.records)


def test_empty_file_exits_zero(
    tmp_path: Path,
    script_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = tmp_path / "var" / "data"
    data_dir.mkdir(parents=True)
    target = data_dir / "work_package_mapping.json"
    target.write_text("   \n", encoding="utf-8")

    with caplog.at_level("INFO", logger="normalize_wp_mapping"):
        exit_code = script_module.run(["--data-dir", str(data_dir)])

    assert exit_code == 0
    assert target.read_text(encoding="utf-8") == "   \n"
