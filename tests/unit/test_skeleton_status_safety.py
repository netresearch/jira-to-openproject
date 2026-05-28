"""Skeleton migration must not silently pick a status_id that doesn't exist.

Issue #260 background
---------------------
A user ran ``--profile full`` and 10 890 of 10 997 (99 %) work packages
failed in ``work_packages_skeleton`` with the OpenProject validation::

    Failed: TK-1336: Status can't be blank.
    Failed: TK-1295: Status can't be blank.
    ...

Trace through the two-step lookup:

1. Python's :meth:`_get_default_status_id` returns ``1`` as a literal
   fallback when ``self.op_client.get_statuses()`` returns empty or
   raises.
2. The Ruby batch creator in ``openproject_bulk_create_service`` then
   does ``Status.where(id: [1]).index_by(&:id)``. If no Status with
   ID 1 exists on this OP instance (e.g. after an OP 15→17 upgrade
   that renumbered records), ``statuses_by_id[1]`` is ``nil``,
   ``wp.status`` is left ``nil`` and ActiveRecord rejects with
   "Status can't be blank".

Note the asymmetry: the *single-WP* code paths in the same Ruby
template have a ``wp.status ||= Status.order(:position).first``
rescue. The *batch* path does not.

These tests pin the contract:
    1. ``_get_default_status_id`` raises a typed error when no statuses
       are cached and ``op_client.get_statuses()`` returned empty —
       no more silent ``return 1``.
    2. The Ruby batch template now contains
       ``wp.status ||= Status.order(:position).first`` so a single bad
       status_id does not kill the entire batch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PROJ": {"openproject_id": 11}},
                "issue_type": {},
                "issue_type_id": {},
                "status": {},
                "user": {},
                "priority": {},
            }

        def get_mapping(self, name: str):  # type: ignore[no-untyped-def]
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):  # type: ignore[no-untyped-def]
            self._m[name] = value

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def _build_mig(tmp_path: Path):  # type: ignore[no-untyped-def]
    from src.application.components.work_package_skeleton_migration import (
        WorkPackageSkeletonMigration,
    )

    jira = MagicMock()
    op = MagicMock()
    mig = WorkPackageSkeletonMigration(jira_client=jira, op_client=op)
    mig.data_dir = tmp_path
    mig.work_package_mapping_file = tmp_path / mig.WORK_PACKAGE_MAPPING_FILE
    return mig


# ---------------------------------------------------------------------------
# 1. _get_default_status_id must not silently return literal 1
# ---------------------------------------------------------------------------


class TestDefaultStatusIdSafety:
    def test_returns_first_cached_status_id_when_available(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        mig = _build_mig(tmp_path)
        mig.op_client.get_statuses = MagicMock(
            return_value=[
                {"id": 42, "name": "New"},
                {"id": 43, "name": "In progress"},
            ],
        )
        assert mig._get_default_status_id() == 42

    def test_raises_when_get_statuses_returns_empty(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """No silent literal-1 fallback — fail loud with an actionable error."""
        from src.models.migration_error import MigrationError

        mig = _build_mig(tmp_path)
        mig.op_client.get_statuses = MagicMock(return_value=[])

        with pytest.raises(MigrationError) as excinfo:
            mig._get_default_status_id()
        msg = str(excinfo.value)
        # Be helpful: name the cause and the actionable next step.
        assert "status" in msg.lower()
        assert "openproject" in msg.lower() or "op" in msg.lower()

    def test_raises_when_get_statuses_raises(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """A swallowed Exception that left _cached_statuses=[] used to fall
        through to ``return 1``. It must now raise.
        """
        from src.models.migration_error import MigrationError

        mig = _build_mig(tmp_path)
        mig.op_client.get_statuses = MagicMock(side_effect=RuntimeError("rails down"))

        with pytest.raises(MigrationError):
            mig._get_default_status_id()


class TestDefaultTypeIdSafety:
    """Mirror tests for ``_get_default_type_id``. Independent review of #262
    pointed out the same literal-1 fallback existed on the type helper —
    same OP-renumber scenario would produce "Type can't be blank" after
    the status fix landed.
    """

    def test_returns_first_cached_type_id_when_available(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        mig = _build_mig(tmp_path)
        mig.op_client.get_work_package_types = MagicMock(
            return_value=[{"id": 13, "name": "Task"}, {"id": 14, "name": "Bug"}],
        )
        assert mig._get_default_type_id() == 13

    def test_raises_when_types_empty(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        from src.models.migration_error import MigrationError

        mig = _build_mig(tmp_path)
        mig.op_client.get_work_package_types = MagicMock(return_value=[])
        with pytest.raises(MigrationError) as excinfo:
            mig._get_default_type_id()
        assert "type" in str(excinfo.value).lower()

    def test_raises_when_types_fetch_raises(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        from src.models.migration_error import MigrationError

        mig = _build_mig(tmp_path)
        mig.op_client.get_work_package_types = MagicMock(side_effect=RuntimeError("rails down"))
        with pytest.raises(MigrationError):
            mig._get_default_type_id()


class TestDefaultPriorityIdSafety:
    """Mirror tests for ``_get_default_priority_id``. Same rationale."""

    def test_returns_normal_priority_when_present(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        mig = _build_mig(tmp_path)
        mig.op_client.get_issue_priorities = MagicMock(
            return_value=[
                {"id": 7, "name": "Low"},
                {"id": 8, "name": "Normal"},
                {"id": 9, "name": "High"},
            ],
        )
        assert mig._get_default_priority_id() == 8

    def test_returns_first_priority_when_no_normal(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        mig = _build_mig(tmp_path)
        mig.op_client.get_issue_priorities = MagicMock(
            return_value=[{"id": 7, "name": "Low"}, {"id": 9, "name": "High"}],
        )
        assert mig._get_default_priority_id() == 7

    def test_raises_when_priorities_empty(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        from src.models.migration_error import MigrationError

        mig = _build_mig(tmp_path)
        mig.op_client.get_issue_priorities = MagicMock(return_value=[])
        with pytest.raises(MigrationError) as excinfo:
            mig._get_default_priority_id()
        assert "priority" in str(excinfo.value).lower()

    def test_raises_when_priorities_fetch_raises(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        from src.models.migration_error import MigrationError

        mig = _build_mig(tmp_path)
        mig.op_client.get_issue_priorities = MagicMock(side_effect=RuntimeError("rails down"))
        with pytest.raises(MigrationError):
            mig._get_default_priority_id()


# ---------------------------------------------------------------------------
# 2. Ruby batch template must carry the ``||=`` safety net
# ---------------------------------------------------------------------------


class TestRubyBatchTemplateStatusFallback:
    def test_template_includes_status_fallback_to_first_position(
        self,
        tmp_path: Path,
    ) -> None:
        """The batch script sent to the Rails console must include the
        same ``wp.status ||= Status.order(:position).first`` rescue that
        the single-WP code paths already have.

        Otherwise a single bad ``status_id`` failing to resolve causes
        ``wp.status = nil`` → AR rejects ``wp.save`` with "Status can't
        be blank". The fallback turns a hard failure into a soft
        "mapped to default initial status" outcome.
        """
        from src.infrastructure.openproject.openproject_bulk_create_service import (
            OpenProjectBulkCreateService,
        )

        fake_docker = MagicMock()
        fake_docker.execute_command.return_value = ("", "", 0)
        fake_client = MagicMock()
        fake_client.docker_client = fake_docker
        fake_client.logger = MagicMock()
        fake_client.execute_json_query = MagicMock(
            return_value={"created": 1, "failed": 0, "results": []},
        )

        service = OpenProjectBulkCreateService(fake_client)

        # Single representative payload — enough to exercise the template.
        payload = [{"subject": "X", "project_id": 1, "type_id": 1, "status_id": 999}]
        service._create_work_packages_batch(payload)

        # The Ruby script is the (only) argument to ``execute_json_query``.
        assert fake_client.execute_json_query.call_count == 1
        script = fake_client.execute_json_query.call_args[0][0]
        assert isinstance(script, str)

        # The safety net must be present after the status assignment block.
        # Memoised via ``@default_status`` so we don't run
        # ``Status.order(:position).first`` once per failing WP — Gemini's
        # N+1 note on the first pass of #262.
        assert "wp.status ||= (@default_status ||= Status.order(:position).first)" in script, (
            "Batch Ruby template is missing the memoised ``wp.status ||= "
            "(@default_status ||= Status.order(:position).first)`` fallback that "
            "the single-WP paths already have. Without it, a single bad status_id "
            "causes the entire WP to fail with 'Status can't be blank'."
        )
        # Independent review of #262 noted that the same fallback shape
        # was missing for type and priority — the generic
        # ``bulk_create_records`` path has all three rescues, the WP-batch
        # path only had status.
        assert "wp.type ||= (@default_type ||= Type.order(:position).first)" in script
        assert "wp.priority ||= (@default_priority ||= IssuePriority.order(:position).first)" in script
