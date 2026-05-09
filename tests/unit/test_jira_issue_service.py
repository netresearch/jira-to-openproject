"""Unit tests for :class:`src.infrastructure.jira.jira_issue_service.JiraIssueService`.

Regression tests for the URL-length 401 bug:
  When ``_fetch_issues_batch`` is called with a large key list (e.g. 100 keys),
  the resulting JQL ``key in ("K-1","K-2",...)`` can exceed 8 KB when URL-encoded
  by the HTTP stack (Apache Tomcat / Traefik default limit).  The server then
  rejects the request — returning HTTP 401 — before the auth layer is even
  consulted, making the error look like an authentication failure.

  Fix: ``_fetch_issues_batch`` must split large input lists into sub-chunks of
  ``_FETCH_BATCH_CHUNK_SIZE`` (≤ 25 keys), fetch each chunk independently, and
  merge the results transparently so callers see a single ``dict[str, Issue]``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import JIRAError from the stub injected by conftest, or fall back to real pkg
# ---------------------------------------------------------------------------
try:
    from jira.exceptions import JIRAError
except ImportError:
    from jira import JIRAError  # type: ignore[no-redef]

from src.infrastructure.jira.jira_issue_service import (
    _FETCH_BATCH_CHUNK_SIZE,
    JiraIssueService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue(key: str) -> SimpleNamespace:
    """Return a minimal Issue-like stub with a ``key`` attribute."""
    return SimpleNamespace(key=key)


def _make_client(search_side_effect: Any = None) -> SimpleNamespace:
    """Build a minimal ``JiraClient``-like stub for ``JiraIssueService``."""
    jira_mock = MagicMock()
    if search_side_effect is not None:
        jira_mock.search_issues.side_effect = search_side_effect
    else:
        jira_mock.search_issues.return_value = []

    # ``JiraIssueService.__init__`` does a local import of ``logger`` from
    # ``src.infrastructure.jira.jira_client`` — we patch that module's logger
    # in the fixture below.  Here we only need the structural attributes.
    performance_optimizer = SimpleNamespace(
        batch_processor=SimpleNamespace(
            process_batches=MagicMock(return_value=[]),
        )
    )

    return SimpleNamespace(
        jira=jira_mock,
        batch_size=100,
        performance_optimizer=performance_optimizer,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suppress logger output in all tests in this module."""
    import logging

    import src.infrastructure.jira.jira_client as jira_client_mod

    monkeypatch.setattr(jira_client_mod, "logger", logging.getLogger("test.jira_issue_service"))


# ---------------------------------------------------------------------------
# Constant sanity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_batch_chunk_size_is_safe() -> None:
    """``_FETCH_BATCH_CHUNK_SIZE`` must be a positive int well below 100."""
    assert isinstance(_FETCH_BATCH_CHUNK_SIZE, int)
    assert 1 <= _FETCH_BATCH_CHUNK_SIZE <= 50, (
        f"Chunk size {_FETCH_BATCH_CHUNK_SIZE} is outside the safe 1–50 range "
        "that avoids Tomcat/Traefik URL-length limits"
    )


# ---------------------------------------------------------------------------
# Core chunking behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_issues_batch_splits_large_input_into_chunks() -> None:
    """``_fetch_issues_batch`` with 100 keys must call ``search_issues`` multiple
    times — at most ``ceil(100 / _FETCH_BATCH_CHUNK_SIZE)`` times — and return
    all 100 issues merged into a single dict.

    This is the regression test for the Tomcat HTTP 401 / URL-too-long bug
    observed during the NRS migration (keys NRS-4311…NRS-4400).
    """
    keys = [f"NRS-{4311 + i}" for i in range(100)]

    def _search_side_effect(jql: str, **_kw: object) -> list[SimpleNamespace]:
        # Extract quoted keys from the JQL and return stubs for each.
        import re

        found = re.findall(r'"([^"]+)"', jql)
        return [_make_issue(k) for k in found]

    client = _make_client(search_side_effect=_search_side_effect)
    service = JiraIssueService(client)  # type: ignore[arg-type]

    result = service._fetch_issues_batch(keys)

    assert len(result) == 100
    for k in keys:
        assert k in result, f"Expected key {k} missing from result"

    import math

    expected_calls = math.ceil(100 / _FETCH_BATCH_CHUNK_SIZE)
    assert client.jira.search_issues.call_count == expected_calls, (
        f"Expected {expected_calls} search_issues calls for 100 keys "
        f"(chunk size={_FETCH_BATCH_CHUNK_SIZE}), "
        f"got {client.jira.search_issues.call_count}"
    )


