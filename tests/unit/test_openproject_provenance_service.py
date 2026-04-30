"""Unit tests for OpenProjectProvenanceService.

Focused regression tests for the bug Copilot caught in PR #112:
``entity_type.title()`` over-capitalised underscore-separated entity types
(``custom_field`` → ``Custom_Field``) while
``ensure_provenance_custom_fields`` named the CFs with ``Custom_field`` /
``Link_type``. The mismatch meant ``custom_field`` and ``link_type``
provenance never matched a CF and ``openproject_id`` always came back ``None``.

Plus a regression test for the truncated-Ruby-script bug in
``record_entity``: the Python conditional inside a parenthesised string-
concatenation expression silently dropped ``wp.save!`` and the ``rescue/end``
lines whenever ``cf_op_id`` was set.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from src.clients.openproject_provenance_service import OpenProjectProvenanceService


@pytest.fixture
def service() -> OpenProjectProvenanceService:
    """Build a service against a minimal mocked OpenProjectClient."""
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_json_query = MagicMock(return_value={"success": True, "id": 1, "subject": "x", "created": True})
    return OpenProjectProvenanceService(client)


class TestEntityTypeLabel:
    """``_entity_type_label`` is the single source of truth for CF/type names."""

    @pytest.mark.parametrize(
        ("entity_type", "expected"),
        [
            ("project", "Project"),
            ("group", "Group"),
            ("type", "Type"),
            ("status", "Status"),
            ("company", "Company"),
            ("account", "Account"),
            # The two tricky ones — title() would give Custom_Field / Link_Type.
            ("custom_field", "Custom_field"),
            ("link_type", "Link_type"),
        ],
    )
    def test_label_matches_cf_name_form(self, entity_type: str, expected: str) -> None:
        assert OpenProjectProvenanceService._entity_type_label(entity_type) == expected

    def test_underscore_label_matches_ensure_cf_naming(self) -> None:
        """The label MUST produce a name that ``ensure_provenance_custom_fields``
        would have created — otherwise lookups fail and ``openproject_id`` is None.
        """
        # CFs created by ensure_provenance_custom_fields:
        cf_names_created = {
            "J2O OP Project ID",
            "J2O OP Group ID",
            "J2O OP Type ID",
            "J2O OP Status ID",
            "J2O OP Company ID",
            "J2O OP Account ID",
            "J2O OP Custom_field ID",
            "J2O OP Link_type ID",
            "J2O Entity Type",
        }
        for entity_type in OpenProjectProvenanceService.ENTITY_TYPES:
            cf_op_id_field = f"J2O OP {OpenProjectProvenanceService._entity_type_label(entity_type)} ID"
            assert cf_op_id_field in cf_names_created, (
                f"Derived CF name {cf_op_id_field!r} for entity_type {entity_type!r} "
                f"does not match any CF created in ensure_provenance_custom_fields"
            )


class TestRecordEntityRubyScript:
    """The generated Ruby script must include wp.save! and the rescue/end block.

    Pre-fix (PR #112 review): a chained Python conditional inside the
    parenthesised string-concat expression caused the script to be truncated
    after the cf_values assignment, dropping wp.save! entirely.
    """

    def test_record_entity_script_contains_save_and_rescue(self, service: OpenProjectProvenanceService) -> None:
        # Pre-populate the cache so record_entity skips the ensure_* roundtrip.
        service._cache = {
            "project_id": 1,
            "type_ids": {"project": 10},
            "cf_ids": {"J2O OP Project ID": 100, "J2O Entity Type": 200},
        }
        service.record_entity(
            entity_type="project",
            jira_key="PROJ-1",
            op_entity_id=42,
        )
        # The mocked execute_json_query was called with the full Ruby script
        # as the first positional argument.
        call_args = service._client.execute_json_query.call_args
        script: str = call_args[0][0]
        # The bug truncated the script after cf_values assignment.
        # All of these MUST be present in a non-truncated script:
        assert "cf_values[100] = 42" in script, "cf_values assignment missing"
        assert "wp.save!" in script, "wp.save! missing — script was truncated"
        assert "rescue => e" in script, "rescue clause missing — script was truncated"
        assert "{ success: false" in script, "error-branch hash missing"
        assert script.rstrip().endswith("end"), "trailing 'end' missing — script was truncated"

    def test_record_entity_script_includes_both_cf_assignments(
        self,
        service: OpenProjectProvenanceService,
    ) -> None:
        """Both the OP-ID and the entity-type CFs should be set independently."""
        service._cache = {
            "project_id": 1,
            "type_ids": {"project": 10},
            "cf_ids": {"J2O OP Project ID": 100, "J2O Entity Type": 200},
        }
        service.record_entity(
            entity_type="project",
            jira_key="PROJ-1",
            op_entity_id=42,
        )
        script: str = service._client.execute_json_query.call_args[0][0]
        assert re.search(r"cf_values\[100\] = 42 if 100", script), "OP-ID assignment missing"
        assert re.search(r"cf_values\[200\] = 'project' if 200", script), "entity-type assignment missing"

    def test_record_entity_omits_optional_assignment_when_cf_missing(
        self,
        service: OpenProjectProvenanceService,
    ) -> None:
        """If the J2O Entity Type CF doesn't exist, that line should be absent.

        (Previously the chained conditional always emitted exactly one line —
        and dropped everything after it.)
        """
        service._cache = {
            "project_id": 1,
            "type_ids": {"project": 10},
            # Note: no "J2O Entity Type" CF.
            "cf_ids": {"J2O OP Project ID": 100},
        }
        service.record_entity(
            entity_type="project",
            jira_key="PROJ-1",
            op_entity_id=42,
        )
        script: str = service._client.execute_json_query.call_args[0][0]
        assert re.search(r"cf_values\[100\] = 42 if 100", script)
        assert "J2O Entity Type" not in script
        # And critically: wp.save! still ships even though only one CF is set.
        assert "wp.save!" in script
