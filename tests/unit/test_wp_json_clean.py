from pathlib import Path
import json


def test_generated_work_packages_json_has_no_links() -> None:






    data_dir = Path('/home/sme/p/j2o/var/data')
    assert data_dir.exists(), "var/data directory must exist"

    json_files = list(data_dir.glob('work_packages_*.json'))
    assert json_files, "no work_packages_*.json files found to validate"

    for jf in json_files:
        with jf.open('r', encoding='utf-8') as f:
            try:
                arr = json.load(f)
            except Exception as e:
                raise AssertionError(f"Invalid JSON in {jf}: {e}")

        # Each entry should not contain _links anywhere
        for idx, item in enumerate(arr if isinstance(arr, list) else [arr]):
            assert isinstance(item, dict), f"{jf} entry #{idx} is not an object"
            assert '_links' not in item, f"{jf} entry #{idx} unexpectedly contains _links"


