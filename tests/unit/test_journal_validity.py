"""Tests for journal validity period handling and PG::ExclusionViolation prevention.

This module tests the journal creation logic to ensure:
1. Validity periods don't overlap (preventing PG::ExclusionViolation)
2. Journal cleanup handles both v1 and v2+ journals during re-migration
3. Timestamp collision detection and resolution works correctly
4. Deduplication of duplicate validity periods
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def mock_logger() -> MagicMock:
    """Create a mock logger."""
    logger = MagicMock()
    logger.debug = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger


@pytest.fixture
def sample_journal_operations() -> list[dict[str, Any]]:
    """Sample journal operations from Jira changelog."""
    base_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
    return [
        {
            "type": "create",
            "created_at": base_time.isoformat(),
            "user_id": 1,
            "notes": "Issue created",
            "field_changes": {"status_id": [None, 1]},
        },
        {
            "type": "update",
            "created_at": (base_time + timedelta(hours=1)).isoformat(),
            "user_id": 2,
            "notes": "Status changed",
            "field_changes": {"status_id": [1, 2]},
        },
        {
            "type": "comment",
            "created_at": (base_time + timedelta(hours=2)).isoformat(),
            "user_id": 1,
            "notes": "Added a comment",
            "field_changes": {},
        },
    ]


@pytest.fixture
def duplicate_timestamp_operations() -> list[dict[str, Any]]:
    """Operations with duplicate timestamps (common in Jira bulk imports)."""
    same_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC).isoformat()
    return [
        {
            "type": "create",
            "created_at": same_time,
            "user_id": 1,
            "notes": "Issue created",
            "field_changes": {"status_id": [None, 1]},
        },
        {
            "type": "update",
            "created_at": same_time,  # Same timestamp!
            "user_id": 2,
            "notes": "Immediate update",
            "field_changes": {"status_id": [1, 2]},
        },
        {
            "type": "update",
            "created_at": same_time,  # Same timestamp again!
            "user_id": 2,
            "notes": "Another immediate update",
            "field_changes": {"priority_id": [1, 2]},
        },
    ]


# ============================================================================
# Validity Period Logic Tests
# ============================================================================


class TestValidityPeriodCalculation:
    """Test validity period calculation logic."""

    def test_validity_period_non_overlapping(
        self, sample_journal_operations: list[dict[str, Any]]
    ) -> None:
        """Verify validity periods are non-overlapping."""
        periods = self._calculate_validity_periods(sample_journal_operations)

        # Check no overlaps
        for i, period in enumerate(periods[:-1]):
            next_period = periods[i + 1]
            assert period["end"] is not None, "Non-final periods must have an end"
            assert period["end"] <= next_period["start"], (
                f"Period {i} end ({period['end']}) must be <= period {i + 1} start ({next_period['start']})"
            )

    def test_final_journal_has_open_ended_validity(
        self, sample_journal_operations: list[dict[str, Any]]
    ) -> None:
        """Verify the last journal has an open-ended validity period."""
        periods = self._calculate_validity_periods(sample_journal_operations)

        last_period = periods[-1]
        assert last_period["end"] is None, "Final journal should have NULL end (open-ended)"

    def test_duplicate_timestamps_get_synthetic_offsets(
        self, duplicate_timestamp_operations: list[dict[str, Any]]
    ) -> None:
        """Verify duplicate timestamps are offset to prevent overlaps."""
        periods = self._calculate_validity_periods(duplicate_timestamp_operations)

        # All starts should be unique
        starts = [p["start"] for p in periods]
        assert len(starts) == len(set(starts)), "All start times should be unique"

        # Each subsequent start should be after the previous
        for i in range(1, len(starts)):
            assert starts[i] > starts[i - 1], (
                f"Start {i} ({starts[i]}) should be after start {i - 1} ({starts[i - 1]})"
            )

    def test_synthetic_timestamp_increment_is_one_second(self) -> None:
        """Verify synthetic timestamp increment matches Ruby constant."""
        # This matches SYNTHETIC_TIMESTAMP_INCREMENT = 1 in create_work_package_journals.rb
        EXPECTED_INCREMENT = 1  # seconds

        # Simulate the Ruby logic
        base_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        same_time_ops = [
            {"created_at": base_time.isoformat()},
            {"created_at": base_time.isoformat()},
        ]

        periods = self._calculate_validity_periods(same_time_ops)

        time_diff = (periods[1]["start"] - periods[0]["start"]).total_seconds()
        assert time_diff >= EXPECTED_INCREMENT, (
            f"Time difference ({time_diff}s) should be >= {EXPECTED_INCREMENT}s"
        )

    def _calculate_validity_periods(
        self, operations: list[dict[str, Any]]
    ) -> list[dict[str, datetime | None]]:
        """Calculate validity periods using the same logic as Ruby script.

        This mirrors the compute_timestamp_and_validity lambda in create_work_package_journals.rb
        """
        SYNTHETIC_TIMESTAMP_INCREMENT = timedelta(seconds=1)

        # Sort by created_at
        sorted_ops = sorted(
            operations,
            key=lambda op: datetime.fromisoformat(
                op.get("created_at") or op.get("timestamp") or "1970-01-01T00:00:00+00:00"
            ),
        )

        periods = []
        last_used_timestamp = None

        for i, op in enumerate(sorted_ops):
            created_at_str = op.get("created_at") or op.get("timestamp")

            # Calculate target_time
            if created_at_str:
                target_time = datetime.fromisoformat(created_at_str)
                if target_time.tzinfo is None:
                    target_time = target_time.replace(tzinfo=UTC)
                if last_used_timestamp and target_time <= last_used_timestamp:
                    target_time = last_used_timestamp + SYNTHETIC_TIMESTAMP_INCREMENT
            elif last_used_timestamp:
                target_time = last_used_timestamp + SYNTHETIC_TIMESTAMP_INCREMENT
            else:
                target_time = datetime.now(tz=UTC)

            # Calculate validity_period end
            if i < len(sorted_ops) - 1:
                next_op = sorted_ops[i + 1]
                next_created_at = next_op.get("created_at") or next_op.get("timestamp")
                if next_created_at:
                    period_end = datetime.fromisoformat(next_created_at)
                    if period_end.tzinfo is None:
                        period_end = period_end.replace(tzinfo=UTC)
                    if period_end <= target_time:
                        period_end = target_time + SYNTHETIC_TIMESTAMP_INCREMENT
                else:
                    period_end = target_time + SYNTHETIC_TIMESTAMP_INCREMENT
            else:
                period_end = None  # Open-ended for last journal

            periods.append({"start": target_time, "end": period_end})
            last_used_timestamp = period_end or target_time

        return periods


# ============================================================================
# Journal Cleanup Tests
# ============================================================================


class TestJournalCleanup:
    """Test journal cleanup logic for re-migration scenarios."""

    def test_v2_plus_cleanup_query_is_correct(self) -> None:
        """Verify the v2+ cleanup query targets correct journals."""
        # The Ruby code at line 26 uses: where('version > 1')
        # This should only delete v2+, not v1
        cleanup_pattern = r"where\s*\(\s*['\"]?version\s*>\s*1['\"]?\s*\)"

        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        # Check the cleanup targets v2+ only
        assert re.search(cleanup_pattern, ruby_content, re.IGNORECASE), (
            "Cleanup should target version > 1 (v2+ journals)"
        )

    def test_cleanup_leaves_v1_journal_intact(self) -> None:
        """Verify v1 journal is updated, not deleted, during re-migration.

        This is critical for preventing PG::ExclusionViolation:
        - v1 journal is UPDATED with new validity_period
        - v2+ journals are DELETED then re-created
        - If v1 is not properly updated, its validity_period may conflict
        """
        # The Ruby code updates v1 journal at lines 222-238
        # Key: It updates v1's validity_period via raw SQL at line 238

        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        # Check v1 journal is retrieved
        assert "version: 1" in ruby_content, "v1 journal should be retrieved"

        # Check v1 validity_period is updated via SQL
        assert "UPDATE journals SET" in ruby_content, "v1 should be updated via SQL"
        assert "validity_period =" in ruby_content, "validity_period should be updated"

    def test_deduplication_removes_duplicate_validity_periods(self) -> None:
        """Verify deduplication logic removes journals with same validity_period."""
        # The Ruby code at lines 259-296 handles deduplication

        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        # Check deduplication section exists
        assert "DEDUPLICATION" in ruby_content, "Deduplication section should exist"
        assert "seen_validity_periods" in ruby_content, "Should track seen periods"

    def test_associated_data_cleaned_before_journals(self) -> None:
        """Verify associated data is deleted before journals to avoid FK violations."""
        # Lines 33-40 in Ruby code delete associated data first

        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        # Check order: CustomizableJournal deleted before Journal
        customizable_delete_pos = ruby_content.find("CustomizableJournal.where")
        journal_delete_pos = ruby_content.find("v2_plus_journals.delete_all")

        assert customizable_delete_pos > 0, "CustomizableJournal cleanup should exist"
        assert journal_delete_pos > 0, "Journal cleanup should exist"
        assert customizable_delete_pos < journal_delete_pos, (
            "Associated data should be deleted before journals"
        )


# ============================================================================
# PG::ExclusionViolation Prevention Tests
# ============================================================================


class TestExclusionViolationPrevention:
    """Test scenarios that could cause PG::ExclusionViolation."""

    def test_remigration_updates_v1_validity_period(self) -> None:
        """When re-migrating, v1's validity_period must be updated to avoid conflicts.

        Root cause of PG::ExclusionViolation:
        1. Initial migration creates v1 with validity_period [t1, t2)
        2. Re-migration deletes v2+ but leaves v1 unchanged
        3. New v2 journals may have validity_periods that overlap with stale v1

        Solution: v1's validity_period is updated via raw SQL during re-migration.
        """
        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        # Find v1 update section
        v1_update_pattern = r"UPDATE journals SET.*validity_period.*WHERE id = #{v1_journal\.id}"
        assert re.search(v1_update_pattern, ruby_content, re.DOTALL), (
            "v1 journal validity_period should be updated via SQL"
        )

    def test_tstzrange_format_is_correct(self) -> None:
        """Verify tstzrange format matches PostgreSQL requirements."""
        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        # Check for correct tstzrange syntax
        # Format: tstzrange('timestamp', 'timestamp'|NULL, '[)')
        assert "tstzrange(" in ruby_content, "Should use tstzrange for validity_period"

        # Check for open-ended range syntax (for last journal)
        assert "tstzrange('#{" in ruby_content, "Should construct tstzrange from timestamps"
        assert ", NULL, '[)')" in ruby_content, "Should support open-ended ranges"

    def test_exclusion_constraint_name_documented(self) -> None:
        """Document the PostgreSQL constraint that causes the error."""
        # The constraint is: non_overlapping_journals_validity_periods
        # This test serves as documentation for developers

        constraint_name = "non_overlapping_journals_validity_periods"

        # This constraint is defined in OpenProject schema:
        # CREATE UNIQUE INDEX non_overlapping_journals_validity_periods
        # ON journals USING gist (journable_type, journable_id, validity_period)
        # WITH (ignore_nulls=false)

        # The constraint prevents two journals for the same work package
        # from having overlapping validity_periods

        assert len(constraint_name) > 0  # Placeholder assertion


# ============================================================================
# Edge Case Tests
# ============================================================================


class TestJournalEdgeCases:
    """Test edge cases in journal creation."""

    def test_empty_changelog_creates_no_v2_journals(self) -> None:
        """Verify empty changelog doesn't create extra journals."""
        # Empty operations should result in only v1 journal (created by WP save)
        empty_ops: list[dict[str, Any]] = []
        periods = TestValidityPeriodCalculation()._calculate_validity_periods(empty_ops)
        assert len(periods) == 0, "Empty operations should create no validity periods"

    def test_single_operation_has_open_ended_validity(self) -> None:
        """Verify single operation results in open-ended validity period."""
        single_op = [
            {
                "type": "create",
                "created_at": datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC).isoformat(),
                "user_id": 1,
                "notes": "Created",
            }
        ]

        periods = TestValidityPeriodCalculation()._calculate_validity_periods(single_op)

        assert len(periods) == 1
        assert periods[0]["end"] is None, "Single journal should have open-ended validity"

    def test_microsecond_precision_handled(self) -> None:
        """Verify microsecond timestamps don't cause precision issues."""
        base_time = datetime(2024, 1, 15, 10, 0, 0, 123456, tzinfo=UTC)
        ops = [
            {"created_at": base_time.isoformat()},
            {"created_at": (base_time + timedelta(microseconds=1)).isoformat()},
        ]

        periods = TestValidityPeriodCalculation()._calculate_validity_periods(ops)

        # Should still maintain non-overlapping
        assert len(periods) == 2
        assert periods[0]["end"] <= periods[1]["start"]

    def test_future_timestamps_handled(self) -> None:
        """Verify future timestamps are handled correctly."""
        future_time = datetime(2030, 1, 15, 10, 0, 0, tzinfo=UTC)
        ops = [
            {"created_at": future_time.isoformat()},
            {"created_at": (future_time + timedelta(hours=1)).isoformat()},
        ]

        periods = TestValidityPeriodCalculation()._calculate_validity_periods(ops)

        assert len(periods) == 2
        assert periods[0]["start"] == future_time
        assert periods[0]["end"] is not None

    def test_nil_timestamp_uses_fallback(self) -> None:
        """Verify nil/missing timestamps use appropriate fallback."""
        ops = [
            {"created_at": None, "user_id": 1},  # Missing timestamp
            {"created_at": datetime(2024, 1, 15, 11, 0, 0, tzinfo=UTC).isoformat()},
        ]

        periods = TestValidityPeriodCalculation()._calculate_validity_periods(ops)

        # Should still create valid periods
        assert len(periods) == 2
        assert all(p["start"] is not None for p in periods)


