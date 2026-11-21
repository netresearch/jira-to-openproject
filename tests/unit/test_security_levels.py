# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Classify restricted Jira issues by their readers; recorded fixtures with invented users."""

import copy
import json
from types import SimpleNamespace

import pytest
from pyrage import x25519

from src.comment_archive.transport import ArchiveError
from src.infrastructure.jira import security_levels as sl
from src.restricted_issues import decrypt, main, migration_account

pytestmark = pytest.mark.unit
_JIRA = "https://jira.example.invalid"
_ACCOUNT = "migrator"
_FIELD = "customfield_100"
# 23 users with Browse on the project; the level lets two of them read.
_BROWSE = ["alice", "bob", "carol", "frank", "grace", _ACCOUNT] + [f"user{n:02d}" for n in range(17)]
_PAGE_CAP = 10  # Jira caps each permission search page; the reader must page.


def _li(labels):
    return "".join(f"<li>{label} </li>" for label in labels)


def _row(name, labels, *, default=False):
    lozenge = '<span class="project-config-list-default aui-lozenge">Default</span>' if default else ""
    return (
        '<tr class="project-config-issuesecurity"><td class="project-config-issuesecurity-identity">'
        f'<span class="project-config-issuesecurity-name">{name}</span>{lozenge}</td>'
        '<td class="project-config-issuesecurity-description">Text</td>'
        f'<td class="project-config-issuesecurity-entitylist"><ul>{_li(labels)}</ul></td></tr>'
    )


_BOARD = [
    "Current assignee",
    "Group (leads)",
    "Project Role (Developers)",
    "Project lead",
    "Reporter",
    "Single user (Dana Sample)",
    "Single user (Migration Bot)",
    "User custom field value (Extra Readers)",
]
PAGE = (
    "<html><body><ul><li>Navigation</li></ul>"
    '<table id="project-config-issuesecurities-table" class="aui"><thead><tr><th>Security Level</th></tr></thead>'
    f"<tbody>{_row('Board', _BOARD, default=True)}{_row('Legacy', ['Group (leads)', 'Current user'])}</tbody>"
    "</table></body></html>"
)


def _user(name):
    return {"name": name, "key": name, "displayName": name.title(), "active": True}


def _issue(number, effective, **fields):
    level = "20003" if number == 7 else "20001"
    return {
        "id": str(100 + number),
        "key": f"DEMO-{number}",
        "effective": effective,
        "fields": {
            "security": {"id": level},
            "assignee": fields.get("assignee"),
            "reporter": fields.get("reporter"),
            _FIELD: fields.get("extra"),
        },
    }


ISSUES = [
    _issue(1, ["alice", "bob"]),
    _issue(2, ["alice", "bob", _ACCOUNT]),
    _issue(3, ["alice", "bob", _ACCOUNT], reporter=_user(_ACCOUNT)),
    _issue(4, ["alice", "bob", "grace"], extra=[_user("grace")]),
    _issue(5, ["alice", "bob", "frank"], assignee=_user("ivan")),
    _issue(6, ["alice"]),
    _issue(7, ["alice"]),
]


class Response:
    def __init__(self, status, value=None, text=""):
        self.status_code = status
        self._value = value
        self.text = text

    def json(self):
        return copy.deepcopy(self._value)


