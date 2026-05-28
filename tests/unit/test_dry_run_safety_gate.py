"""Startup gate that keeps ``--dry-run`` honest.

Issue #260 background
---------------------
The orchestrator printed a confident WARNING — *"no changes will be made to
OpenProject"* — at the top of every ``--dry-run`` run. In reality only 5 of
~41 components honoured the flag; the rest wrote to OpenProject regardless.
The reporter trusted the WARNING and got 10 890 real work-package creation
attempts.

PR D introduces a startup gate (Phase 1 of the hybrid plan):

    * Each component declares ``DRY_RUN_SAFE: ClassVar[bool]`` on its class.
      ``BaseMigration`` defaults the attribute to ``False``; the 5 safe
      components override it to ``True``.
    * The orchestrator partitions the requested components against the
      declared safety. Under ``--dry-run`` the run aborts when any unsafe
      component is in scope — unless the operator opts in with
      ``--allow-unsafe-dry-run``.
    * Acknowledged-unsafe runs print a STRONGER warning enumerating the
      components that will still write.

These tests pin the partitioning + gate behaviour as pure functions so we
can extend the safe-set incrementally without touching the orchestrator.
"""

from __future__ import annotations

import asyncio

import pytest

from src.migration import (
    DRY_RUN_SAFE_COMPONENTS,
    _build_dry_run_banner,
    _dry_run_safety_partition,
    run_migration,
)


class TestDryRunSafetyPartition:
    def test_partitions_known_safe_and_unsafe_components(self) -> None:
        safe, unsafe = _dry_run_safety_partition(
            ["projects", "work_packages_skeleton", "issue_types", "attachments"],
        )
        assert safe == ["projects", "issue_types"]
        assert unsafe == ["work_packages_skeleton", "attachments"]

    def test_returns_empty_lists_for_no_requests(self) -> None:
        safe, unsafe = _dry_run_safety_partition([])
        assert safe == []
        assert unsafe == []

    def test_unknown_components_count_as_unsafe(self) -> None:
        """A typo or out-of-tree component name must NOT be silently
        treated as safe — fail-closed is the right default for a flag
        whose contract is "no changes to OpenProject".
        """
        safe, unsafe = _dry_run_safety_partition(["does_not_exist"])
        assert safe == []
        assert unsafe == ["does_not_exist"]


class TestDryRunBanner:
    """``_build_dry_run_banner`` returns ``(level, lines, abort)`` —
    ``abort=True`` means the orchestrator should refuse to run.
    """

    def test_no_components_means_no_banner_and_no_abort(self) -> None:
        _level, lines, abort = _build_dry_run_banner(
            requested=[],
            allow_unsafe=False,
        )
        assert abort is False
        assert lines == []

    def test_all_safe_emits_clean_warning_no_abort(self) -> None:
        level, lines, abort = _build_dry_run_banner(
            requested=["projects", "companies"],
            allow_unsafe=False,
        )
        assert abort is False
        assert level == "warning"
        body = " ".join(lines).lower()
        assert "dry run" in body
        assert "projects" in body and "companies" in body
        # The honest-warning case may say "no changes will be made" — but
        # it must scope that claim to the listed components, not blanket
        # the whole run as the original buggy banner did.
        if "no changes will be made" in body:
            assert "for these components" in body or "to these components" in body

    def test_unsafe_without_opt_in_aborts(self) -> None:
        level, lines, abort = _build_dry_run_banner(
            requested=["projects", "work_packages_skeleton", "attachments"],
            allow_unsafe=False,
        )
        assert abort is True
        assert level == "error"
        body = "\n".join(lines).lower()
        assert "work_packages_skeleton" in body
        assert "attachments" in body
        # Must suggest the opt-in flag so users know how to proceed.
        assert "--allow-unsafe-dry-run" in body

    def test_unsafe_with_opt_in_warns_loudly_but_does_not_abort(self) -> None:
        level, lines, abort = _build_dry_run_banner(
            requested=["projects", "work_packages_skeleton"],
            allow_unsafe=True,
        )
        assert abort is False
        assert level == "warning"
        body = "\n".join(lines).lower()
        # Must explicitly call out the unsafe ones — silence here would
        # restore the original honesty bug.
        assert "work_packages_skeleton" in body
        assert "will still write" in body or "writes to openproject" in body


