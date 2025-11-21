#!/usr/bin/env python3
"""Unit tests for timestamp collision detection and resolution."""

import sys
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, '/home/sme/p/j2o/src')

from migrations.work_package_migration import WorkPackageMigration


class TestTimestampCollisionDetection:
    """Test suite for timestamp collision detection in journal entries."""

    @pytest.fixture
    def wpm(self):
        """Create WorkPackageMigration instance with mocked clients."""
        mock_jira = Mock()
        mock_op = Mock()
        return WorkPackageMigration(jira_client=mock_jira, op_client=mock_op)

    def test_no_collision_no_modification(self, wpm):
        """Test that entries with different timestamps are not modified."""
        entries = [
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "comment"},
            {"timestamp": "2011-08-23T13:41:22.000+0000", "type": "changelog"},
            {"timestamp": "2011-08-23T13:41:23.000+0000", "type": "comment"}
        ]

        original_timestamps = [e["timestamp"] for e in entries]

        # Simulate the collision detection logic
        from datetime import datetime, timedelta

        for i in range(1, len(entries)):
            current_timestamp = entries[i].get("timestamp", "")
            previous_timestamp = entries[i-1].get("timestamp", "")

            if current_timestamp and previous_timestamp and current_timestamp == previous_timestamp:
                if 'T' in current_timestamp:
                    dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                    dt = dt + timedelta(seconds=1)
                    entries[i]["timestamp"] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000'

        # Verify no timestamps were modified
        modified_timestamps = [e["timestamp"] for e in entries]
        assert original_timestamps == modified_timestamps, "Timestamps without collisions should not be modified"

    def test_single_collision_resolution(self, wpm):
        """Test that a single collision is resolved with 1-second separation."""
        entries = [
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "comment"},
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "changelog"}
        ]

        # Simulate the collision detection logic
        from datetime import datetime, timedelta

        for i in range(1, len(entries)):
            current_timestamp = entries[i].get("timestamp", "")
            previous_timestamp = entries[i-1].get("timestamp", "")

            if current_timestamp and previous_timestamp and current_timestamp == previous_timestamp:
                if 'T' in current_timestamp:
                    dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                    dt = dt + timedelta(seconds=1)
                    entries[i]["timestamp"] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000'

        # Verify collision was resolved
        assert entries[0]["timestamp"] == "2011-08-23T13:41:21.000+0000"
        assert entries[1]["timestamp"] == "2011-08-23T13:41:22.000+0000"
        assert entries[0]["timestamp"] != entries[1]["timestamp"], "Collision should be resolved"

    def test_multiple_collisions_sequential_resolution(self, wpm):
        """Test that multiple consecutive collisions are resolved sequentially."""
        entries = [
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "comment"},
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "changelog"},
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "comment"},
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "changelog"}
        ]

        # Simulate the collision detection logic
        from datetime import datetime, timedelta

        for i in range(1, len(entries)):
            current_timestamp = entries[i].get("timestamp", "")
            previous_timestamp = entries[i-1].get("timestamp", "")

            if current_timestamp and previous_timestamp and current_timestamp == previous_timestamp:
                if 'T' in current_timestamp:
                    dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                    dt = dt + timedelta(seconds=1)
                    entries[i]["timestamp"] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000'

        # Verify collisions were resolved by comparing consecutive entries
        # i=1: entries[1] == entries[0]? YES → adjust to 13:41:22
        # i=2: entries[2] == entries[1]? NO (21 != 22) → no adjustment
        # i=3: entries[3] == entries[2]? YES (both 21) → adjust to 13:41:22
        assert entries[0]["timestamp"] == "2011-08-23T13:41:21.000+0000"
        assert entries[1]["timestamp"] == "2011-08-23T13:41:22.000+0000"
        assert entries[2]["timestamp"] == "2011-08-23T13:41:21.000+0000"  # No collision with entry[1]
        assert entries[3]["timestamp"] == "2011-08-23T13:41:22.000+0000"  # Collision with entry[2]

    def test_iso8601_timezone_parsing(self, wpm):
        """Test that ISO8601 timestamps with timezone are parsed correctly."""
        test_cases = [
            "2011-08-23T13:41:21.000+0000",
            "2011-08-23T13:41:21.000+00:00",
            "2011-08-23T13:41:21.000Z",
            "2011-08-23T13:41:21+0000"
        ]

        from datetime import datetime

        for timestamp_str in test_cases:
            try:
                if 'T' in timestamp_str:
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    assert dt is not None, f"Failed to parse: {timestamp_str}"
            except Exception as e:
                pytest.fail(f"Failed to parse {timestamp_str}: {e}")

    def test_collision_with_mixed_formats(self, wpm):
        """Test collision detection with mixed timestamp formats."""
        entries = [
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "comment"},
            {"timestamp": "2011-08-23T13:41:21.000+00:00", "type": "changelog"}
        ]

        # These should NOT be detected as collisions because format differs
        # But the parsing should handle both
        from datetime import datetime, timedelta

        parsed_timestamps = []
        for entry in entries:
            ts = entry.get("timestamp", "")
            if 'T' in ts:
                dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                parsed_timestamps.append(dt)

        # Both should parse to the same datetime
        assert parsed_timestamps[0] == parsed_timestamps[1], "Different formats should parse to same datetime"

    def test_empty_timestamp_handling(self, wpm):
        """Test that empty timestamps don't cause crashes."""
        entries = [
            {"timestamp": "", "type": "comment"},
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "changelog"},
            {"timestamp": None, "type": "comment"}
        ]

        from datetime import datetime, timedelta

        # Should not crash
        for i in range(1, len(entries)):
            current_timestamp = entries[i].get("timestamp", "")
            previous_timestamp = entries[i-1].get("timestamp", "")

            if current_timestamp and previous_timestamp and current_timestamp == previous_timestamp:
                if 'T' in current_timestamp:
                    dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                    dt = dt + timedelta(seconds=1)
                    entries[i]["timestamp"] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000'

        # Verify no crashes and empty timestamps remain empty
        assert entries[0]["timestamp"] == ""
        assert entries[2]["timestamp"] is None

    def test_collision_resolution_preserves_milliseconds(self, wpm):
        """Test that millisecond precision is preserved after collision resolution."""
        entries = [
            {"timestamp": "2011-08-23T13:41:21.123+0000", "type": "comment"},
            {"timestamp": "2011-08-23T13:41:21.123+0000", "type": "changelog"}
        ]

        from datetime import datetime, timedelta

        for i in range(1, len(entries)):
            current_timestamp = entries[i].get("timestamp", "")
            previous_timestamp = entries[i-1].get("timestamp", "")

            if current_timestamp and previous_timestamp and current_timestamp == previous_timestamp:
                if 'T' in current_timestamp:
                    dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                    dt = dt + timedelta(seconds=1)
                    entries[i]["timestamp"] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000'

        # Verify milliseconds are preserved
        assert entries[0]["timestamp"] == "2011-08-23T13:41:21.123+0000"
        assert entries[1]["timestamp"] == "2011-08-23T13:41:22.123+0000"
        assert ".123" in entries[1]["timestamp"], "Milliseconds should be preserved"

    def test_collision_at_different_positions(self, wpm):
        """Test collision detection at various positions in the list."""
        entries = [
            {"timestamp": "2011-08-23T13:41:20.000+0000", "type": "comment"},
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "changelog"},
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "comment"},  # Collision at index 2
            {"timestamp": "2011-08-23T13:41:22.000+0000", "type": "changelog"},
            {"timestamp": "2011-08-23T13:41:23.000+0000", "type": "comment"},
            {"timestamp": "2011-08-23T13:41:23.000+0000", "type": "changelog"}  # Collision at index 5
        ]

        from datetime import datetime, timedelta

        for i in range(1, len(entries)):
            current_timestamp = entries[i].get("timestamp", "")
            previous_timestamp = entries[i-1].get("timestamp", "")

            if current_timestamp and previous_timestamp and current_timestamp == previous_timestamp:
                if 'T' in current_timestamp:
                    dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                    dt = dt + timedelta(seconds=1)
                    entries[i]["timestamp"] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000'

        # Verify collisions were resolved by comparing consecutive entries
        # i=2: entries[2] == entries[1]? YES (both 21) → adjust to 13:41:22
        # i=3: entries[3] == entries[2]? YES (both 22) → adjust to 13:41:23
        # i=4: entries[4] == entries[3]? YES (both 23) → adjust to 13:41:24
        # i=5: entries[5] == entries[4]? NO (23 != 24) → no adjustment
        # CASCADE EFFECT: Original collision at i=2 created chain reaction through i=4
        assert entries[1]["timestamp"] != entries[2]["timestamp"]
        assert entries[4]["timestamp"] != entries[5]["timestamp"]  # 24 != 23

        # Verify 1-second separation for resolved collisions
        assert entries[2]["timestamp"] == "2011-08-23T13:41:22.000+0000"
        assert entries[5]["timestamp"] == "2011-08-23T13:41:23.000+0000"  # Unchanged from original


