#!/usr/bin/env python3
"""Regression tests for ``OpenProjectCustomFieldService.ensure_wp_custom_field_id``.

GitHub issue #260: creating a WorkPackage custom field crashed five
migration components (resolutions, labels, security_levels,
affects_versions, votes_reactions) with::

    ValueError: invalid literal for int() with base 10:
      'open-project(prod):2920> # j2o: ... func=get_work_package_types ...'

Root cause: ``ensure_wp_custom_field_id`` called the *raw* console
transport (``execute_query`` -> ``_send_command_to_tmux``), which returns
the unfiltered tmux pane tail — irb continuation prompts and stale
``--EXEC`` / ``# j2o:`` residue from a *previous* call — and then did a
naked ``int(result)`` on that text. The prompt-stripping filter added in
74b16bc is only wired into the marker-based ``execute()`` path, never the
raw ``execute_query`` path this method used.

Fix: route through the isolated ``execute_json_query`` (Ruby writes the id
to a container file that is read back via ``cat`` + ``json.loads``) exactly
like the sibling ``ensure_work_package_custom_field`` — pane contamination
can no longer reach the integer conversion.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from src.infrastructure.exceptions import RecordNotFoundError
from src.infrastructure.openproject.openproject_custom_field_service import (
    OpenProjectCustomFieldService,
)

# The verbatim contaminated buffer from issue #260 (resolutions run). A naked
# ``int()`` of this raises ``ValueError`` — which is exactly the crash.
CONTAMINATED_CONSOLE_OUTPUT = (
    "open-project(prod):2920> # j2o: infrastructure/openproject/"
    "openproject_status_type_service func=get_work_package_types "
    "ts=2026-05-29T19:52:28Z pid=5789\n\n"
    "open-project(prod):2921>\nopen-project(prod):2922>"
)


class _FakeClient:
    """Minimal stand-in for ``OpenProjectClient`` for the service under test.

    ``find_record`` raises so the method takes the *create* path. The two
    console entry points are tracked so a test can assert which transport
    the production code chose:

    * ``execute_query`` is the *raw, contamination-prone* path. It returns
      the issue-#260 buffer, so any code path that still calls it and then
      does ``int(...)`` reproduces the crash.
    * ``execute_json_query`` is the *isolated* path; it returns a clean dict.
    """

    def __init__(self, json_result: Any) -> None:
        self.logger = logging.getLogger("test.ensure_wp_cf")
        self._json_result = json_result
        self.execute_query_calls: list[str] = []
        self.execute_json_query_calls: list[str] = []

    def find_record(self, _model: str, _query: dict[str, Any]) -> dict[str, Any]:
        msg = "not found"
        raise RecordNotFoundError(msg)

    def execute_query(self, script: str, timeout: int | None = None) -> str:
        self.execute_query_calls.append(script)
        return CONTAMINATED_CONSOLE_OUTPUT

    def execute_json_query(self, query: str, timeout: int | None = None) -> Any:
        self.execute_json_query_calls.append(query)
        return self._json_result


def test_contaminated_console_buffer_does_not_break_cf_id_resolution() -> None:
    """#260: a stale/garbled pane must not crash CF-id resolution.

    With the fix the method reads the id from the isolated JSON path; the
    contaminated ``execute_query`` buffer is never fed to ``int()``.
    """
    client = _FakeClient(json_result={"id": 42})
    service = OpenProjectCustomFieldService(client)

    cf_id = service.ensure_wp_custom_field_id("J2O Resolution", "string")

    assert cf_id == 42
    assert isinstance(cf_id, int)
    # The fix must use the isolated JSON path, not the raw pane transport.
    assert client.execute_json_query_calls, "expected the isolated execute_json_query path to be used"
    assert not client.execute_query_calls, "raw execute_query must not be used for CF-id resolution"


def test_cf_id_is_int_even_when_json_returns_string_id() -> None:
    """The id is coerced to ``int`` (Rails ``as_json`` may emit it as text)."""
    client = _FakeClient(json_result={"id": "57"})
    service = OpenProjectCustomFieldService(client)

    assert service.ensure_wp_custom_field_id("J2O Labels", "text") == 57


@pytest.mark.parametrize("json_result", [None, {}, {"id": None}, {"id": 0}, False])
def test_creation_failure_returns_zero_not_raise(json_result: Any) -> None:
    """Genuine creation failure returns 0 — the contract callers rely on.

    Components such as ``LabelsMigration`` / ``VotesMigration`` guard with
    ``if not cf_id: return ComponentResult(success=False, ...)``. The fix
    must preserve that falsy-on-failure contract rather than raising.
    """
    client = _FakeClient(json_result=json_result)
    service = OpenProjectCustomFieldService(client)

    assert service.ensure_wp_custom_field_id("J2O Votes", "int") == 0
