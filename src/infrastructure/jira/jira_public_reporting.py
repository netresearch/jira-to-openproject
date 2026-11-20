# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Read the public representation of a Jira report before publishing its contents."""

from typing import Any
from urllib.parse import urlsplit

import requests


def anonymous_verify(client: Any, url: str) -> str | bool:
    """Return the CA bundle the Jira client's session uses for ``url``, else True.

    Anonymous proofs keep ``trust_env = False``, so they would not see
    ``REQUESTS_CA_BUNDLE``. Never returns False: these reads decide what is public.
    """
    session = getattr(getattr(client, "jira", None), "_session", None)
    merge = getattr(session, "merge_environment_settings", None)
    settings = merge(url, {}, None, None, None) if callable(merge) else None
    verify = settings.get("verify") if isinstance(settings, dict) else None
    return verify if isinstance(verify, str) and verify else True


def public_report(
    base_url: str, kind: str, report_id: str | int | None, *, verify: str | bool = True
) -> dict[str, Any] | None:
    """Return an anonymously readable record, or None when access cannot be confirmed."""
    base = urlsplit(base_url)
    identifier = str(report_id)
    if base.scheme != "https" or not base.hostname or base.username is not None or base.query or base.fragment:
        return None
    if kind not in {"filter", "dashboard"} or not identifier.isascii() or not identifier.isdecimal():
        return None
    url = f"{base_url.rstrip('/')}/rest/api/2/{kind}/{identifier}"
    with requests.Session() as session:
        # A dedicated session also excludes cookies, proxy credentials and .netrc.
        session.trust_env = False
        session.verify = verify
        response = session.get(url, timeout=20, allow_redirects=False)
        # Jira answers 400 "The selected filter is not available to you" for a filter it does not share.
        if response.status_code in {400, 401, 403, 404} or response.is_redirect:
            return None
        response.raise_for_status()
        if response.headers.get("X-AUSERNAME", "").casefold() != "anonymous":
            return None
        if "json" not in response.headers.get("Content-Type", "").lower():
            return None
        data = response.json()
        return data if isinstance(data, dict) and str(data.get("id")) == identifier else None
