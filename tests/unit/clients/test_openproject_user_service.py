"""Regression tests for :class:`OpenProjectUserService`.

Covers issue #255: ``get_user_by_email`` built a Rails query against the
non-existent ``users.email`` column. OpenProject's column is ``mail``
(``User.column_names`` has ``mail``, never ``email``), so the cache-miss
fallback raised ``PG::UndefinedColumn`` and stalled ``--components users``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.infrastructure.openproject.openproject_user_service import (
    OpenProjectUserService,
)


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.fix_verification
def test_get_user_by_email_queries_mail_column_not_email() -> None:
    """On a cache miss the fallback must query ``mail``, never ``email``.

    OpenProject has no ``users.email`` column; querying it raises
    ``PG::UndefinedColumn`` (issue #255).
    """
    client = MagicMock()
    client._users_by_email_cache = {}
    client.find_record.return_value = {"id": 7, "mail": "user@example.com"}

    service = OpenProjectUserService(client)
    # Empty full-list so the email index stays cold and we hit the
    # direct-query fallback that previously used the wrong column.
    service.get_users = MagicMock(return_value=[])

    result = service.get_user_by_email("User@Example.com")

    client.find_record.assert_called_once_with("User", {"mail": "User@Example.com"})
    assert result == {"id": 7, "mail": "user@example.com"}
    # Successful lookups are cached under the normalised (lower-cased) key.
    assert client._users_by_email_cache["user@example.com"] == result
