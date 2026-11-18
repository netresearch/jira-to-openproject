# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The container chooses the directory and a failed allocation never triggers cleanup."""

from unittest.mock import Mock

import pytest

from src.infrastructure.openproject.container_staging import private_container_directory

pytestmark = pytest.mark.unit


def test_container_staging_cleanup_preserves_original_failure():
    docker = Mock()
    docker.execute_command.side_effect = [("/tmp/j2o-stage.aB921zaq5Z\n", "", 0), OSError("connection lost")]
    with pytest.raises(ValueError, match="transfer failed"), private_container_directory(docker) as directory:
        assert directory.name == "j2o-stage.aB921zaq5Z"
        raise ValueError("transfer failed")
    assert docker.execute_command.call_args.args == ("rm -rf -- /tmp/j2o-stage.aB921zaq5Z",)


@pytest.mark.parametrize("output", ["/", "/tmp/../j2o-stage.aB921zaq5Z", "unallocated", ""])
def test_unexpected_container_staging_path_is_never_used(output):
    docker = Mock()
    docker.execute_command.return_value = output, "", 0
    with pytest.raises(OSError, match="allocate"), private_container_directory(docker):
        pytest.fail("unexpected allocation succeeded")
    docker.execute_command.assert_called_once()