@pytest.mark.unit
def test_fetch_issues_batch_small_input_single_call() -> None:
    """Inputs at or below ``_FETCH_BATCH_CHUNK_SIZE`` must result in exactly
    one ``search_issues`` call — no unnecessary chunking overhead.
    """
    keys = [f"TEST-{i}" for i in range(1, _FETCH_BATCH_CHUNK_SIZE + 1)]

    def _search(jql: str, **_kw: object) -> list[SimpleNamespace]:
        import re

        return [_make_issue(k) for k in re.findall(r'"([^"]+)"', jql)]

    client = _make_client(search_side_effect=_search)
    service = JiraIssueService(client)  # type: ignore[arg-type]

    result = service._fetch_issues_batch(keys)

    assert len(result) == _FETCH_BATCH_CHUNK_SIZE
    assert client.jira.search_issues.call_count == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_issues_batch_chunk_failure_returns_empty_for_that_chunk() -> None:
    """If one chunk raises an exception, that chunk contributes an empty dict
    to the result (existing error-logging path) while other chunks succeed.
    """
    # 50 keys in two chunks.  First chunk succeeds; second raises JIRAError.
    keys_a = [f"OK-{i}" for i in range(1, 26)]
    keys_b = [f"FAIL-{i}" for i in range(1, 26)]
    all_keys = keys_a + keys_b

    call_count = 0

    def _search(jql: str, **_kw: object) -> list[SimpleNamespace]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            import re

            return [_make_issue(k) for k in re.findall(r'"([^"]+)"', jql)]
        raise JIRAError("Simulated 401", 401, "http://jira.test/search?jql=...")

    client = _make_client(search_side_effect=_search)
    service = JiraIssueService(client)  # type: ignore[arg-type]

    result = service._fetch_issues_batch(all_keys)

    # First chunk's issues must be present; second chunk is empty.
    for k in keys_a:
        assert k in result
    # No FAIL- keys because the second chunk errored out.
    for k in keys_b:
        assert k not in result


@pytest.mark.unit
def test_fetch_issues_batch_empty_input_returns_empty_dict() -> None:
    """Empty input must return an empty dict without calling ``search_issues``."""
    client = _make_client()
    service = JiraIssueService(client)  # type: ignore[arg-type]

    result = service._fetch_issues_batch([])

    assert result == {}
    client.jira.search_issues.assert_not_called()


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_issues_batch_deduplicates_keys() -> None:
    """Duplicate keys in the input must be collapsed to unique keys before the
    JQL query is built, so ``search_issues`` is called exactly once with only
    the distinct keys.
    """
    captured_jql: list[str] = []

    def _search(jql: str, **_kw: object) -> list[SimpleNamespace]:
        captured_jql.append(jql)
        import re

        return [_make_issue(k) for k in re.findall(r'"([^"]+)"', jql)]

    client = _make_client(search_side_effect=_search)
    service = JiraIssueService(client)  # type: ignore[arg-type]

    result = service._fetch_issues_batch(["TEST-1", "TEST-1", "TEST-2"])

    # Only one search_issues call (two unique keys fit in a single chunk).
    assert client.jira.search_issues.call_count == 1
    # Result contains exactly the two unique keys.
    assert set(result.keys()) == {"TEST-1", "TEST-2"}
    # The JQL must not repeat any key.
    jql = captured_jql[0]
    assert jql.count('"TEST-1"') == 1, "TEST-1 must appear exactly once in JQL"
    assert jql.count('"TEST-2"') == 1, "TEST-2 must appear exactly once in JQL"


# ---------------------------------------------------------------------------
# batch_num in error log
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_single_chunk_includes_batch_num_in_error_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``_fetch_single_chunk`` fails, the error log must include the
    ``batch_num`` kwarg so operators can correlate errors across concurrent
    batches.
    """
    import logging

    client = _make_client(search_side_effect=RuntimeError("simulated failure"))
    service = JiraIssueService(client)  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR):
        result = service._fetch_single_chunk(
            ["PROJ-10", "PROJ-11"],
            chunk_index=0,
            batch_num=7,
        )

    assert result == {}, "Failed chunk must return empty dict"
    # batch_num=7 must appear somewhere in the captured log output.
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert "7" in combined, f"batch_num=7 not found in log output: {combined!r}"