class TestRegistryClassAttributeDriftGuard:
    """``DRY_RUN_SAFE_COMPONENTS`` is the orchestrator's view of safety;
    each safe component also declares ``DRY_RUN_SAFE = True`` on its
    class. The two must stay in sync — drift here would re-introduce
    the original honesty bug.
    """

    def test_registry_matches_class_attributes(self) -> None:
        from src.application.components.company_migration import CompanyMigration
        from src.application.components.issue_type_migration import IssueTypeMigration
        from src.application.components.link_type_migration import LinkTypeMigration
        from src.application.components.project_migration import ProjectMigration
        from src.application.components.status_migration import StatusMigration

        declared_via_class = {
            name
            for name, cls in {
                "projects": ProjectMigration,
                "issue_types": IssueTypeMigration,
                "link_types": LinkTypeMigration,
                "companies": CompanyMigration,
                "status_types": StatusMigration,
            }.items()
            if getattr(cls, "DRY_RUN_SAFE", False)
        }
        assert declared_via_class == DRY_RUN_SAFE_COMPONENTS

    def test_basemigration_default_is_unsafe(self) -> None:
        from src.application.components.base_migration import BaseMigration

        assert getattr(BaseMigration, "DRY_RUN_SAFE", None) is False


class TestUnsafeComponentClassesStayUnsafe:
    """A regression guard — picking a handful of high-impact unsafe
    components and asserting they did NOT accidentally inherit the safe
    marker. The set is small on purpose; it documents *what was unsafe
    in the original #260 incident*.
    """

    @pytest.mark.parametrize(
        ("module_path", "class_name"),
        [
            (
                "src.application.components.work_package_skeleton_migration",
                "WorkPackageSkeletonMigration",
            ),
            (
                "src.application.components.attachments_migration",
                "AttachmentsMigration",
            ),
            (
                "src.application.components.time_entry_migration",
                "TimeEntryMigration",
            ),
            (
                "src.application.components.admin_scheme_migration",
                "AdminSchemeMigration",
            ),
        ],
    )
    def test_class_is_marked_unsafe(self, module_path: str, class_name: str) -> None:
        import importlib

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        assert getattr(cls, "DRY_RUN_SAFE", False) is False, (
            f"{class_name} does not honour --dry-run; it must NOT carry DRY_RUN_SAFE = True"
        )


class TestRunMigrationGateIntegration:
    """End-to-end gate behaviour: the abort must propagate out of
    ``run_migration`` as a ``SystemExit`` (not a swallowed ``Exception``).

    Independent code review of #264 noted that the outer ``try/except
    Exception`` in ``run_migration`` correctly leaves ``BaseException``
    subclasses alone — but nothing pinned that contract. A future
    refactor swapping to ``except BaseException`` would silently
    re-introduce the #260 honesty bug.
    """

    def test_bare_dry_run_with_unsafe_default_components_aborts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The critical case: ``j2o migrate --dry-run`` with no
        ``--components`` and no ``--profile``. Defaults expand to the
        full sequence, which contains many unsafe components, and the
        gate must abort. This pins the fix for the regression the
        independent reviewer found on the first iteration of this PR.
        """
        monkeypatch.setattr(
            "src.migration.config.migration_config",
            {"dry_run": True, "no_backup": True},
        )
        with pytest.raises(SystemExit) as excinfo:
            # ``components=None`` triggers default expansion in
            # ``run_migration`` — same path as bare ``--dry-run``.
            asyncio.run(run_migration(components=None))
        assert "--allow-unsafe-dry-run" in str(excinfo.value)

    def test_dry_run_with_only_safe_components_does_not_abort(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting ``--components`` to only safe ones lets the run pass
        the gate without ``--allow-unsafe-dry-run``. ``run_migration``
        will then fail on downstream client setup (no real Jira / OP
        configured), but the gate's own ``SystemExit`` must NOT fire.
        """
        monkeypatch.setattr(
            "src.migration.config.migration_config",
            {"dry_run": True, "no_backup": True},
        )
        # The outer ``try/except Exception`` in ``run_migration``
        # swallows non-BaseException failures; assert it returns
        # normally rather than aborting via the gate's SystemExit.
        result = asyncio.run(run_migration(components=["projects"]))
        # If the gate had aborted, SystemExit would have propagated and
        # we'd never reach this assert.
        assert result is not None

    def test_dry_run_with_allow_unsafe_flag_does_not_abort(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--allow-unsafe-dry-run`` lets the run pass the gate even
        when unsafe components are in scope.
        """
        monkeypatch.setattr(
            "src.migration.config.migration_config",
            {
                "dry_run": True,
                "allow_unsafe_dry_run": True,
                "no_backup": True,
            },
        )
        # Same shape as the previous test: the gate must NOT abort.
        # Downstream failure inside ``run_migration`` is irrelevant —
        # only the gate's own ``SystemExit`` would be a regression here.
        result = asyncio.run(run_migration(components=["work_packages_skeleton"]))
        assert result is not None
