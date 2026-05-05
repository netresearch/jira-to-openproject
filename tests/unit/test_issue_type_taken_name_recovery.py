"""Recovery from ``Name has already been taken`` errors in issue-type bulk create.

The pre-check at the start of ``migrate_issue_types_via_rails`` queries OP
once for existing types and skips Jira types whose normalized name already
exists. But during a real run on 2026-05-04 ten Jira types still hit
``Name has already been taken.`` from ``Type.create!`` Rails validation —
likely because the same ``openproject_name`` was assigned to multiple Jira
types whose first instance got created earlier in the same bulk batch and
made the second instance a duplicate, or because the pre-check missed a
case-/whitespace-sensitive match.

Either way: a duplicate-name error after bulk_create is recoverable.
``_resolve_name_taken_errors`` looks up each colliding name in OP, points
the corresponding ``issue_type_mapping`` entries at the existing type's id,
and returns those failures stripped from the error list.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.application.components.issue_type_migration import IssueTypeMigration


def _make_migration() -> IssueTypeMigration:
    """Construct a minimal IssueTypeMigration for unit-level method calls."""
    obj = IssueTypeMigration.__new__(IssueTypeMigration)
    obj.logger = MagicMock()
    obj.issue_type_mapping = {}
    obj.op_client = MagicMock()
    return obj


def test_resolves_taken_name_by_existing_op_id() -> None:
    """A 'Name has already been taken' error is reclaimed by looking up the
    existing OP type and pointing the mapping at it.
    """
    mig = _make_migration()
    mig.issue_type_mapping = {
        "Sub: Documentation": {
            "jira_id": 100,
            "jira_name": "Sub: Documentation",
            "openproject_name": "Documentation",
            "openproject_id": None,
        },
        "Documentation": {
            "jira_id": 101,
            "jira_name": "Documentation",
            "openproject_name": "Documentation",
            "openproject_id": None,
        },
    }

    records = [
        {"name": "Documentation", "is_milestone": False, "is_default": False},
    ]
    errors = [{"index": 0, "errors": ["Name has already been taken."]}]
    existing_types = [{"id": 42, "name": "Documentation"}]

    resolved, unresolved = mig._resolve_name_taken_errors(
        errors, records, existing_types,
    )

    assert resolved == 1
    assert unresolved == []
    # Both Jira mappings that normalized to "Documentation" now point at 42
    assert mig.issue_type_mapping["Documentation"]["openproject_id"] == 42
    assert mig.issue_type_mapping["Sub: Documentation"]["openproject_id"] == 42
    # And matched_by is updated for traceability
    assert mig.issue_type_mapping["Documentation"]["matched_by"] == "found_existing_after_create"


def test_unrelated_errors_pass_through_untouched() -> None:
    """Errors that aren't 'Name has already been taken' must remain in the
    unresolved list — caller still needs to surface real failures.
    """
    mig = _make_migration()
    mig.issue_type_mapping = {
        "Documentation": {
            "jira_id": 101,
            "jira_name": "Documentation",
            "openproject_name": "Documentation",
            "openproject_id": None,
        },
    }
    records = [{"name": "Documentation"}]
    errors = [{"index": 0, "errors": ["Color is invalid"]}]

    resolved, unresolved = mig._resolve_name_taken_errors(errors, records, [])

    assert resolved == 0
    assert unresolved == errors


def test_taken_but_not_found_in_op_remains_unresolved() -> None:
    """If a name is reported as taken but no matching existing type can be
    found (extreme race / OP returned partial list), keep the error so the
    component still surfaces it.
    """
    mig = _make_migration()
    mig.issue_type_mapping = {
        "Mystery": {
            "jira_id": 200,
            "jira_name": "Mystery",
            "openproject_name": "Mystery",
            "openproject_id": None,
        },
    }
    records = [{"name": "Mystery"}]
    errors = [{"index": 0, "errors": ["Name has already been taken."]}]

    # OP returns a different type
    existing_types = [{"id": 7, "name": "Other"}]

    resolved, unresolved = mig._resolve_name_taken_errors(errors, records, existing_types)

    assert resolved == 0
    assert len(unresolved) == 1


def test_match_is_case_insensitive() -> None:
    """OP type names can differ in casing from the proposed name."""
    mig = _make_migration()
    mig.issue_type_mapping = {
        "Question": {
            "jira_id": 300,
            "jira_name": "Question",
            "openproject_name": "Question",
            "openproject_id": None,
        },
    }
    records = [{"name": "Question"}]
    errors = [{"index": 0, "errors": ["Name has already been taken."]}]
    existing_types = [{"id": 9, "name": "QUESTION"}]

    resolved, unresolved = mig._resolve_name_taken_errors(errors, records, existing_types)

    assert resolved == 1
    assert mig.issue_type_mapping["Question"]["openproject_id"] == 9
    assert unresolved == []


def test_mixed_resolvable_and_unresolvable() -> None:
    """A batch can contain both: only the resolvable taken-name ones get
    pulled out; the rest stay in ``unresolved`` for the caller.
    """
    mig = _make_migration()
    mig.issue_type_mapping = {
        "TypeA": {
            "jira_id": 1,
            "jira_name": "TypeA",
            "openproject_name": "TypeA",
            "openproject_id": None,
        },
        "TypeB": {
            "jira_id": 2,
            "jira_name": "TypeB",
            "openproject_name": "TypeB",
            "openproject_id": None,
        },
    }
    records = [{"name": "TypeA"}, {"name": "TypeB"}]
    errors = [
        {"index": 0, "errors": ["Name has already been taken."]},
        {"index": 1, "errors": ["Color is invalid"]},
    ]
    existing_types = [{"id": 11, "name": "TypeA"}]

    resolved, unresolved = mig._resolve_name_taken_errors(errors, records, existing_types)

    assert resolved == 1
    assert mig.issue_type_mapping["TypeA"]["openproject_id"] == 11
    assert len(unresolved) == 1
    assert unresolved[0]["errors"] == ["Color is invalid"]