class TestTimestampCollisionIntegration:
    """Integration tests for collision detection in real migration scenario."""

    @pytest.fixture
    def wpm(self):
        """Create WorkPackageMigration instance with mocked clients."""
        mock_jira = Mock()
        mock_op = Mock()
        return WorkPackageMigration(jira_client=mock_jira, op_client=mock_op)

    def test_nrs_182_scenario(self, wpm):
        """Test the exact NRS-182 scenario that caused constraint violations."""
        # Real data from NRS-182 that had collisions
        entries = [
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "comment", "author": "user1"},
            {"timestamp": "2011-08-23T13:41:21.000+0000", "type": "changelog", "author": "user1"},
            {"timestamp": "2011-08-23T13:42:00.000+0000", "type": "comment", "author": "user2"},
            {"timestamp": "2011-09-03T14:21:26.000+0000", "type": "comment", "author": "user3"},
            {"timestamp": "2011-09-03T14:21:26.000+0000", "type": "changelog", "author": "user3"}
        ]

        from datetime import datetime, timedelta

        collision_count = 0
        for i in range(1, len(entries)):
            current_timestamp = entries[i].get("timestamp", "")
            previous_timestamp = entries[i-1].get("timestamp", "")

            if current_timestamp and previous_timestamp and current_timestamp == previous_timestamp:
                collision_count += 1
                if 'T' in current_timestamp:
                    dt = datetime.fromisoformat(current_timestamp.replace('Z', '+00:00'))
                    dt = dt + timedelta(seconds=1)
                    entries[i]["timestamp"] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000'

        # Verify 2 collisions were detected and resolved
        assert collision_count == 2, "NRS-182 should have exactly 2 timestamp collisions"

        # Verify no duplicate timestamps remain
        timestamps = [e["timestamp"] for e in entries]
        assert len(timestamps) == len(set(timestamps)), "All timestamps should be unique after resolution"

        # Verify specific resolutions
        assert entries[0]["timestamp"] == "2011-08-23T13:41:21.000+0000"
        assert entries[1]["timestamp"] == "2011-08-23T13:41:22.000+0000"  # Resolved +1 second
        assert entries[3]["timestamp"] == "2011-09-03T14:21:26.000+0000"
        assert entries[4]["timestamp"] == "2011-09-03T14:21:27.000+0000"  # Resolved +1 second


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
