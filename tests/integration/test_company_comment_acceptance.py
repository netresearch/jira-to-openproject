# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Check that the import keeps each real comment's reader circle, per test account and per system.

Opt-in: run through tests/integration/run_company_comment_acceptance.sh with a private fixture from
tests/integration/company_comment_sample.py. Real comment text stays in the private state directory.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import requests
from pyrage import x25519

from src.comment_archive.archive import enrollment, migrate, unpack
from src.comment_archive.transport import ArchiveError, Endpoint, attachments, download
from tests.integration.company_comment_load import PROJECTS, load
from tests.integration.company_comment_stack import JiraAdmin, rails, run_j2o, start_console, stop_console

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

FIXTURE = os.getenv("J2O_COMPANY_COMMENT_FIXTURE")
STACK = os.getenv("J2O_ACCEPT_STACK")
CHECKOUT = Path(__file__).resolve().parents[2]
REFUSED = {401, 403, 404}


def _private_json(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)


def _container(project: str, compose_file: str) -> str:
    container_id = subprocess.check_output(
        ["docker", "compose", "-p", project, "-f", compose_file, "ps", "-q", "op"],
        text=True,
        timeout=60,
    ).strip()
    return subprocess.check_output(
        ["docker", "inspect", "--format", "{{.Name}}", container_id],
        text=True,
        timeout=60,
    ).strip("/\n")


