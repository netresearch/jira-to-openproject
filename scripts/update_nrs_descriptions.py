#!/usr/bin/env python
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Generate NRS description updates using the source keys stored in OpenProject."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.generate_description_updates import publish_updates
from src import config
from src.domain.provenance_fields import ORIGIN_KEY
from src.infrastructure.openproject.openproject_client import OpenProjectClient

_ORIGIN_KEYS = """
project = Project.find_by!(identifier: input_data.fetch("identifier"))
field = WorkPackageCustomField.find_by!(name: input_data.fetch("field"))
values = CustomValue.where(customized_type: 'WorkPackage', customized_id: project.work_packages.select(:id),
                           custom_field_id: field.id).where.not(value: [nil, '']).pluck(:value, :customized_id)
puts $j2o_start_marker
puts JSON.generate(values)
puts $j2o_end_marker
"""


def read_targets(client: OpenProjectClient) -> dict[str, int]:
    result = client.execute_script_with_data(_ORIGIN_KEYS, {"identifier": "nrs", "field": ORIGIN_KEY})
    if result.get("status") != "success" or not isinstance(result.get("data"), list):
        msg = "Could not read Jira keys from OpenProject"
        raise RuntimeError(msg)
    targets = {}
    for key, work_package_id in result["data"]:
        if key in targets and targets[key] != work_package_id:
            msg = f"Jira key {key} identifies multiple target work packages"
            raise ValueError(msg)
        targets[key] = work_package_id
    return targets


def main() -> int:
    try:
        targets = read_targets(OpenProjectClient())
    except Exception as exc:
        print(f"Could not load NRS mappings: {exc}", file=sys.stderr)
        return 1
    return publish_updates(targets, Path(config.get_path("data")))


if __name__ == "__main__":
    sys.exit(main())
