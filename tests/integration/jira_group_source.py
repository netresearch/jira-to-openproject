# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Optional native Jira source for the group-transition test, restricted to an isolated Docker fixture."""

import ipaddress
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import requests


def _docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True, timeout=15).strip()


def _isolated_origin(container: str) -> str:
    """Resolve the address of a Jira container on its single named, internal test network."""
    if not container.startswith("j2o-grptest-") or not container.endswith("-jira") or not container.isascii():
        raise ValueError("Expected a disposable j2o-grptest-…-jira container")
    image = _docker("inspect", "--format", "{{.Config.Image}}", container)
    networks = json.loads(_docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", container))
    network = container.removesuffix("-jira")
    if image != "atlassian/jira-software:9.12.3" or set(networks) != {network}:
        raise ValueError("Expected the isolated Jira Software 9.12.3 fixture")
    if _docker("network", "inspect", "--format", "{{.Internal}}", network) != "true":
        raise ValueError("The disposable Jira network must be internal")
    address = ipaddress.IPv4Address(networks[network]["IPAddress"])
    if not address.is_private or address.is_unspecified:
        raise ValueError("Expected a private fixture address")
    return f"http://{address}:8080"


class NativeGroupSource:
    """Forward ordinary bearer-authenticated API reads and change only the synthetic ordinary account."""

    def __init__(self, fixture: Path):
        config = json.loads(fixture.read_text())
        self.origin = _isolated_origin(config["container"])
        self.issue = config["issue"]
        self.issue_id = config["issue_id"]
        self.public_id = config["public_id"]
        self.protected_id = config["protected_id"]
        self.tokens = config["tokens"]
        self.principals = config["principals"]
        self.inventory_token = config["inventory_token"]
        self.offline = False
        self.session = requests.Session()
        self.session.trust_env = False
        # The test redirects target requests into OpenProject Rack after fixture construction.
        self.send = self.session.send
        actual_version = self._request("GET", "/rest/api/2/serverInfo", self.tokens["reader"]).json()["version"]
        if actual_version != "9.12.3":
            raise ValueError("Expected Jira Software 9.12.3 for this fixture")

    def _request(self, method, path, token, **kwargs):
        request = requests.Request(method, self.origin + path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        response = self.send(request.prepare(), timeout=(5, 45), allow_redirects=False)
        response.raise_for_status()
        return response

    def _is_member(self):
        token = self.tokens["reader"]
        user = self._request("GET", "/rest/api/2/myself", token, params={"expand": "groups"}).json()
        if user["name"] != "groupverifierreader" or user["key"] != self.principals["reader"]:
            raise ValueError("Reader token does not belong to the expected disposable account")
        permissions = self._request("GET", "/rest/api/2/mypermissions", token, params={"projectKey": "GROUP"}).json()[
            "permissions"
        ]
        if any(permissions[p]["havePermission"] for p in ("SYSTEM_ADMIN", "ADMINISTER", "PROJECT_ADMIN")):
            raise ValueError("The group-switch reader must be an ordinary Jira account")
        return "fixture-reviewers" in {row["name"] for row in user["groups"]["items"]}

    def set_membership(self, *, member):
        if self._is_member() != member:
            path = "/rest/api/2/group/user"
            group = {"groupname": "fixture-reviewers"}
            if member:
                self._request("POST", path, self.inventory_token, params=group, json={"name": "groupverifierreader"})
            else:
                self._request("DELETE", path, self.inventory_token, params={**group, "username": "groupverifierreader"})
        assert self._is_member() == member

    def respond(self, request):
        assert not self.offline, "Offline archive reads must not contact Jira"
        assert urlsplit(request.url).netloc == "jira.invalid"
        assert request.method == "GET"
        assert request.path_url.startswith("/rest/api/2/")
        native = request.copy()
        native.url = self.origin + request.path_url
        return self.send(native, timeout=(5, 45), allow_redirects=False)
