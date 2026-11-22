# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Administer the disposable Jira and OpenProject of the company comment acceptance test."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


class _Forms(HTMLParser):
    """Collect each form's action and its named fields."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._form: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self._form = {"action": values.get("action") or "", "hidden": {}, "names": set()}
            self.forms.append(self._form)
        elif self._form is not None and tag in {"input", "select", "textarea"} and values.get("name"):
            self._form["names"].add(values["name"])
            if values.get("type") == "hidden":
                self._form["hidden"][values["name"]] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None


class JiraAdmin:
    """Drive the local Jira through its REST API and, where no REST write exists, its admin forms."""

    def __init__(self, base: str, ca: str, user: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = ca

    def rest(self, method: str, path: str, *, auth: tuple[str, str] | None = None, ok=(200, 201, 204), **kwargs):
        """Call the REST API as the administrator or as another local account."""
        self.session.cookies.clear()
        response = self.session.request(
            method, self.base + path, auth=auth or (self.user, self.password), timeout=(10, 300), **kwargs
        )
        if response.status_code not in ok:
            msg = f"{method} {path.split('?')[0]} returned HTTP {response.status_code}"
            raise RuntimeError(msg)
        if response.status_code >= 300 or "json" not in response.headers.get("Content-Type", ""):
            return None
        return response.json()

    def form(
        self,
        page: str,
        action: str,
        fields: dict[str, str],
        *,
        auth: bool = True,
        previous: requests.Response | None = None,
    ) -> requests.Response:
        """Submit the form with this action on a page, keeping its hidden fields such as the XSRF token."""
        credentials = (self.user, self.password) if auth else None
        headers = {"X-Atlassian-Token": "no-check"}
        page_response = previous or self.session.get(
            self.base + page, auth=credentials, headers=headers, timeout=(10, 300)
        )
        parser = _Forms()
        parser.feed(page_response.text)
        matches = [
            form
            for form in parser.forms
            if form["action"].split("?")[0].endswith(action) and set(fields) <= form["names"]
        ]
        if len(matches) != 1:
            msg = f"Expected one form {action} on {page.split('?')[0]}"
            raise RuntimeError(msg)
        response = self.session.post(
            urljoin(page_response.url, matches[0]["action"]),
            data={**matches[0]["hidden"], **fields},
            auth=credentials,
            headers=headers,
            timeout=(10, 900),
        )
        response.raise_for_status()
        return response

    def setup(self, license_key: str) -> None:
        """Complete the first-run wizard with the built-in database and a test license."""
        if self.session.get(self.base + "/status", timeout=30).json().get("state") != "FIRST_RUN":
            return
        steps = [
            ("SetupMode", {"setupOption": "classic"}),
            ("SetupDatabase", {"databaseOption": "internal"}),
            (
                "SetupApplicationProperties",
                {"title": "j2o acceptance", "mode": "private", "baseURL": self.base, "nextStep": "true"},
            ),
            ("SetupLicense", {"setupLicenseKey": license_key}),
            (
                "SetupAdminAccount",
                {
                    "fullname": "Acceptance administrator",
                    "email": "admin@example.invalid",
                    "username": self.user,
                    "password": self.password,
                    "confirm": self.password,
                },
            ),
            ("SetupMailNotifications", {"noemail": "true", "analytics-enabled": "false"}),
        ]
        for step, fields in steps:
            self.form(f"/secure/{step}!default.jspa", f"{step}.jspa", fields, auth=False)
        if self.session.get(self.base + "/status", timeout=30).json().get("state") != "RUNNING":
            msg = "Jira setup did not finish"
            raise RuntimeError(msg)

    def enable_group_comment_visibility(self) -> None:
        """Allow comments restricted to groups, not only to project roles."""
        page = "/secure/admin/EditApplicationProperties!default.jspa"
        response = self.session.get(self.base + page, auth=(self.user, self.password), timeout=60)
        current = _Settings()
        current.feed(response.text)
        self.form(page, "EditApplicationProperties.jspa", {**current.values, "groupVisibility": "true"})

    def security_level(self, scheme: str, level: str, group: str, project: str) -> str:
        """Create a scheme with one level readable by a group and assign it to a project."""
        self.form("/secure/admin/AddIssueSecurityScheme!default.jspa", "AddIssueSecurityScheme.jspa", {"name": scheme})
        scheme_id = next(
            row["id"]
            for row in self.rest("GET", "/rest/api/2/issuesecurityschemes")["issueSecuritySchemes"]
            if row["name"] == scheme
        )
        self.form(
            f"/secure/admin/EditIssueSecurities!default.jspa?schemeId={scheme_id}",
            "EditIssueSecurities!addLevel.jspa",
            {"name": level, "description": "Recreated from the sampled issue"},
        )
        levels = self.rest("GET", f"/rest/api/2/issuesecurityschemes/{scheme_id}")["levels"]
        level_id = next(row["id"] for row in levels if row["name"] == level)
        self.form(
            f"/secure/admin/AddIssueSecurity!default.jspa?schemeId={scheme_id}&security={level_id}",
            "AddIssueSecurity.jspa",
            {"type": "group", "group": group},
        )
        project_id = self.rest("GET", f"/rest/api/2/project/{project}")["id"]
        confirm = self.form(
            f"/secure/project/SelectProjectIssueSecurityScheme!default.jspa?projectId={project_id}",
            "SelectProjectSecuritySchemeStep2!default.jspa",
            {"newSchemeId": str(scheme_id)},
        )
        self.form("", "SelectProjectSecuritySchemeStep2.jspa", {}, previous=confirm)
        assigned = self.rest("GET", f"/rest/api/2/project/{project}/issuesecuritylevelscheme")
        if str(assigned["id"]) != str(scheme_id):
            msg = "Issue security scheme was not assigned"
            raise RuntimeError(msg)
        return level_id


class _Settings(HTMLParser):
    """Read the current values of Jira's general configuration form."""

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}
        self._inside = False
        self._select: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self._inside = values.get("action") == "EditApplicationProperties.jspa"
        if not self._inside:
            return
        name = values.get("name")
        kind = values.get("type") or ""
        if tag == "input" and name and kind not in {"submit", "button", "reset"}:
            if kind not in {"radio", "checkbox"} or "checked" in values:
                self.values[name] = values.get("value") or ""
        elif tag == "select":
            self._select = name
        elif tag == "option" and self._select and ("selected" in values or self._select not in self.values):
            self.values[self._select] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._inside = False
        elif tag == "select":
            self._select = None


