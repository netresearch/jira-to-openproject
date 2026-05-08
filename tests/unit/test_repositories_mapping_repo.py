"""Unit tests for :class:`JsonFileMappingRepository` (ADR-002 phase 4a)."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import pytest

from src.domain.repositories import MappingRepository
from src.infrastructure.persistence.mapping_repo import JsonFileMappingRepository


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Per-test data directory."""
    target = tmp_path / "data"
    target.mkdir()
    return target


@pytest.fixture
def repo(data_dir: Path) -> JsonFileMappingRepository:
    """Repository pointed at the per-test data dir."""
    return JsonFileMappingRepository(data_dir=data_dir)


class TestProtocolConformance:
    """Structural conformance with the MappingRepository Protocol."""

    def test_satisfies_protocol(self, repo: JsonFileMappingRepository) -> None:
        assert isinstance(repo, MappingRepository)


class TestGet:
    """Behaviour of :meth:`JsonFileMappingRepository.get`."""

    def test_missing_file_returns_empty_dict(
        self,
        repo: JsonFileMappingRepository,
    ) -> None:
        assert repo.get("nonexistent_mapping") == {}

    def test_present_file_returns_loaded_dict(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
    ) -> None:
        payload = {"jira-1": {"openproject_id": 42}, "jira-2": {"openproject_id": 7}}
        (data_dir / "user_mapping.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        assert repo.get("user_mapping") == payload

    def test_get_caches_in_memory(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
    ) -> None:
        path = data_dir / "user_mapping.json"
        path.write_text(json.dumps({"a": 1}), encoding="utf-8")

        first = repo.get("user_mapping")
        # Mutate the on-disk file; cached call should ignore the change.
        path.write_text(json.dumps({"a": 999}), encoding="utf-8")
        second = repo.get("user_mapping")

        assert first == second == {"a": 1}

    def test_malformed_json_returns_empty_and_warns(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (data_dir / "broken_mapping.json").write_text(
            "{not valid json",
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING):
            result = repo.get("broken_mapping")

        assert result == {}
        assert any("malformed JSON" in record.message for record in caplog.records)

    def test_non_dict_top_level_returns_empty_without_warning(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """List-shaped JSON files in the data dir must not emit a WARNING.

        The mapping repository shares its data directory with raw API
        cache files (e.g. ``jira_custom_fields.json``) that are
        legitimately list-shaped.  When ``Mappings.get_all_mappings()``
        enumerates all names via ``all_names()`` it will call ``get()``
        on every stem, including those raw-cache stems.  A WARNING here
        causes a flood of 16 identical log lines on every startup.

        The correct behaviour is: return ``{}`` (already the case) and
        log only at DEBUG so operators can diagnose unexpected files
        without being spammed on normal runs.
        """
        (data_dir / "jira_custom_fields.json").write_text(
            '[{"id": 1, "name": "cf"}]',
            encoding="utf-8",
        )

        # Scope the caplog assertion to the mapping_repo logger so unrelated
        # WARNINGs from other libraries / fixtures cannot make this test flaky.
        repo_logger = "src.infrastructure.persistence.mapping_repo"
        with caplog.at_level(logging.WARNING, logger=repo_logger):
            result = repo.get("jira_custom_fields")

        assert result == {}
        warning_records = [r for r in caplog.records if r.name == repo_logger and r.levelno >= logging.WARNING]
        assert warning_records == [], (
            f"Unexpected WARNING(s) from {repo_logger}: {[r.message for r in warning_records]}"
        )

    def test_non_dict_top_level_returns_empty_and_logs_at_debug(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """List-shaped files log the shape mismatch at DEBUG, not WARNING."""
        (data_dir / "list_mapping.json").write_text("[1, 2, 3]", encoding="utf-8")

        with caplog.at_level(logging.DEBUG):
            result = repo.get("list_mapping")

        assert result == {}
        assert any("unexpected top-level shape" in record.message for record in caplog.records)


class TestSet:
    """Behaviour of :meth:`JsonFileMappingRepository.set`."""

    def test_set_then_get_in_process(
        self,
        repo: JsonFileMappingRepository,
    ) -> None:
        repo.set("user_mapping", {"jira-1": {"openproject_id": 42}})
        assert repo.get("user_mapping") == {"jira-1": {"openproject_id": 42}}

    def test_set_round_trips_through_disk(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
    ) -> None:
        payload = {"jira-1": {"openproject_id": 42}}
        repo.set("user_mapping", payload)

        # Read directly from disk via a fresh repo instance to bypass
        # the in-process cache.
        fresh = JsonFileMappingRepository(data_dir=data_dir)
        assert fresh.get("user_mapping") == payload

    def test_set_overwrites_existing(
        self,
        repo: JsonFileMappingRepository,
    ) -> None:
        repo.set("user_mapping", {"jira-1": {"openproject_id": 1}})
        repo.set("user_mapping", {"jira-2": {"openproject_id": 2}})
        assert repo.get("user_mapping") == {"jira-2": {"openproject_id": 2}}

    def test_set_does_not_alias_caller_payload(
        self,
        repo: JsonFileMappingRepository,
    ) -> None:
        payload: dict[str, int] = {"a": 1}
        repo.set("mapping", payload)
        payload["a"] = 999
        # Cached value must not reflect post-set mutations of the input.
        assert repo.get("mapping") == {"a": 1}

    def test_set_cleans_up_tempfiles(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
    ) -> None:
        repo.set("user_mapping", {"a": 1})

        # No leftover ``.tmp`` files from the atomic rename.
        leftover = [p for p in data_dir.iterdir() if p.suffix == ".tmp"]
        assert leftover == []

        # The expected file is the only artefact in the directory.
        assert {p.name for p in data_dir.iterdir()} == {"user_mapping.json"}

    def test_set_preserves_file_mode(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
    ) -> None:
        repo.set("user_mapping", {"a": 1})
        path = data_dir / "user_mapping.json"
        path.chmod(0o600)

        repo.set("user_mapping", {"a": 2})

        # Mode is mirrored from the previous file before os.replace.
        actual_mode = stat.S_IMODE(path.stat().st_mode)
        assert actual_mode == 0o600

    def test_set_creates_data_dir_if_missing(
        self,
        tmp_path: Path,
    ) -> None:
        target_dir = tmp_path / "deep" / "nested" / "data"
        # Note: directory does not exist yet.
        repo = JsonFileMappingRepository(data_dir=target_dir)
        repo.set("user_mapping", {"a": 1})

        assert target_dir.is_dir()
        assert (target_dir / "user_mapping.json").is_file()


class TestHas:
    """Behaviour of :meth:`JsonFileMappingRepository.has`."""

    def test_missing_returns_false(
        self,
        repo: JsonFileMappingRepository,
    ) -> None:
        assert repo.has("nonexistent_mapping") is False

    def test_empty_dict_returns_false(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
    ) -> None:
        (data_dir / "empty_mapping.json").write_text("{}", encoding="utf-8")
        assert repo.has("empty_mapping") is False

    def test_non_empty_returns_true(
        self,
        repo: JsonFileMappingRepository,
    ) -> None:
        repo.set("user_mapping", {"a": 1})
        assert repo.has("user_mapping") is True


class TestAllNames:
    """Behaviour of :meth:`JsonFileMappingRepository.all_names`."""

    def test_empty_directory(
        self,
        repo: JsonFileMappingRepository,
    ) -> None:
        assert repo.all_names() == []

    def test_lists_json_stems(
        self,
        repo: JsonFileMappingRepository,
        data_dir: Path,
    ) -> None:
        (data_dir / "user_mapping.json").write_text("{}", encoding="utf-8")
        (data_dir / "project_mapping.json").write_text("{}", encoding="utf-8")
        # Non-JSON files are ignored.
        (data_dir / "README.md").write_text("# notes", encoding="utf-8")

        assert repo.all_names() == ["project_mapping", "user_mapping"]

    def test_includes_in_memory_only_writes(
        self,
        repo: JsonFileMappingRepository,
    ) -> None:
        repo.set("freshly_written", {"a": 1})
        assert "freshly_written" in repo.all_names()

    def test_missing_data_dir_returns_empty(
        self,
        tmp_path: Path,
    ) -> None:
        # Directory does not exist yet; no error, just nothing to list.
        repo = JsonFileMappingRepository(data_dir=tmp_path / "ghost")
        assert repo.all_names() == []
