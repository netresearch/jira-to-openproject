# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import pytest

from src.application.components.simpletasks_migration import SimpleTasksMigration


class DummyJira:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def get_issue_property(self, key: str, prop: str):
        return self._payload.get(key)


class DummyOp:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def set_checklist_section(self, wp_id: int, md: str):
        self.calls.append((wp_id, md))
        return True


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    from src import config
    from src import mappings as pkg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "90901": {"openproject_id": 101, "jira_key": "J-1"},
                    "J-2": {"openproject_id": 102},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    # Patch both the Mappings class and the config.mappings instance
    monkeypatch.setattr(pkg, "Mappings", DummyMappings)
    monkeypatch.setattr(config, "mappings", DummyMappings())


def test_simpletasks_migration_renders_markdown_and_updates():
    jira = DummyJira(
        {
            "J-1": {"tasks": [{"title": "A", "checked": True}, {"title": "B", "labels": ["x", "y"]}]},
            "J-2": {"value": [{"text": "C", "dueDate": "2025-09-01"}]},
        },
    )
    op = DummyOp()

    mig = SimpleTasksMigration(jira_client=jira, op_client=op)  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)

    assert ld.success is True
    assert ld.updated == 2
    # Verify markdown shape
    assert any("- [x] A" in md for _, md in op.calls)
    assert any("labels: x, y" in md for _, md in op.calls)
    assert any("due:" in md for _, md in op.calls)


def _render(identifier="42", key="J-1", checked=True):
    flag = " checked" if checked else ""
    return (
        f'<div class="task" data-issue="{key}" data-task-id="{identifier}"><div>'
        f'<input type="checkbox" class="task-input" data-task-id="{identifier}"{flag}/></div></div>'
    )


def _raw(source, rendered):
    return {"key": "J-1", "fields": {"description": source}, "renderedFields": {"description": rendered}}


def test_migrates_only_description_macros_when_property_is_missing():
    from unittest.mock import Mock

    jira = Mock()
    op = DummyOp()
    jira.get_issue_property.side_effect = [None, []]
    source = "{task:id=42}![image](somewhere) `sample`\nnext{task}{task:id=57}Last{task}"
    raw = _raw(source, _render("57", checked=False) + _render())
    raw["renderedFields"]["comment"] = _render("63")
    raw["fields"]["comment"] = "{task:id=63}unknown audience{task}"
    jira.jira.issue.return_value.raw = raw
    result = SimpleTasksMigration(jira_client=jira, op_client=op).run()
    assert result.success and result.updated == 1
    jira.jira.issue.assert_called_once_with("J-1", fields="description", expand="renderedFields")
    assert op.calls == [(101, "- [x] `` ![image](somewhere) `sample` next ``\n- [ ] ` Last `")]


@pytest.mark.parametrize(
    ("source", "rendered"),
    [
        ("{task:id=42}title{task}", _render(key="X-1")),
        ("{task:id=42}title{task}", _render("41")),
        ("{task:id=42}title{task}", _render() * 2),
        ("{task:id=42}one{task}{task:id=42}two{task}", _render()),
        ("{task:type=other}title{task}", _render()),
        ("{task}title{task}", _render()),
        ("{task:id=42}title", _render()),
    ],
)
def test_inconsistent_description_tasks_abort_before_writing(source, rendered):
    from unittest.mock import Mock

    jira = Mock()
    op = DummyOp()
    jira.get_issue_property.return_value = None
    jira.jira.issue.return_value.raw = _raw(source, rendered)
    assert not SimpleTasksMigration(jira_client=jira, op_client=op).run().success
    assert op.calls == []


def test_missing_description_tasks_and_issue_identity():
    from src.infrastructure.jira.description_tasks import description_tasks

    assert description_tasks("J-1", _raw(None, None)) == []
    with pytest.raises(ValueError, match="different issue"):
        description_tasks("J-2", _raw(None, None))


def test_issue_property_only_falls_back_on_missing():
    from unittest.mock import Mock

    from src.infrastructure.jira.jira_client import JiraApiError, JiraResourceNotFoundError
    from src.infrastructure.jira.jira_field_service import JiraFieldService

    client = Mock()
    fields = JiraFieldService(client)
    client._make_request.side_effect = JiraApiError("unavailable")
    with pytest.raises(JiraApiError, match="unavailable"):
        fields.get_issue_property("J-1", "example/key")
    client._make_request.assert_called_with("/rest/api/2/issue/J-1/properties/example%2Fkey", timeout=15)
    client._make_request.side_effect = JiraResourceNotFoundError("missing")
    assert fields.get_issue_property("J-1", "example") is None
    sdk_error = Exception("missing")
    sdk_error.status_code = 404
    wrapped = JiraApiError("upstream request failed")
    wrapped.__cause__ = sdk_error
    client._make_request.side_effect = wrapped
    assert fields.get_issue_property("J-1", "example") is None
    client._make_request.side_effect = None
    client._make_request.return_value.json.return_value = {"value": []}
    assert fields.get_issue_property("J-1", "example") == []
    client._make_request.return_value.json.return_value = "bad"
    with pytest.raises(JiraApiError, match="Expected an object"):
        fields.get_issue_property("J-1", "example")


def test_checklist_writer_requires_a_verified_result():
    from unittest.mock import Mock

    from src.infrastructure.openproject.openproject_work_package_content_service import (
        OpenProjectWorkPackageContentService,
    )

    client = Mock()
    service = OpenProjectWorkPackageContentService(client)
    client.execute_script_with_data.return_value = {"status": "success", "data": {"verified": True}}
    assert service.set_checklist_section(12, "example")
    script, data = client.execute_script_with_data.call_args.args
    assert "example" not in script
    assert data == {"id": 12, "markdown": "example"}
    client.execute_script_with_data.return_value = {"status": "success"}
    assert not service.set_checklist_section(12, "example")
