# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""An uncertain response must not cause a second target write."""

from unittest.mock import Mock

import pytest

from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.openproject_records_service import OpenProjectRecordsService


def test_ambiguous_create_response_stops_before_retry() -> None:
    client = Mock()
    client.execute_query_to_json_file.return_value = None
    service = OpenProjectRecordsService(client)
    with pytest.raises(QueryExecutionError, match="Verify the target"):
        service.create_record("Type", {"name": "synthetic"})
    client.execute_query_to_json_file.assert_called_once()
    client.execute_json_query.assert_not_called()
