# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Exercise privileged and anonymous views of the same paginated Jira resource."""

from unittest.mock import Mock, patch

import pytest

from src.infrastructure.jira.comment_audience import select_comments


def page(rows, *, offset=0, total=None, principal="anonymous", status=200, redirect=False):
    response = Mock(status_code=status, headers={"X-AUSERNAME": principal}, is_redirect=redirect)
    response.json.return_value = {"startAt": offset, "comments": rows, "total": len(rows) if total is None else total}
    return response


def rows():
    return [
        {"id": "1", "body": "J2O-CANARY public control", "author": {"name": "writer"}},
        {"id": "2", "body": "J2O-CANARY group restricted", "visibility": {"type": "group", "value": "finance"}},
        {"id": "3", "body": "J2O-CANARY role restricted", "visibility": {"type": "role", "value": "agents"}},
        {
            "id": "4",
            "body": "J2O-CANARY internal",
            "properties": [{"key": "sd.public.comment", "value": {"internal": True}}],
        },
        {"id": "5", "body": "J2O-CANARY private issue or participants"},
        {"id": "6", "body": "J2O-CANARY not in anonymous revision"},
        {"id": "7", "body": "J2O-CANARY plugin restriction", "properties": [{"key": "unknown", "value": {}}]},
        {"id": "8", "body": "J2O-CANARY private customer reply", "jsdPublic": False},
    ]


def test_only_identical_public_body_from_explicitly_anonymous_pages(monkeypatch):
    monkeypatch.setenv("J2O_COPY_PUBLIC_COMMENTS", "1")
    source = rows()
    client = Mock(base_url="https://jira.example/jira")
    client._make_request.side_effect = [page(source[:3], total=8), page(source[3:], offset=3, total=8)]
    # Include rows with explicit restrictions even in the anonymous reply: the
    # importer must still honour them. User metadata also comes from this reply.
    anonymous = [
        dict(source[0], author={"name": "public writer"}),
        *source[1:4],
        dict(source[5], body="older revision"),
        *source[6:],
    ]
    with patch("src.infrastructure.jira.comment_audience.requests.Session") as factory:
        session = factory.return_value.__enter__.return_value
        session.get.side_effect = [page(anonymous[:2], total=7), page(anonymous[2:], offset=2, total=7)]
        selection = select_comments(client, "DEMO-1")
        assert session.trust_env is False
        assert session.cookies.clear.call_count == 2
        for call in session.get.call_args_list:
            assert call.args == ("https://jira.example/jira/rest/api/2/issue/DEMO-1/comment",)
            assert call.kwargs["allow_redirects"] is False
    assert [(c.id, c.body, c.author.name) for c in selection.comments] == [
        ("1", "J2O-CANARY public control", "public writer")
    ]
    assert selection.held == 7


@pytest.mark.parametrize(
    "proof",
    [page(rows(), principal="svc"), page(rows(), principal=""), page([], status=403), page(rows(), redirect=True)],
)
def test_unproven_identity_or_access_holds_everything(monkeypatch, proof):
    monkeypatch.setenv("J2O_COPY_PUBLIC_COMMENTS", "1")
    client = Mock(base_url="https://jira.example")
    client._make_request.return_value = page(rows())
    with patch("src.infrastructure.jira.comment_audience.requests.Session") as factory:
        factory.return_value.__enter__.return_value.get.return_value = proof
        selection = select_comments(client, "PRIVATE-1")
    assert selection.held == 8
    assert not selection.comments


def test_default_keeps_comments_at_source(monkeypatch):
    monkeypatch.delenv("J2O_COPY_PUBLIC_COMMENTS", raising=False)
    client = Mock(base_url="https://jira.example")
    client._make_request.return_value = page(rows())
    with patch("src.infrastructure.jira.comment_audience.requests.Session") as factory:
        selection = select_comments(client, "DEMO-1")
        factory.assert_not_called()
    assert selection.held == 8
    assert not selection.comments


def test_incomplete_inventory_is_an_error():
    client = Mock()
    client._make_request.side_effect = [page(rows()[:1], total=8), page([], offset=1, total=8)]
    with pytest.raises(ValueError, match="Incomplete"):
        select_comments(client, "DEMO-1")


def test_issue_cache_omits_embedded_rendered_and_historical_comments():
    from src.infrastructure.jira.comment_audience import issue_without_comments

    raw = {
        "fields": {"summary": "kept", "comment": "private"},
        "renderedFields": {"comment": "private HTML"},
        "changelog": {
            "histories": [
                {
                    "items": [
                        {"fieldId": "comment", "field": "Kommentar", "fromString": "private revision"},
                        {"field": "status", "toString": "Done"},
                    ]
                }
            ]
        },
    }
    cleaned = issue_without_comments(raw)
    assert "private" not in str(cleaned)
    assert "private" in str(raw)
    assert cleaned["changelog"]["histories"] == [{"items": [{"field": "status", "toString": "Done"}]}]


def test_legacy_history_requires_an_identifiable_non_comment_field():
    from src.infrastructure.jira.comment_audience import issue_without_comments

    history = [
        {"field": "Kommentar", "toString": "confidential revision"},
        {"field": "Texte du journal", "fieldtype": "jira", "fromString": "another private revision"},
        {"field": "  ", "fieldtype": "custom", "toString": "unidentified custom revision"},
        {"field": "Status", "toString": "Done"},
        {"field": "Legacy Team", "fieldtype": "custom", "toString": "Platform"},
        {"field": "Team", "fieldId": "customfield_10400", "toString": "Platform"},
    ]
    result = issue_without_comments({"changelog": {"histories": [{"items": history}]}})
    assert result["changelog"]["histories"][0]["items"] == history[3:]


@pytest.mark.parametrize(
    "audience",
    [
        {"visibility": {"type": "role", "value": "Support"}},
        {"public": True},
        {"jsdPublic": False},
        {"internal": False},
        {"isInternal": True},
        {"properties": [{"key": "sd.public.comment", "value": {"internal": True}}]},
        {"properties": {"unexpected": "shape"}},
        {"historyMetadata": {"extraData": {"groupLevel": "private"}}},
    ],
)
def test_restricted_histories_and_individual_items_are_withheld(audience):
    from types import SimpleNamespace

    from src.infrastructure.jira.comment_audience import (
        has_comment_audience,
        issue_without_comments,
        may_copy_history_item,
    )

    safe = {"field": "status", "toString": "Done"}
    restricted = {**safe, **audience, "toString": "RESTRICTED-CHANGE"}
    cache = issue_without_comments(
        {
            "changelog": {
                "histories": [
                    {**audience, "items": [{**safe, "toString": "RESTRICTED-HISTORY"}]},
                    {"items": [safe, restricted]},
                ]
            }
        }
    )
    assert cache["changelog"]["histories"] == [{"items": [safe]}]
    assert has_comment_audience(SimpleNamespace(raw=audience))
    assert not may_copy_history_item(SimpleNamespace(raw=restricted))
    assert not may_copy_history_item(SimpleNamespace(**safe), SimpleNamespace(raw=audience))
    assert may_copy_history_item(SimpleNamespace(raw=safe), SimpleNamespace(raw={"items": [restricted]}))


def test_sdk_field_identity_disagreement_withholds_history():
    from types import SimpleNamespace

    from src.infrastructure.jira.comment_audience import may_copy_history_item

    item = SimpleNamespace(field="status", raw={"field": "comment"})
    assert not may_copy_history_item(item)
