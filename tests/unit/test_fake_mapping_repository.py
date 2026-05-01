"""Unit tests for :class:`FakeMappingRepository` (ADR-002 phase 4a)."""

from __future__ import annotations

from src.domain.repositories import MappingRepository
from tests.utils.fake_mapping_repository import FakeMappingRepository


class TestProtocolConformance:
    """Structural conformance with the MappingRepository Protocol."""

    def test_satisfies_protocol(self) -> None:
        fake = FakeMappingRepository()
        assert isinstance(fake, MappingRepository)


class TestConstruction:
    """Behaviour of :meth:`FakeMappingRepository.__init__`."""

    def test_empty_default(self) -> None:
        fake = FakeMappingRepository()
        assert fake.all_names() == []
        assert fake.get("anything") == {}

    def test_initial_state(self) -> None:
        fake = FakeMappingRepository(
            initial={
                "user_mapping": {"jira-1": {"openproject_id": 42}},
                "project_mapping": {"ABC": {"openproject_id": 7}},
            },
        )
        assert fake.get("user_mapping") == {"jira-1": {"openproject_id": 42}}
        assert fake.get("project_mapping") == {"ABC": {"openproject_id": 7}}

    def test_initial_is_defensively_copied(self) -> None:
        seed: dict[str, dict[str, int]] = {"user_mapping": {"a": 1}}
        fake = FakeMappingRepository(initial=seed)
        # Mutating the seed must not bleed into the fake's store.
        seed["user_mapping"]["a"] = 999
        assert fake.get("user_mapping") == {"a": 1}


class TestGet:
    """Behaviour of :meth:`FakeMappingRepository.get`."""

    def test_missing_returns_empty_dict(self) -> None:
        fake = FakeMappingRepository()
        assert fake.get("nonexistent") == {}

    def test_present_returns_stored_value(self) -> None:
        fake = FakeMappingRepository(initial={"user_mapping": {"a": 1}})
        assert fake.get("user_mapping") == {"a": 1}


class TestSet:
    """Behaviour of :meth:`FakeMappingRepository.set`."""

    def test_set_stores_value(self) -> None:
        fake = FakeMappingRepository()
        fake.set("user_mapping", {"a": 1})
        assert fake.get("user_mapping") == {"a": 1}

    def test_set_overwrites_existing(self) -> None:
        fake = FakeMappingRepository(initial={"user_mapping": {"a": 1}})
        fake.set("user_mapping", {"b": 2})
        assert fake.get("user_mapping") == {"b": 2}

    def test_set_does_not_alias_input(self) -> None:
        fake = FakeMappingRepository()
        payload: dict[str, int] = {"a": 1}
        fake.set("user_mapping", payload)
        payload["a"] = 999
        assert fake.get("user_mapping") == {"a": 1}


class TestHas:
    """Behaviour of :meth:`FakeMappingRepository.has`."""

    def test_missing_returns_false(self) -> None:
        fake = FakeMappingRepository()
        assert fake.has("nonexistent") is False

    def test_empty_dict_returns_false(self) -> None:
        fake = FakeMappingRepository(initial={"empty_mapping": {}})
        assert fake.has("empty_mapping") is False

    def test_non_empty_returns_true(self) -> None:
        fake = FakeMappingRepository(initial={"user_mapping": {"a": 1}})
        assert fake.has("user_mapping") is True


class TestAllNames:
    """Behaviour of :meth:`FakeMappingRepository.all_names`."""

    def test_empty(self) -> None:
        fake = FakeMappingRepository()
        assert fake.all_names() == []

    def test_sorted(self) -> None:
        fake = FakeMappingRepository(
            initial={
                "user_mapping": {"a": 1},
                "project_mapping": {"b": 2},
                "issue_type_mapping": {"c": 3},
            },
        )
        assert fake.all_names() == [
            "issue_type_mapping",
            "project_mapping",
            "user_mapping",
        ]


class TestSetAll:
    """Behaviour of :meth:`FakeMappingRepository.set_all`."""

    def test_replaces_entire_store(self) -> None:
        fake = FakeMappingRepository(
            initial={"user_mapping": {"a": 1}, "stale": {"x": 0}},
        )
        fake.set_all(
            {
                "user_mapping": {"b": 2},
                "project_mapping": {"c": 3},
            },
        )
        assert fake.all_names() == ["project_mapping", "user_mapping"]
        assert fake.get("user_mapping") == {"b": 2}
        assert fake.get("project_mapping") == {"c": 3}
        # Stale key was dropped, not merged.
        assert fake.get("stale") == {}

    def test_set_all_does_not_alias_input(self) -> None:
        fake = FakeMappingRepository()
        seed: dict[str, dict[str, int]] = {"user_mapping": {"a": 1}}
        fake.set_all(seed)
        seed["user_mapping"]["a"] = 999
        assert fake.get("user_mapping") == {"a": 1}

    def test_set_all_empty_clears_store(self) -> None:
        fake = FakeMappingRepository(initial={"user_mapping": {"a": 1}})
        fake.set_all({})
        assert fake.all_names() == []
