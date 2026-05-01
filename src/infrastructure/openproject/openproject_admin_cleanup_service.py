"""Admin cleanup helpers for the OpenProject Rails console.

Phase 2v of ADR-002 collects the destructive admin operations onto a
focused service so they're easy to find and audit. The service owns:

* ``delete_all_projects`` — ``Project.delete_all`` (no validation,
  no callbacks).
* ``delete_non_default_issue_types`` — destroys every ``Type`` with
  ``is_default: false`` AND ``is_standard: false`` (callbacks fire).
* ``delete_non_default_issue_statuses`` — destroys every ``Status``
  with ``is_default: false`` (callbacks fire).

Note on authorisation
---------------------
The Rails-console execution path runs whatever the console session is
allowed to run; there is no server-side admin guard inside these
helpers. The methods exist for migration teardown and CI fixture
reset, NOT for runtime use against a live install. Wrap them in your
own authorisation check (or just don't call them) if you have any
concerns about misuse.

Note on return values
---------------------
The pre-extraction implementation routed through
``self.execute_query`` whose result is the raw console output as a
*string* — so the previous ``isinstance(count, int)`` guard always
saw ``False`` and the methods silently reported 0 even when
``Project.delete_all`` etc. succeeded. Same shape as the
``delete_all_work_packages`` bug fixed during Phase 2n review.
Fixed at the move by routing through ``execute_json_query`` so the
integer comes back as an actual ``int``. When the response somehow
isn't an integer the method warns via ``self._logger`` and returns
0 — still honest, but flags the anomaly for log scraping.

``OpenProjectClient`` exposes the service via ``self.admin_cleanup``
and keeps thin delegators for the same method names so existing call
sites work unchanged. ``delete_all_custom_fields`` stays on the
client as a delegator into ``OpenProjectCustomFieldService`` (where
it was extracted in Phase 2a/2b) — no further move needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.infrastructure.exceptions import QueryExecutionError

if TYPE_CHECKING:
    from src.infrastructure.openproject.openproject_client import OpenProjectClient


class OpenProjectAdminCleanupService:
    """Destructive admin / cleanup helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    def delete_all_projects(self) -> int:
        """Delete all projects in bulk.

        Returns:
            Number of deleted projects

        Raises:
            QueryExecutionError: If bulk deletion fails

        """
        try:
            # ``execute_json_query`` parses the integer; the previous
            # ``execute_query`` returned the raw console string and
            # the ``isinstance(count, int)`` guard always saw
            # ``False``, masking successful runs as "0 deleted".
            count = self._client.execute_json_query("Project.delete_all")
            if isinstance(count, int):
                return count
            self._logger.warning(
                "Project.delete_all returned non-int %s; reporting 0 deleted",
                type(count).__name__,
            )
            return 0
        except Exception as e:
            msg = "Failed to delete all projects."
            raise QueryExecutionError(msg) from e

    def delete_non_default_issue_types(self) -> int:
        """Delete non-default issue types (work package types).

        Returns:
            Number of deleted types

        Raises:
            QueryExecutionError: If deletion fails

        """
        script = """
        non_default_types = Type.where(is_default: false, is_standard: false)
        count = non_default_types.count
        non_default_types.destroy_all
        count
        """

        try:
            # See ``delete_all_projects`` — switch from
            # ``execute_query`` to ``execute_json_query`` so the count
            # comes back as a real int rather than a console string.
            count = self._client.execute_json_query(script)
            if isinstance(count, int):
                return count
            self._logger.warning(
                "delete_non_default_issue_types returned non-int %s; reporting 0 deleted",
                type(count).__name__,
            )
            return 0
        except Exception as e:
            msg = "Failed to delete non-default issue types."
            raise QueryExecutionError(msg) from e

    def delete_non_default_issue_statuses(self) -> int:
        """Delete non-default issue statuses.

        Returns:
            Number of deleted statuses

        Raises:
            QueryExecutionError: If deletion fails

        """
        script = """
        non_default_statuses = Status.where(is_default: false)
        count = non_default_statuses.count
        non_default_statuses.destroy_all
        count
        """

        try:
            count = self._client.execute_json_query(script)
            if isinstance(count, int):
                return count
            self._logger.warning(
                "delete_non_default_issue_statuses returned non-int %s; reporting 0 deleted",
                type(count).__name__,
            )
            return 0
        except Exception as e:
            msg = "Failed to delete non-default issue statuses."
            raise QueryExecutionError(msg) from e
