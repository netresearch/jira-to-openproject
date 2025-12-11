from pathlib import Path


def test_python_sanitizer_function_present() -> None:
    project_root = Path(__file__).resolve().parents[2]
    path = project_root / "src" / "migrations" / "work_package_migration.py"
    text = path.read_text()
    assert "def _sanitize_wp_dict" in text
