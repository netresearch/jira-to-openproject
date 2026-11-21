# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Detect the OpenProject edition and list the administrators (ADR-009, decisions 2 and 4).

Source references are to opf/openproject v17.9.7.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

FEATURES = ("work_package_sharing", "internal_comments")
# ``active_tokens`` is memoised in RequestStore (app/models/enterprise_token.rb:38-42), which a
# long-lived console never resets; clear it so a token added after the console started counts.
# ``allows_to?`` is false without a token, for an expired token or an invalid domain
# (app/services/authorization/enterprise_service.rb:39-46).
_RAILS_EDITION = (
    "EnterpriseToken.clear_current_tokens_cache; "
    '{"host_name" => Setting.host_name, '
    + ", ".join(f'"{name}" => EnterpriseToken.allows_to?(:{name})' for name in FEATURES)
    + "}"
)
# SystemUser is a User subclass created with admin: true (app/models/system_user.rb), so the
# type is pinned. The default scope already hides deleted principals (app/models/principal.rb:35).
_RAILS_ADMINS = (
    'User.where(type: "User", admin: true).order(:id)'
    '.map { |u| {"id" => u.id, "login" => u.login, "status" => u.status} }'
)
STATUSES = frozenset({"active", "registered", "locked", "invited"})


class EditionError(ValueError):
    """An edition or administrator answer could not be verified."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise EditionError(message)


class RailsReader(Protocol):
    """The JSON path of the Rails console (``OpenProjectClient.execute_json_query``)."""

    def execute_json_query(self, query: str, timeout: int | None = None) -> object: ...


class ApiReader(Protocol):
    """An authenticated, origin-bound OpenProject reader (``src.comment_archive.transport.Endpoint``)."""

    def get(self, path: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class Edition:
    """The detected edition and both raw answers (feature names only)."""

    enterprise: bool
    rails: dict[str, bool]
    available_features: tuple[str, ...]

    @property
    def name(self) -> str:
        return "enterprise" if self.enterprise else "community"


def detect_edition(rails: RailsReader, api: ApiReader) -> Edition:
    """Enterprise only when Rails and ``GET /api/v3/configuration`` both allow work package sharing.

    ``availableFeatures`` is ``EnterpriseToken.available_features``
    (lib/api/v3/configuration/configuration_representer.rb:106-109); its names are the
    Rails feature names (the frontend checks ``allowsTo('work_package_sharing')``). A
    disagreement means Community; a failed or malformed answer raises.
    """
    answer = rails.execute_json_query(_RAILS_EDITION)
    _require(
        isinstance(answer, dict)
        and set(answer) == {"host_name", *FEATURES}
        and all(type(answer[name]) is bool for name in FEATURES),
        "Malformed Enterprise token answer from Rails",
    )
    configuration = api.get("/api/v3/configuration")
    _require(isinstance(configuration, dict), "Malformed OpenProject configuration")
    features = configuration.get("availableFeatures")
    _require(
        isinstance(features, list) and all(isinstance(name, str) for name in features),
        "Malformed availableFeatures in the OpenProject configuration",
    )
    # Both sources must describe the same instance (configuration_representer.rb hostName).
    _require(
        isinstance(answer["host_name"], str) and answer["host_name"] == configuration.get("hostName"),
        "Rails console and API answer for different OpenProject hosts",
    )
    rails_features = {name: answer[name] for name in FEATURES}
    result = Edition(
        rails_features["work_package_sharing"] and "work_package_sharing" in features,
        rails_features,
        tuple(sorted(features)),
    )
    for name in FEATURES:
        if rails_features[name] != (name in features):
            logger.warning("Rails and availableFeatures disagree on %s; treating it as unavailable", name)
    logger.info(
        "OpenProject edition: %s (Rails %s, availableFeatures %s)",
        result.name,
        rails_features,
        list(result.available_features),
    )
    return result


def administrators(rails: RailsReader) -> list[dict[str, Any]]:
    """Every non-deleted OpenProject administrator with ID, login and status."""
    rows = rails.execute_json_query(_RAILS_ADMINS)
    _require(isinstance(rows, list), "Malformed administrator list")
    for row in rows:
        _require(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and isinstance(row.get("login"), str)
            and row["login"]
            and row.get("status") in STATUSES,
            "Malformed administrator list",
        )
    _require(len({row["id"] for row in rows}) == len(rows), "Duplicate administrator")
    return rows