class FakeJira:
    """Answer the recorded Jira reads; any other request fails the test."""

    url = _JIRA

    def __init__(self, issues=ISSUES, page=PAGE, *, levels=("Board", "Legacy"), browse=_BROWSE, elsewhere=0, leads=()):
        self.issues = issues
        self.leads = ["alice", "ivan", *leads]
        self.elsewhere = elsewhere  # issues with a level outside the listed projects
        self.page = page
        self.levels = levels
        self.browse = browse
        self.calls = []

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs).json()

    def request(self, method, path, *, denial=False, params=None):
        assert method == "GET"
        self.calls.append((path, params))
        p = params or {}
        if path == "/rest/api/2/project/DEMO/issuesecuritylevelscheme":
            ids = {"Board": "20001", "Legacy": "20003"}
            return Response(200, {"id": 1, "levels": [{"id": ids.get(n, "20009"), "name": n} for n in self.levels]})
        if path == "/rest/api/2/project/NONE/issuesecuritylevelscheme":
            assert denial
            return Response(404, {"errorMessages": ["Security level for project NONE does not exist."]})
        if path == "/plugins/servlet/project-config/DEMO/issuesecurity":
            return Response(200, text=self.page)
        if path == "/rest/api/2/user/permission/search":
            assert p["permissions"] == "BROWSE"
            assert p["username"] == ""
            if "projectKey" in p:
                names = self.browse
            else:
                names = next(i["effective"] for i in self.issues if i["key"] == p["issueKey"])
            start = p["startAt"]
            return Response(200, [_user(n) for n in names[start : start + min(p["maxResults"], _PAGE_CAP)]])
        if path == "/rest/api/2/group/member":
            members = {"leads": self.leads, "devs": ["ivan"]}[p["groupname"]]
            return Response(200, {"startAt": p["startAt"], "isLast": True, "values": [_user(n) for n in members]})
        if path == "/rest/api/2/user/search":
            names = {"Dana Sample": ["dana"], "Migration Bot": [_ACCOUNT], "Twin Name": ["twin1", "twin2"]}
            found = [{**_user(n), "displayName": p["username"]} for n in names[p["username"]]]
            return Response(200, [*found, {**_user("other"), "displayName": "Someone Else"}])
        if path == "/rest/api/2/project/DEMO/role":
            return Response(200, {"Developers": _JIRA + "/rest/api/2/project/DEMO/role/10"})
        if path == "/rest/api/2/project/DEMO/role/10":
            return Response(
                200,
                {
                    "actors": [
                        {"type": "atlassian-user-role-actor", "name": "bob"},
                        {"type": "atlassian-group-role-actor", "name": "devs"},
                    ]
                },
            )
        if path == "/rest/api/2/project":
            return Response(200, [{"key": "NONE"}, {"key": "DEMO"}])
        if path == "/rest/api/2/user":
            assert denial
            return Response(200, _user(p["username"])) if p["username"] == _ACCOUNT else Response(404, {})
        if path == "/rest/api/2/project/DEMO":
            return Response(200, {"key": "DEMO", "lead": _user("alice")})
        if path == "/rest/api/2/field":
            schema = {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:multiuserpicker"}
            return Response(200, [{"id": _FIELD, "name": "Extra Readers", "schema": schema}, {"id": "summary"}])
        if path == "/rest/api/2/search":
            return Response(200, self._search(p))
        if path.startswith("/rest/api/2/issue/"):
            issue = next(i for i in self.issues if i["id"] == path.rsplit("/", 1)[1])
            full = {k: v for k, v in issue.items() if k != "effective"}
            full["fields"] = {**full["fields"], "summary": "Text", "comment": {"comments": [], "total": 0}}
            return Response(200, {**full, "changelog": {"startAt": 0, "total": 0, "histories": []}})
        raise AssertionError(path)

    def _search(self, p):
        jql = p["jql"]
        if p.get("maxResults") == 0:
            if "project" not in jql:
                return {"startAt": 0, "total": len(self.issues) + self.elsewhere}
            return {"startAt": 0, "total": len(self.issues) if '"DEMO"' in jql else 0}
        assert jql == 'project = "DEMO" AND level IS NOT EMPTY ORDER BY id ASC'
        assert set(p["fields"].split(",")) == {"security", "assignee", "reporter", _FIELD}
        start = p["startAt"]
        page = [{k: v for k, v in i.items() if k != "effective"} for i in self.issues[start : start + 3]]
        return {"startAt": start, "total": len(self.issues), "issues": page}


def scan(jira=None, account=_ACCOUNT):
    return sl.scan_project(jira or FakeJira(), "DEMO", account)


def by_key(inventory):
    return {row.issue["key"]: row for rows in inventory.issues.values() for row in rows}


def test_parses_grants_and_fails_closed_on_unknown_pages():
    grants = sl.parse_grant_page(PAGE)
    assert grants["Board"][0] == sl.Grant("assignee")
    assert sl.Grant("user", "Dana Sample") in grants["Board"]
    assert sl.Grant("user_field", "Extra Readers") in grants["Board"]
    assert grants["Legacy"][1] == sl.Grant("unsupported", "Current user")
    with pytest.raises(sl.SecurityLevelError, match="table not found"):
        sl.parse_grant_page("<html><title>Administrator Access</title></html>")
    duplicate = PAGE.replace("Legacy", "Board")
    with pytest.raises(sl.SecurityLevelError, match="duplicate"):
        sl.parse_grant_page(duplicate)
    renamed = FakeJira(levels=("Board", "Renamed"))
    with pytest.raises(sl.SecurityLevelError, match="differ"):
        scan(renamed)


def test_issue_readers_come_from_the_issue_not_the_project():
    # Control: the project grants Browse to 23 users, the level to two of them.
    inventory = scan()
    first = by_key(inventory)["DEMO-1"]
    assert len(_BROWSE) == 23
    assert first.kind == "static"
    assert first.readers == {"alice", "bob"}


def test_static_readers_are_the_grants_intersected_with_project_browse():
    board = scan().levels[0]
    # dana (single user) and ivan (group, role group) lack Browse on the project.
    assert board.readers == {"alice", "bob"}
    assert board.account_grants == 1


def test_temporary_grant_does_not_make_the_migration_account_a_reader():
    rows = by_key(scan())
    assert rows["DEMO-2"].kind == "static"
    assert _ACCOUNT not in rows["DEMO-2"].readers
    assert rows["DEMO-2"].account_dropped
    # Another grant (here: reporter) does make the account a reader.
    assert rows["DEMO-3"].kind == "per_issue"
    assert rows["DEMO-3"].extra == {_ACCOUNT}
    assert not rows["DEMO-3"].account_dropped


def test_migration_account_counts_when_a_static_grant_explains_it():
    # The account holds the temporary grant and is also a member of a granted group.
    board = scan(FakeJira(leads=[_ACCOUNT])).levels[0]
    assert board.readers == {"alice", "bob", _ACCOUNT}
    assert board.account_grants == 1
    level = sl.Level("1", "L", (sl.Grant("group", "g"),))
    readers = sl.LevelReaders(level, frozenset({"alice", _ACCOUNT}), 0)
    row = sl.classify({"fields": {}}, readers, frozenset({"alice", _ACCOUNT}), {}, _ACCOUNT)
    assert (row.kind, row.readers, row.account_dropped) == ("static", {"alice", _ACCOUNT}, False)


def test_classifies_per_issue_and_unexplained_readers():
    rows = by_key(scan())
    assert (rows["DEMO-4"].kind, rows["DEMO-4"].extra) == ("per_issue", {"grace"})
    # frank is neither a static reader nor named by a per-issue grant.
    assert (rows["DEMO-5"].kind, rows["DEMO-5"].extra) == ("unexplained", {"frank"})
    # A static reader that Jira does not list as a reader of the issue.
    assert (rows["DEMO-6"].kind, rows["DEMO-6"].missing) == ("unexplained", {"bob"})
    # A level with a grant type that has no fixed membership stops the level.
    assert rows["DEMO-7"].kind == "unexplained"
    assert {k for k, r in rows.items() if r.kind == "static"} == {"DEMO-1", "DEMO-2"}


def test_rejects_contradicting_searches_and_ambiguous_grants():
    other_level = copy.deepcopy(ISSUES)
    other_level[0]["fields"]["security"]["id"] = "20009"
    cases = [
        (FakeJira(issues=other_level), "outside its scheme"),
        (FakeJira(page=PAGE.replace("Dana Sample", "Twin Name")), "exactly one Jira user"),
        (FakeJira(page=PAGE.replace("(Extra Readers)", "(Unknown Field)")), "exactly one field"),
    ]
    for jira, message in cases:
        with pytest.raises(sl.SecurityLevelError, match=message):
            scan(jira)
    without_scheme = SimpleNamespace(url=_JIRA, **_none_with_issues())
    with pytest.raises(sl.SecurityLevelError, match="no security scheme"):
        sl.scan_project(without_scheme, "NONE", _ACCOUNT)


def _none_with_issues():
    jira = FakeJira()
    return {
        "request": jira.request,
        "get": lambda path, **kw: {"total": 1} if path == "/rest/api/2/search" else jira.get(path, **kw),
    }


def test_never_uses_the_endpoint_that_ignores_the_level():
    jira = FakeJira()
    scan(jira)
    assert all("viewissue" not in path for path, _ in jira.calls)


def test_migration_account_defaults_to_the_configured_jira_user(monkeypatch):
    jira = FakeJira()
    monkeypatch.setenv("J2O_JIRA_USERNAME", _ACCOUNT)
    assert migration_account(jira, None) == _ACCOUNT
    monkeypatch.setenv("J2O_JIRA_USERNAME", "placeholder")
    with pytest.raises(ArchiveError, match="not found"):
        migration_account(jira, None)
    monkeypatch.delenv("J2O_JIRA_USERNAME")
    with pytest.raises(ArchiveError, match="J2O_JIRA_USERNAME"):
        migration_account(jira, None)
    assert migration_account(jira, _ACCOUNT) == _ACCOUNT


def _cli(tmp_path, monkeypatch, jira):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("J2O_ARCHIVE_INVENTORY_TOKEN", "test-token")
    monkeypatch.setattr("src.restricted_issues.Endpoint", lambda *a, **kw: jira)
    identity = x25519.Identity.generate()
    key = tmp_path / "operator.key"
    key.write_text(str(identity))
    return identity, ["--identity", str(key), "--archive", "restricted.age"]


def test_all_projects_must_account_for_every_restricted_issue(tmp_path, monkeypatch):
    _, args = _cli(tmp_path, monkeypatch, FakeJira())
    run = ["inventory", "--jira", _JIRA, "--report", "report.json", "--migration-account", _ACCOUNT, *args]
    assert main(run) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert (report["projects"], report["projects_with_levels"], report["totals"]["issues"]) == (2, 1, 7)
    for name in ("restricted.age", "report.json"):
        (tmp_path / name).unlink()
    _cli(tmp_path, monkeypatch, FakeJira(elsewhere=1))
    assert main(run) == 1
    assert not (tmp_path / "restricted.age").exists()


def test_seals_non_static_issues_and_reports_counts_without_reader_names(tmp_path, monkeypatch):
    identity, args = _cli(tmp_path, monkeypatch, FakeJira())
    key = args[1]
    run = ["inventory", "--jira", _JIRA, "--project", "DEMO", "--report", "report.json", *args]
    assert main([*run, "--migration-account", _ACCOUNT]) == 0

    value = decrypt((tmp_path / "restricted.age").read_bytes(), identity)
    assert sorted(i["key"] for i in value["issues"].values()) == ["DEMO-3", "DEMO-4", "DEMO-5", "DEMO-6", "DEMO-7"]
    assert value["issues"]["104"]["extra_readers"] == ["grace"]
    assert value["issues"]["104"]["issue"]["fields"]["summary"] == "Text"
    assert value["levels"]["DEMO:20001"]["static_readers"] == ["alice", "bob"]

    text = (tmp_path / "report.json").read_text()
    report = json.loads(text)
    assert report["totals"] == {"issues": 7, "static": 2, "per_issue": 2, "unexplained": 3}
    board = report["levels"][0]
    assert (board["static_readers"], board["migration_account_not_counted"]) == (2, 1)
    assert [row["key"] for row in report["archived"]] == ["DEMO-3", "DEMO-4", "DEMO-5", "DEMO-6", "DEMO-7"]
    names = {*_BROWSE, "dana", "ivan", "Dana Sample", "Migration Bot", "Alice", "Grace"}
    assert not [name for name in names if name in text]
    assert (tmp_path / "report.json").stat().st_mode & 0o777 == 0o600

    # An existing archive is never overwritten.
    assert main([*run, "--migration-account", _ACCOUNT]) == 1
    restore = ["restore", *args, "--output", "restored.json"]
    assert main(restore) == 0
    assert json.loads((tmp_path / "restored.json").read_text()) == value
    assert main(["restore", "--identity", key, "--archive", str(tmp_path / "report.json"), "--output", "x.json"])