def rails(container: str, ruby: str) -> Any:
    """Run Ruby in the OpenProject container and return its JSON result."""
    script = f"result = begin\n{ruby}\nend\nputs 'J2O_ACCEPT ' + JSON.generate(result)"
    output = subprocess.run(
        ["docker", "exec", "-i", container, "bundle", "exec", "rails", "runner", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    ).stdout
    marker = [line for line in output.splitlines() if line.startswith("J2O_ACCEPT ")]
    if len(marker) != 1:
        msg = "OpenProject runner returned no result"
        raise RuntimeError(msg)
    return json.loads(marker[0].removeprefix("J2O_ACCEPT "))


_SSH_SHIM = """#!/usr/bin/env bash
# Run the j2o SSH layer against the local Docker host. Only the configured alias is accepted.
set -euo pipefail
args=("$@")
i=0
while (( i < ${#args[@]} )); do
    case "${args[i]}" in
        -[oipFlOJESBbcDLRW]) i=$((i + 2)) ;;
        -*) i=$((i + 1)) ;;
        *) break ;;
    esac
done
host=${args[i]#*@}
[[ "$host" == "$J2O_ACCEPT_SSH_ALIAS" ]] || { echo "Unexpected host" >&2; exit 255; }
i=$((i + 1))
# OpenSSH also accepts options after the destination.
while (( i < ${#args[@]} )); do
    case "${args[i]}" in
        -[oipFlOJESBbcDLRW]) i=$((i + 2)) ;;
        -*) i=$((i + 1)) ;;
        *) break ;;
    esac
done
command=("${args[@]:i}")
(( ${#command[@]} )) || exit 0
exec bash -c "${command[*]}"
"""

_SCP_SHIM = """#!/usr/bin/env bash
# Copy between the local filesystem and the configured alias, which is the same host.
set -euo pipefail
paths=()
while (( $# )); do
    case "$1" in
        -[oiPFlSJc]) shift 2 ;;
        -*) shift ;;
        *) paths+=("${1#"$J2O_ACCEPT_SSH_ALIAS":}"); shift ;;
    esac
done
exec cp -r -- "${paths[@]}"
"""


def run_j2o(state: Path, checkout: Path, env: dict[str, str], args: list[str], log: Path) -> int:
    """Run the j2o CLI with its data, logs and SSH layer redirected into the private state directory."""
    shims = state / "bin"
    shims.mkdir(mode=0o700, exist_ok=True)
    for name, body in (("ssh", _SSH_SHIM), ("scp", _SCP_SHIM)):
        (shims / name).write_text(body)
        (shims / name).chmod(0o700)
    var = checkout / "var"
    (state / "var").mkdir(mode=0o700, exist_ok=True)
    if not var.exists() and not var.is_symlink():
        var.symlink_to(state / "var")
    elif not var.is_symlink() or var.resolve() != (state / "var").resolve():
        msg = "The checkout already has a var directory; move it aside so company data stays private"
        raise RuntimeError(msg)
    try:
        # Drop the test suite's J2O_* defaults and pytest marker, which make j2o load .env.test.
        inherited = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("J2O_") and key != "PYTEST_CURRENT_TEST"
        }
        environment = {**inherited, **env, "PATH": f"{shims}:{os.environ['PATH']}"}
        with log.open("a") as stream:
            process = subprocess.Popen(
                ["uv", "run", "--no-sync", "j2o", *args],
                cwd=checkout,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                return process.wait(timeout=3600)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=60)
    finally:
        var.unlink()


