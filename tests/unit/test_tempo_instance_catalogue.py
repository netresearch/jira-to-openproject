# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Check instance scope, complete static choices and conflict-safe catalogue recovery."""

from unittest.mock import Mock

import pytest
from pyrage import x25519

from src.application.components.tempo_attribute_catalogue import archive_catalogue
from src.infrastructure.jira.jira_client import JiraApiError
from src.infrastructure.jira.jira_tempo_service import JiraTempoService
from src.utils.tempo_attribute_archive import KEY_SETTING, open_document


def test_all_definitions_and_static_choices():
    client = Mock()
    service = JiraTempoService(client)
    options = [{"id": 12, "workAttributeId": 5, "removed": False}, {"id": 13, "workAttributeId": 5, "removed": True}]
    static = {"id": 5, "type": {"value": "STATIC_LIST"}, "name": "Unused"}
    dynamic = {"id": 1, "type": {"value": "DYNAMIC_DROPDOWN"}, "externalUrl": "https://provider.example.test"}
    service._work_attribute_catalogue = Mock(side_effect=[[static, dynamic], options])
    assert service.work_attribute_snapshot() == [
        {"definition": dynamic, "static_options": []},
        {"definition": static, "static_options": options},
    ]
    service._work_attribute_catalogue.assert_called_with("/rest/tempo-core/1/work-attribute/5/static-list-value")
    service._work_attribute_catalogue.side_effect = [[static], [{**options[0], "workAttributeId": 6}]]
    with pytest.raises(JiraApiError, match="different"):
        service.work_attribute_snapshot()
    service._work_attribute_catalogue.side_effect = [[{**static, "type": {"value": "NEW_PROVIDER"}}]]
    with pytest.raises(JiraApiError, match="unsupported"):
        service.work_attribute_snapshot()


def test_separate_tenant_archive_with_revisions_and_race(tmp_path, monkeypatch):
    key = tmp_path / "identity"
    key.write_text(str(x25519.Identity.generate()))
    key.chmod(0o600)
    monkeypatch.setenv(KEY_SETTING, str(key))
    project = "private-archive"
    jira = Mock(base_url="https://jira.example.test")
    source = [
        {
            "definition": {"id": 7, "name": "Confidential unused definition", "type": {"value": "CHECKBOX"}},
            "static_options": [],
        }
    ]
    jira.tempo.work_attribute_snapshot.return_value = source
    op = Mock()
    packets = []

    def target(script, data):
        assert data["project"] == project
        assert "Confidential" not in str(data)
        if "replacement" in data:
            packets.append(data["replacement"])
            if len(packets) == 1:
                return {"status": "success", "data": {"conflict": True}}
        return {"status": "success", "data": {"packet": packets[-1] if packets else None, "field": 3}}

    op.execute_script_with_data.side_effect = target
    assert archive_catalogue(jira, op, project)["written"] is False
    assert archive_catalogue(jira, op, project)["revisions"] == 1
    jira.tempo.work_attribute_snapshot.return_value = []
    assert archive_catalogue(jira, op, project)["written"] is True
    recovered = open_document(packets[-1])
    assert recovered == {
        "format": "j2o-tempo-instance-work-attributes-v1",
        "scope": "jira-instance",
        "jira": jira.base_url,
        "snapshots": [source, []],
    }
    jira.base_url = "https://another.example.test"
    with pytest.raises(ValueError, match="another source"):
        archive_catalogue(jira, op, project)
    assert len(packets) == 2


def test_changing_source_is_not_published():
    jira = Mock(base_url="https://jira.example.test")
    jira.tempo.work_attribute_snapshot.side_effect = [[], [{"definition": {"id": 1}, "static_options": []}]]
    op = Mock()
    with pytest.raises(ValueError, match="changed"):
        archive_catalogue(jira, op, "private-archive")
    op.execute_script_with_data.assert_not_called()
