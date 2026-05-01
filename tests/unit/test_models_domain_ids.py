"""Tests for branded identifier types in :mod:`src.domain.ids`.

``NewType`` is a no-op at runtime — these tests pin that contract so a
future refactor that swaps ``NewType`` for ``Annotated`` (or vice versa)
trips the suite if the substitution would change runtime semantics.
"""

from __future__ import annotations

from src.domain.ids import (
    JiraAccountId,
    JiraIssueKey,
    JiraProjectKey,
    JiraUserKey,
    OpCustomFieldId,
    OpPriorityId,
    OpProjectId,
    OpStatusId,
    OpTypeId,
    OpUserId,
    OpWorkPackageId,
)


def test_jira_str_brands_are_zero_cost() -> None:
    issue = JiraIssueKey("ABC-123")
    project = JiraProjectKey("ABC")
    user_key = JiraUserKey("jdoe")
    account = JiraAccountId("557058:abc")

    # NewType wrappers are runtime-equal to their underlying values.
    assert issue == "ABC-123"
    assert project == "ABC"
    assert user_key == "jdoe"
    assert account == "557058:abc"
    # All four are concretely ``str`` at runtime.
    assert isinstance(issue, str)
    assert isinstance(account, str)


def test_op_int_brands_are_zero_cost() -> None:
    user = OpUserId(1)
    project = OpProjectId(2)
    wp = OpWorkPackageId(3)
    cf = OpCustomFieldId(4)
    status = OpStatusId(5)
    priority = OpPriorityId(6)
    op_type = OpTypeId(7)

    assert user == 1
    assert project == 2
    assert wp == 3
    assert cf == 4
    assert status == 5
    assert priority == 6
    assert op_type == 7
    # Concretely ``int`` at runtime.
    assert isinstance(user, int)
    assert isinstance(op_type, int)


def test_branded_ids_compose_with_unbranded_arithmetic() -> None:
    """At runtime, branded ints behave exactly like ints."""
    wp = OpWorkPackageId(40)
    assert wp + 2 == 42
    assert sorted([OpUserId(3), OpUserId(1), OpUserId(2)]) == [1, 2, 3]