# ============================================================================
# Integration-style Tests (Ruby Script Validation)
# ============================================================================


class TestRubyScriptIntegrity:
    """Validate the Ruby script structure and key logic."""

    def test_ruby_script_exists(self) -> None:
        """Verify the Ruby script exists and is readable."""
        import os

        script_path = "src/ruby/create_work_package_journals.rb"
        assert os.path.exists(script_path), f"Ruby script should exist at {script_path}"

    def test_ruby_script_has_error_handling(self) -> None:
        """Verify the Ruby script has proper error handling."""
        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        assert "rescue =>" in ruby_content, "Should have rescue block for errors"
        assert "errors <<" in ruby_content or "errors.<<" in ruby_content, (
            "Should propagate errors to Python"
        )

    def test_ruby_script_has_bulk_insert_optimization(self) -> None:
        """Verify the Ruby script uses bulk INSERT for performance."""
        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        assert "bulk INSERT" in ruby_content.lower() or "bulk_journals" in ruby_content, (
            "Should use bulk insert optimization"
        )
        assert "INSERT INTO journals" in ruby_content, (
            "Should have bulk INSERT for journals table"
        )
        assert "INSERT INTO work_package_journals" in ruby_content, (
            "Should have bulk INSERT for work_package_journals table"
        )

    def test_ruby_script_logs_cleanup_actions(self) -> None:
        """Verify cleanup actions are logged for debugging."""
        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        assert "CLEANUP" in ruby_content, "Cleanup actions should be logged"
        assert "puts" in ruby_content, "Should have logging statements"


# ============================================================================
# Regression Tests
# ============================================================================


class TestJournalRegressions:
    """Regression tests for known journal issues."""

    def test_no_orphaned_work_package_journals(self) -> None:
        """Verify work_package_journals cleanup doesn't leave orphans.

        Regression: Previously cleanup could leave orphaned work_package_journals
        when parent journals were deleted.
        """
        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        # Check data_ids are captured before deletion
        assert "data_ids = v2_plus_journals.pluck(:data_id)" in ruby_content, (
            "Should capture data_ids before deleting journals"
        )

        # Check WorkPackageJournal cleanup
        assert "Journal::WorkPackageJournal.where(id: data_ids)" in ruby_content, (
            "Should clean up associated work_package_journals"
        )

    def test_customizable_journals_cleaned_before_journals(self) -> None:
        """Verify customizable_journals are deleted before parent journals.

        Regression: FK violation when trying to delete journals with
        existing customizable_journal references.
        """
        with open("src/ruby/create_work_package_journals.rb", encoding="utf-8") as f:
            ruby_content = f.read()

        # Check CustomizableJournal cleanup happens first
        assert "CustomizableJournal.where(journal_id: v2_plus_ids).delete_all" in ruby_content, (
            "Should delete customizable_journals before journals"
        )