def _jira_view(session: requests.Session, jira: str, issue: str, token: str | None) -> tuple[int, set[str]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = session.get(f"{jira}/rest/api/2/issue/{issue}/comment", headers=headers, timeout=60)
    if response.status_code != 200:
        return response.status_code, set()
    page = response.json()
    assert page["total"] == len(page["comments"])
    return 200, {row["id"] for row in page["comments"]}


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _activity_leaks(op: Endpoint, wp: int, bodies: list[str]) -> int:
    """Count comment bodies that appear in any ordinary activity the account can read."""
    texts = [
        _normalized((row.get("comment") or {}).get("raw") or "")
        for row in op.get(f"/api/v3/work_packages/{wp}/activities")["_embedded"]["elements"]
    ]
    return sum(any(_normalized(body) in text for text in texts) for body in bodies)


def _components(results: Path, started: float) -> dict[str, str]:
    """Read the component outcomes of the migration run started at this time."""
    runs = [path for path in results.glob("migration_results_*.json") if path.stat().st_mtime >= started]
    if len(runs) != 1:
        return {}
    outcome = {}
    for name, row in json.loads(runs[0].read_text())["components"].items():
        skipped = str(row.get("message") or "").startswith("No changes detected")
        outcome[name] = "failed" if not row.get("success") else "skipped" if skipped else "ran"
    return outcome


def _op_view(op: Endpoint, wp: int | None, key: x25519.Identity) -> tuple[str, set[str]]:
    if wp is None:
        return "not imported", set()
    probe = op.request("GET", f"/api/v3/work_packages/{wp}", denial=True)
    if probe.status_code in REFUSED:
        return f"HTTP {probe.status_code}", set()
    files = attachments(op, wp)
    if not files:
        return "no archive", set()
    try:
        view = unpack(download(op, files[0]), key, wp)
    except ArchiveError:
        return "no readable copy", set()
    return "archive", {row["id"] for row in view["comments"]}


def _cutover(stack, case, loaded, *, people, fields, inventory_token, publisher, state) -> tuple[dict | str, dict]:
    """Enroll both case accounts with their own credentials and publish the project's archive."""
    jira, op, ca = stack["jira"], stack["openproject"], stack["ca"]
    keys = {}
    readers = []
    directory = []
    data = loaded["cases"][case]
    with pytest.MonkeyPatch.context() as env:
        for side, account in data["accounts"].items():
            person = people[account["email"]]
            keys[side] = x25519.Identity.generate()
            record = enrollment(
                Endpoint(jira, account["token"], jira=True, ca=ca),
                Endpoint(op, person["token"], jira=False, ca=ca),
                str(keys[side].to_public()),
                account["token"],
                person["token"],
            )
            names = (f"J2O_ACCEPT_{case.upper()}_{side.upper()}_JIRA", f"J2O_ACCEPT_{case.upper()}_{side.upper()}_OP")
            env.setenv(names[0], account["token"])
            env.setenv(names[1], person["token"])
            readers.append({"enrollment": record, "jira_token_env": names[0], "op_token_env": names[1]})
            directory.append({"jira_key": account["key"], "op_id": person["id"]})
        env.setenv("J2O_ARCHIVE_INVENTORY_TOKEN", inventory_token)
        env.setenv("J2O_ARCHIVE_PUBLISHER_TOKEN", publisher)
        config = {
            "jira": jira,
            "openproject": op,
            "origin_id_field": fields["id"],
            "origin_url_field": fields["url"],
            "readers": readers,
        }
        inventory = {
            "jira": jira,
            "project": data["project"],
            "issues": {data["issue_id"]: list(data["comments"].values())},
        }
        try:
            outcome = migrate(
                config,
                None,
                data["project"],
                state / f"archive-{case}-{os.urandom(4).hex()}.sqlite",
                ca,
                directory=directory,
                inventory_file=inventory,
            )
        except ArchiveError as error:
            outcome = f"blocked: {error}"
    return outcome, keys


@pytest.fixture(scope="module")
def acceptance():
    if not (FIXTURE and STACK):
        pytest.skip("Set J2O_COMPANY_COMMENT_FIXTURE and J2O_ACCEPT_STACK (see run_company_comment_acceptance.sh)")
    stack = json.loads(Path(STACK).read_text())
    state = Path(STACK).parent
    fixture = json.loads(Path(FIXTURE).read_text())
    container = _container(stack["compose_project"], stack["compose_file"])
    admin = JiraAdmin(stack["jira"], stack["ca"], "acceptadmin", (state / "jira-admin-password").read_text().strip())
    loaded_file = state / "loaded.json"
    if not loaded_file.exists():
        _private_json(loaded_file, load(admin, fixture))
    loaded = json.loads(loaded_file.read_text())
    credentials_file = state / "migration-credentials.json"
    if not credentials_file.exists():
        _private_json(
            credentials_file,
            {
                "jira": admin.rest(
                    "POST", "/rest/pat/latest/tokens", json={"name": "migration", "expirationDuration": 2}
                )["rawToken"],
                "op": rails(container, "Token::API.create!(user: User.admin.first!).plain_value"),
            },
        )
    credentials = json.loads(credentials_file.read_text())
    session = f"{stack['compose_project']}-console"
    try:
        start_console(container, session, CHECKOUT / "contrib" / "openproject.irbrc")
        env = {
            "J2O_JIRA_URL": stack["jira"],
            "J2O_JIRA_USERNAME": admin.user,
            "J2O_JIRA_API_TOKEN": credentials["jira"],
            "J2O_OPENPROJECT_URL": stack["openproject"],
            "J2O_OPENPROJECT_API_TOKEN": credentials["op"],
            "J2O_OPENPROJECT_API_KEY": credentials["op"],
            "J2O_OPENPROJECT_SERVER": "j2o-accept-local",
            "J2O_ACCEPT_SSH_ALIAS": "j2o-accept-local",
            "J2O_OPENPROJECT_USER": "",
            "J2O_OPENPROJECT_CONTAINER": container,
            "J2O_OPENPROJECT_TMUX_SESSION_NAME": session,
            "J2O_COPY_PUBLIC_COMMENTS": "0",
            "REQUESTS_CA_BUNDLE": stack["ca"],
        }
        args = ["migrate", "--profile", "full", "--jira-project-filter", ",".join(PROJECTS.values())]
        started = time.time()
        exit_code = run_j2o(state, CHECKOUT, env, [*args, "--no-confirm", "--no-backup"], state / "migrate.log")
        components = _components(state / "var" / "results", started)
    finally:
        stop_console(session)
    emails = sorted(account["email"] for case in loaded["cases"].values() for account in case["accounts"].values())
    people = rails(
        container,
        f"""
        {json.dumps(emails)}.to_h do |mail|
          user = User.find_by!(mail: mail)
          [mail, {{id: user.id, admin: user.admin?, token: Token::API.create!(user: user).plain_value}}]
        end
        """,
    )
    fields = rails(
        container,
        """{id: WorkPackageCustomField.find_by!(name: "J2O Origin ID").id,
            url: WorkPackageCustomField.find_by!(name: "J2O Origin URL").id}""",
    )
    results = {}
    http = requests.Session()
    http.trust_env = False
    http.verify = stack["ca"]
    for case, data in loaded["cases"].items():
        assert not any(people[account["email"]]["admin"] for account in data["accounts"].values())
        outcome, keys = _cutover(
            stack,
            case,
            loaded,
            people=people,
            fields=fields,
            inventory_token=credentials["jira"],
            publisher=credentials["op"],
            state=state,
        )
        wp = rails(
            container,
            f"""CustomValue.where(customized_type: "WorkPackage", custom_field_id: {fields["id"]},
                                  value: {json.dumps(data["issue_id"])}).pluck(:customized_id).first""",
        )
        to_source = {local: source for source, local in data["comments"].items()}
        restricted = set(data["restricted"])
        row = {"cutover": outcome, "work_package": wp is not None, "jira": {}, "op": {}, "restricted": {}}
        for side, account in data["accounts"].items():
            status, jira_ids = _jira_view(http, stack["jira"], data["issue"], account["token"])
            op = Endpoint(stack["openproject"], people[account["email"]]["token"], jira=False, ca=stack["ca"])
            how, op_ids = _op_view(op, wp, keys[side])
            row["jira"][side] = sorted(to_source[cid] for cid in jira_ids)
            row["op"][side] = sorted(to_source[cid] for cid in op_ids)
            row["op"][f"{side}_how"] = how
            row["jira"][f"{side}_status"] = status
            row["restricted"][side] = {
                "jira": len(restricted & set(row["jira"][side])),
                "op": len(restricted & set(row["op"][side])),
            }
            if wp is not None and how not in {"HTTP 403", "HTTP 404"}:
                bodies = [comment["body"] for comment in fixture["cases"][case]["comments"]]
                row["op"][f"{side}_activity_leaks"] = _activity_leaks(op, wp, bodies)
            listing = op.get(f"/api/v3/projects/{data['project'].lower()}/work_packages", denial=True)
            row["op"][f"{side}_listed"] = listing.get("total", 0) if isinstance(listing, dict) else 0
        anonymous_op = http.get(f"{stack['openproject']}/api/v3/work_packages/{wp or 0}", timeout=60).status_code
        row["anonymous"] = {"jira": _jira_view(http, stack["jira"], data["issue"], None)[0], "op": anonymous_op}
        results[case] = row
    _private_json(
        state / "acceptance-results.json", {"migrate_exit": exit_code, "components": components, "cases": results}
    )
    return {"fixture": fixture, "results": results, "components": components}


def _expected(source: dict) -> tuple[list[str], list[str]]:
    every = sorted(comment["id"] for comment in source["comments"])
    public = sorted(comment["id"] for comment in source["comments"] if not comment["visibility"])
    # The unauthorized account of the ordinary and security cases has no project role at all.
    return every, public


def test_import_components_ran(acceptance):
    required = ("users", "groups", "projects", "work_packages_skeleton", "admin_schemes")
    assert {name: acceptance["components"].get(name) for name in required} == dict.fromkeys(required, "ran")


@pytest.mark.parametrize("case", list(PROJECTS))
def test_jira_reader_circle(acceptance, case):
    every, public = _expected(acceptance["fixture"]["cases"][case])
    row = acceptance["results"][case]
    assert row["jira"]["authorized"] == every
    assert row["jira"]["other"] == (public if case in {"group", "role"} else [])


@pytest.mark.parametrize("case", list(PROJECTS))
def test_unauthorized_account_reads_no_restricted_comment(acceptance, case):
    row = acceptance["results"][case]
    assert row["restricted"]["other"] == {"jira": 0, "op": 0}
    assert not row["op"].get("other_activity_leaks")
    if case == "security":
        assert row["op"]["other_listed"] == 0


@pytest.mark.parametrize("case", list(PROJECTS))
def test_authorized_account_reads_its_jira_view(acceptance, case):
    row = acceptance["results"][case]
    assert row["op"]["authorized"] == row["jira"]["authorized"], (row["op"]["authorized_how"], row["cutover"])
    assert row["op"]["other"] == row["jira"]["other"], (row["op"]["other_how"], row["cutover"])
    assert not row["op"].get("authorized_activity_leaks")


@pytest.mark.parametrize("case", list(PROJECTS))
def test_anonymous_access_is_refused(acceptance, case):
    anonymous = acceptance["results"][case]["anonymous"]
    assert anonymous["jira"] in REFUSED
    assert anonymous["op"] in REFUSED
