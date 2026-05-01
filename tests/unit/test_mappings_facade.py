"""Tests for the :class:`Mappings` facade over :class:`MappingRepository`.

Phase 4b of ADR-002 makes :class:`src.mappings.mappings.Mappings` a thin
facade over the repository introduced in PR 4a. These tests exercise the
new injection seam and verify that the legacy public API (attribute
reads, ``get_mapping`` / ``set_mapping`` / ``has_mapping`` /
``get_all_mappings``, the op-id helpers) is preserved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from src.domain.repositories import MappingRepository
from src.infrastructure.persistence.mapping_repo import JsonFileMappingRepository
from src.mappings.mappings import Mappings
from tests.utils.fake_mapping_repository import FakeMappingRepository

if TYPE_CHECKING:
    from collections.abc import Iterator


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Per-test data directory."""
    target = tmp_path / "data"
    target.mkdir()
    return target


@pytest.fixture
def fake_repo() -> FakeMappingRepository:
    """In-memory repository with no pre-population."""
    return FakeMappingRepository()


@pytest.fixture
def seeded_repo() -> FakeMappingRepository:
    """In-memory repository pre-populated with realistic payloads."""
    return FakeMappingRepository(
        initial={
            "user_mapping": {"alice": {"openproject_id": 7}},
            "project_mapping": {"PROJ": {"openproject_id": 42}},
            "issue_type_mapping": {"Bug": {"openproject_id": 3}},
            "status_mapping": {"Open": {"openproject_id": 1}},
        },
    )


# ── Construction & repository plumbing ────────────────────────────────────


