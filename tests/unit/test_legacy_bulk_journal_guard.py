# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Legacy replay must be rejected before an unrelated record or journal changes."""

from unittest.mock import Mock

import pytest

from src.infrastructure.openproject.openproject_bulk_create_service import (
    LegacyJournalReplayError,
    OpenProjectBulkCreateService,
    reject_legacy_journal_operations,
)
from src.infrastructure.openproject.openproject_work_package_service import OpenProjectWorkPackageService


def test_mixed_batch_fails_before_staging_or_writing() -> None:
    client = Mock()
    bulk = OpenProjectBulkCreateService(client)
    wps = OpenProjectWorkPackageService(client)
    replay = {"id": 123, "_rails_operations": [{"notes": "private source text"}]}
    records = [{"subject": "eligible"}, replay]

    for operation in (
        lambda: bulk.bulk_create_records("WorkPackage", records),
        lambda: bulk.batch_create_work_packages(records),
        lambda: bulk._create_work_packages_batch(records),
        lambda: wps.create_work_package(replay),
        lambda: wps.batch_update_work_packages(records),
        lambda: wps.update_work_package(123, replay),
    ):
        with pytest.raises(LegacyJournalReplayError, match="work_packages_skeleton") as exc:
            operation()
        assert "private source text" not in str(exc.value)
        assert client.mock_calls == []


@pytest.mark.parametrize("invalid", [{}, "", False, "operation"])
def test_unknown_operations_are_held(invalid: object) -> None:
    record = {"_rails_operations": invalid}
    with pytest.raises(LegacyJournalReplayError):
        reject_legacy_journal_operations([record])


def test_no_journal_operations_are_requested() -> None:
    reject_legacy_journal_operations([{}, {"_rails_operations": []}, {"_rails_operations": None}])


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"subject": "one", "author_id": 7}, {"subject": "one", "author_id": 7}),
        (
            {"subject": "two", "_links": {"status": {"href": "/api/v3/statuses/5"}}},
            {"subject": "two", "status_id": 5},
        ),
    ],
)
def test_ordinary_work_package_creation(payload: dict, expected: dict) -> None:
    client = Mock()
    client._create_work_packages_batch.return_value = {"results": [42]}
    assert OpenProjectWorkPackageService(client).create_work_package(payload) == {"id": 42}
    client._create_work_packages_batch.assert_called_once_with([expected])
