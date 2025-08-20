from pathlib import Path


def test_python_sanitizer_function_present() -> None:
    path = Path("/home/sme/p/j2o/src/migrations/work_package_migration.py")
    text = path.read_text()
    assert "def _sanitize_wp_dict" in text