class TestConstruction:
    """:meth:`Mappings.__init__` repository wiring."""

    def test_default_construction_uses_json_repo(self, data_dir: Path) -> None:
        """Without an injected repo, a JSON adapter is created at ``data_dir``."""
        m = Mappings(data_dir=data_dir)
        # Internal access is acceptable here — the test asserts on the
        # documented seam contract used by :class:`BaseMigration`.
        assert isinstance(m._repo, JsonFileMappingRepository)

    def test_injected_repo_is_used(self, fake_repo: FakeMappingRepository) -> None:
        """An injected repository replaces the default JSON adapter."""
        m = Mappings(repo=fake_repo)
        assert m._repo is fake_repo

    def test_injected_repo_satisfies_protocol(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """The injection accepts any structural :class:`MappingRepository`."""
        m = Mappings(repo=fake_repo)
        assert isinstance(m._repo, MappingRepository)

    def test_init_does_not_eagerly_load(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """Construction must not pre-populate every mapping into memory.

        Phase 4b moves to lazy reads — the override cache should only be
        populated on first access of a given mapping.
        """
        Mappings(repo=fake_repo)
        # ``_warn_missing_essentials`` calls ``has`` (cheap), not ``get``;
        # nothing should be cached just from constructing the facade.
        # Re-construct to inspect overrides without triggering reads.
        m = Mappings(repo=fake_repo)
        assert m._overrides == {}


# ── get_mapping / set_mapping / has_mapping (legacy short names) ─────────


class TestGetMapping:
    """:meth:`Mappings.get_mapping` short-name resolution."""

    def test_short_name_resolves_to_stem(
        self,
        seeded_repo: FakeMappingRepository,
    ) -> None:
        """``get_mapping("user")`` reads the ``user_mapping`` stem."""
        m = Mappings(repo=seeded_repo)
        assert m.get_mapping("user") == {"alice": {"openproject_id": 7}}

    def test_full_stem_also_works(
        self,
        seeded_repo: FakeMappingRepository,
    ) -> None:
        """Full stems pass through unchanged for back-compat."""
        m = Mappings(repo=seeded_repo)
        assert m.get_mapping("user_mapping") == {"alice": {"openproject_id": 7}}

    def test_missing_mapping_returns_empty_dict(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """Missing names yield ``{}`` — no exception, matches legacy."""
        m = Mappings(repo=fake_repo)
        assert m.get_mapping("user") == {}

    def test_returns_stable_identity(
        self,
        seeded_repo: FakeMappingRepository,
    ) -> None:
        """Repeated calls return the same dict so in-place mutation is visible.

        Legacy callers do ``m.get_mapping("project")["X"] = {...}`` and
        expect the next call to see the new key.
        """
        m = Mappings(repo=seeded_repo)
        first = m.get_mapping("project")
        first["NEW_KEY"] = {"openproject_id": 99}
        second = m.get_mapping("project")
        assert second["NEW_KEY"] == {"openproject_id": 99}

    def test_unknown_short_name_falls_through_to_stem(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """Names not in the resolution table get a ``"_mapping"`` suffix."""
        fake_repo.set("custom_thing_mapping", {"k": "v"})
        m = Mappings(repo=fake_repo)
        assert m.get_mapping("custom_thing") == {"k": "v"}

    def test_path_constant_resolves_to_stem(
        self,
        seeded_repo: FakeMappingRepository,
    ) -> None:
        """The legacy ``Mappings.*_FILE`` :class:`Path` constants are accepted.

        ``account_migration.py`` and friends pass these directly into
        ``get_mapping``; the resolver must strip the ``.json`` suffix.
        """
        m = Mappings(repo=seeded_repo)
        assert m.get_mapping(Mappings.USER_MAPPING_FILE) == {
            "alice": {"openproject_id": 7},
        }

    def test_filename_string_resolves_to_stem(
        self,
        seeded_repo: FakeMappingRepository,
    ) -> None:
        """A ``"foo_mapping.json"`` filename string also works."""
        m = Mappings(repo=seeded_repo)
        assert m.get_mapping("user_mapping.json") == {
            "alice": {"openproject_id": 7},
        }


class TestSetMapping:
    """:meth:`Mappings.set_mapping` writes to repo + cache."""

    def test_set_mapping_routes_to_repository(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        m = Mappings(repo=fake_repo)
        m.set_mapping("project", {"PROJ-1": {"openproject_id": 7}})
        assert fake_repo.get("project_mapping") == {"PROJ-1": {"openproject_id": 7}}

    def test_set_mapping_updates_cached_view(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """After ``set_mapping``, ``get_mapping`` returns the new payload.

        Important: this must hold even if ``get_mapping`` was called
        beforehand (priming the override cache with the empty default).
        """
        m = Mappings(repo=fake_repo)
        # Prime the cache with the empty default.
        assert m.get_mapping("project") == {}
        m.set_mapping("project", {"PROJ-1": {"openproject_id": 7}})
        assert m.get_mapping("project") == {"PROJ-1": {"openproject_id": 7}}

    def test_set_mapping_updates_attribute_view(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """Legacy attribute reads see what ``set_mapping`` just wrote."""
        m = Mappings(repo=fake_repo)
        m.set_mapping("user", {"u": {"openproject_id": 1}})
        assert m.user_mapping == {"u": {"openproject_id": 1}}


class TestHasMapping:
    """:meth:`Mappings.has_mapping` short-name resolution."""

    def test_has_mapping_present(
        self,
        seeded_repo: FakeMappingRepository,
    ) -> None:
        m = Mappings(repo=seeded_repo)
        assert m.has_mapping("user") is True

    def test_has_mapping_missing(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        m = Mappings(repo=fake_repo)
        assert m.has_mapping("user") is False

    def test_has_mapping_empty_override_is_false(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """In-memory override of an empty dict counts as absent.

        The legacy implementation used ``bool(getattr(self, attr))``;
        empty dicts were falsy, so a fresh in-memory ``{}`` should still
        report ``False``.
        """
        m = Mappings(repo=fake_repo)
        m.user_mapping = {}  # in-memory only, no disk write
        assert m.has_mapping("user") is False


# ── Op-id helpers (kept on facade per ADR) ───────────────────────────────


class TestOpIdHelpers:
    """Op-id helpers compose repository reads with attribute lookups."""

    def test_get_op_project_id(self, seeded_repo: FakeMappingRepository) -> None:
        m = Mappings(repo=seeded_repo)
        assert m.get_op_project_id("PROJ") == 42

    def test_get_op_project_id_missing(
        self,
        seeded_repo: FakeMappingRepository,
    ) -> None:
        m = Mappings(repo=seeded_repo)
        assert m.get_op_project_id("does-not-exist") is None

    def test_get_op_user_id(self, seeded_repo: FakeMappingRepository) -> None:
        m = Mappings(repo=seeded_repo)
        assert m.get_op_user_id("alice") == 7

    def test_get_op_type_id(self, seeded_repo: FakeMappingRepository) -> None:
        m = Mappings(repo=seeded_repo)
        assert m.get_op_type_id("Bug") == 3

    def test_get_op_status_id(self, seeded_repo: FakeMappingRepository) -> None:
        m = Mappings(repo=seeded_repo)
        assert m.get_op_status_id("Open") == 1


# ── get_all_mappings ─────────────────────────────────────────────────────


class TestGetAllMappings:
    """:meth:`Mappings.get_all_mappings` includes essentials + extras."""

    def test_includes_legacy_essentials_even_when_missing(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """Empty essentials show up as ``{}`` rather than disappearing."""
        m = Mappings(repo=fake_repo)
        result = m.get_all_mappings()
        # Every entry from the legacy hardcoded list is present.
        assert "user_mapping" in result
        assert "project_mapping" in result
        assert "issue_type_mapping" in result
        assert "work_package_mapping" in result
        # Each value is at least an empty dict.
        assert all(isinstance(v, dict) for v in result.values())

    def test_includes_repository_extras(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """Names known to the repo but not the legacy list are included."""
        fake_repo.set("custom_extra_mapping", {"k": "v"})
        m = Mappings(repo=fake_repo)
        result = m.get_all_mappings()
        assert result["custom_extra_mapping"] == {"k": "v"}

    def test_reflects_seeded_payloads(
        self,
        seeded_repo: FakeMappingRepository,
    ) -> None:
        m = Mappings(repo=seeded_repo)
        result = m.get_all_mappings()
        assert result["user_mapping"] == {"alice": {"openproject_id": 7}}
        assert result["project_mapping"] == {"PROJ": {"openproject_id": 42}}


# ── Legacy attribute access semantics ────────────────────────────────────


class TestAttributeAccess:
    """Property descriptors preserving the legacy attribute API."""

    def test_attribute_read_lazy_loads_from_repo(
        self,
        seeded_repo: FakeMappingRepository,
    ) -> None:
        m = Mappings(repo=seeded_repo)
        assert m.user_mapping == {"alice": {"openproject_id": 7}}

    def test_attribute_assign_does_not_persist(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        """``self.x_mapping = v`` updates memory only, mirroring legacy.

        Only :meth:`set_mapping` is a persistent operation; bare
        assignment was always an in-memory override in the legacy
        implementation.
        """
        m = Mappings(repo=fake_repo)
        m.user_mapping = {"alice": {"openproject_id": 7}}
        # Visible to subsequent reads…
        assert m.user_mapping == {"alice": {"openproject_id": 7}}
        # …but not in the repo.
        assert fake_repo.get("user_mapping") == {}

    def test_dict_style_get(self, seeded_repo: FakeMappingRepository) -> None:
        m = Mappings(repo=seeded_repo)
        assert m["user_mapping"] == {"alice": {"openproject_id": 7}}

    def test_dict_style_set(self, fake_repo: FakeMappingRepository) -> None:
        m = Mappings(repo=fake_repo)
        m["user_mapping"] = {"alice": {"openproject_id": 1}}
        assert m.user_mapping == {"alice": {"openproject_id": 1}}

    def test_dict_style_get_missing_raises(
        self,
        fake_repo: FakeMappingRepository,
    ) -> None:
        m = Mappings(repo=fake_repo)
        with pytest.raises(KeyError):
            _ = m["definitely_unknown_attr"]


# ── Default JSON adapter round-trip (no injection) ───────────────────────


class TestJsonAdapterRoundTrip:
    """:class:`Mappings` with the default JSON adapter still round-trips.

    Sanity check that the facade-without-injection path mirrors the old
    behaviour from ``test_mappings_controller``.
    """

    def test_set_mapping_writes_to_disk(self, data_dir: Path) -> None:
        m = Mappings(data_dir=data_dir)
        payload = {"PROJ": {"openproject_id": 1}}
        m.set_mapping("project", payload)
        on_disk = json.loads((data_dir / "project_mapping.json").read_text())
        assert on_disk == payload


# ── Essentials warning ───────────────────────────────────────────────────


class TestEssentialsWarning:
    """:meth:`_warn_missing_essentials` mirrors the legacy ``__init__`` notice."""

    def test_warns_when_essentials_absent(
        self,
        fake_repo: FakeMappingRepository,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The notice level is custom; capture at INFO so we always see it.
        with caplog.at_level("INFO"):
            Mappings(repo=fake_repo)
        # We do not pin the exact log level (legacy used ``logger.notice``);
        # we only assert that the warning text is emitted.
        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "project_mapping" in messages.lower() or "project mapping" in messages.lower()

    def test_no_warn_when_essentials_present(
        self,
        seeded_repo: FakeMappingRepository,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("INFO"):
            Mappings(repo=seeded_repo)
        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "missing or empty" not in messages


# ── BaseMigration injection seam (the demo) ──────────────────────────────


class TestBaseMigrationInjection:
    """The new ``mapping_repo=`` kwarg on migrations bypasses the proxy."""

    @pytest.fixture
    def _quiet_clients(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Iterator[None]:
        """Stub out the heavy clients so migrations construct cleanly.

        ``BaseMigration.__init__`` instantiates ``JiraClient`` /
        ``OpenProjectClient`` on the fall-through paths; in unit tests
        we always pass explicit dummies and never want the real
        constructors firing. The fixture asserts that path is taken.
        """
        # ``BaseMigration`` does ``from src.infrastructure... import JiraClient/
        # OpenProjectClient`` so the local binding is what
        # ``self.jira_client = jira_client or JiraClient()`` resolves.
        # Patching the source modules wouldn't intercept those calls;
        # patch the symbols on ``base_migration`` itself.
        from src.application.components import base_migration as bm_mod

        def _boom(*_a: object, **_k: object) -> None:
            msg = "real client constructed in unit test"
            raise AssertionError(msg)

        monkeypatch.setattr(bm_mod, "JiraClient", _boom)
        monkeypatch.setattr(bm_mod, "OpenProjectClient", _boom)
        return

    def test_priority_migration_reads_from_injected_repo(
        self,
        _quiet_clients: None,
    ) -> None:
        """``PriorityMigration`` constructed with a fake repo reads from it.

        This is the proof-of-concept for the phase-4b seam: tests no
        longer need ``monkeypatch.setattr(cfg, "mappings", DummyMappings())``
        to wire mapping data into a migration.
        """
        from src.application.components.priority_migration import PriorityMigration

        fake = FakeMappingRepository(
            initial={
                "work_package_mapping": {"PROJ-1": {"openproject_id": 7}},
            },
        )

        # Minimal client doubles so ``BaseMigration.__init__`` does not
        # try to connect to anything.
        class _Jira:
            pass

        class _Op:
            pass

        mig = PriorityMigration(
            jira_client=_Jira(),  # type: ignore[arg-type]
            op_client=_Op(),  # type: ignore[arg-type]
            mapping_repo=fake,
        )

        # The seam: ``self._mapping_repo`` IS the injected fake.
        assert mig._mapping_repo is fake
        assert mig._mapping_repo.get("work_package_mapping") == {
            "PROJ-1": {"openproject_id": 7},
        }

    def test_labels_migration_accepts_injected_repo(
        self,
        _quiet_clients: None,
    ) -> None:
        """``LabelsMigration`` exposes the same seam."""
        from src.application.components.labels_migration import LabelsMigration

        fake = FakeMappingRepository(
            initial={"work_package_mapping": {"J1": {"openproject_id": 99}}},
        )

        class _Jira:
            pass

        class _Op:
            pass

        mig = LabelsMigration(
            jira_client=_Jira(),  # type: ignore[arg-type]
            op_client=_Op(),  # type: ignore[arg-type]
            mapping_repo=fake,
        )

        assert mig._mapping_repo is fake

    def test_falls_back_when_proxy_lacks_repo(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _quiet_clients: None,
    ) -> None:
        """Tests that monkeypatch ``cfg.mappings`` with a Dummy keep working.

        The Dummy will not have ``_repo``, so :class:`BaseMigration`
        falls back to a fresh JSON adapter rooted at ``data_dir``. This
        is the safety net described in the phase-4b plan.
        """
        from src import config as cfg
        from src.application.components.priority_migration import PriorityMigration

        # Redirect ``data_dir`` to a temp tree so the fall-back JSON
        # adapter writes nowhere interesting.
        monkeypatch.setattr(cfg, "var_dirs", {**cfg.var_dirs, "data": tmp_path})

        class DummyMappings:
            def get_mapping(self, _name: str) -> dict[str, object]:
                return {}

        monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)

        class _Jira:
            pass

        class _Op:
            pass

        mig = PriorityMigration(
            jira_client=_Jira(),  # type: ignore[arg-type]
            op_client=_Op(),  # type: ignore[arg-type]
        )

        # No injection, no _repo on the Dummy → fresh JSON adapter.
        assert isinstance(mig._mapping_repo, JsonFileMappingRepository)