def start_console(container: str, session: str, irbrc: Path) -> None:
    """Start the persistent tmux Rails console that j2o drives, without an SSH hop."""
    subprocess.run(["docker", "cp", str(irbrc), f"{container}:/app/.irbrc"], check=True, timeout=60)
    if subprocess.run(["tmux", "has-session", "-t", session], check=False, capture_output=True).returncode == 0:
        return
    console = (
        f"docker exec -e IRBRC=/app/.irbrc -e RELINE_OUTPUT_ESCAPES=false -e RELINE_INPUTRC=/dev/null "
        f"-ti {container} bundle exec rails console"
    )
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50", console], check=True)
    # j2o addresses window 0, pane 0, whatever base-index the operator's tmux configuration uses.
    subprocess.run(["tmux", "set-window-option", "-t", session, "pane-base-index", "0"], check=True)
    subprocess.run(["tmux", "move-window", "-s", session, "-t", f"{session}:0"], check=False)
    subprocess.run(["tmux", "display-message", "-p", "-t", session, "ok"], check=True, capture_output=True)
    for _ in range(120):
        pane = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        lines = [line.rstrip() for line in pane.splitlines() if line.strip()]
        if "IRBRC loaded" in pane and lines and lines[-1].endswith(">>"):
            return
        time.sleep(2)
    msg = "Rails console did not start"
    raise RuntimeError(msg)


def stop_console(session: str) -> None:
    """Remove the acceptance test's tmux session."""
    subprocess.run(["tmux", "kill-session", "-t", session], check=False, capture_output=True)
