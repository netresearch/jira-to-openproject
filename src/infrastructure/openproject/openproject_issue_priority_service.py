"""IssuePriority helpers for the OpenProject Rails console.

Phase 2p of ADR-002 continues the openproject_client.py god-class
decomposition by collecting the small ``IssuePriority`` subsystem onto
a focused service. The service owns:

* ``get_issue_priorities`` — list all ``IssuePriority`` rows (id, name,
  position, default flag, active flag) ordered by ``position``.
* ``find_issue_priority_by_name`` — look up a single priority by name
  or return ``None``.
* ``create_issue_priority`` — create a new priority and return the
  freshly persisted attributes.

``OpenProjectClient`` exposes the service via ``self.priorities`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

from typing import Any

from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.openproject_client import OpenProjectClient


class OpenProjectIssuePriorityService:
    """``IssuePriority`` Rails-console helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    def get_issue_priorities(self) -> list[dict[str, Any]]:
        """Return list of IssuePriority with id, name, position, is_default, active."""
        script = """
        IssuePriority.order(:position).map do |p|
          { id: p.id, name: p.name, position: p.position, is_default: p.is_default, active: p.active }
        end
        """
        try:
            result = self._client.execute_json_query(script)
            return result if isinstance(result, list) else []
        except Exception:
            self._logger.exception("Failed to get issue priorities")
            return []

    def find_issue_priority_by_name(self, name: str) -> dict[str, Any] | None:
        # Lazy import: ``escape_ruby_single_quoted`` lives on the
        # client; lazy keeps the service ↔ client cycle out of
        # module-load time.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        # SECURITY: build a single-quoted Ruby literal. The previous
        # ``json.dumps(name)`` produced a double-quoted Ruby string,
        # which Ruby parses with ``#{...}`` interpolation — a priority
        # name from Jira (untrusted source) could have triggered
        # arbitrary Ruby execution in the Rails console.
        safe_name = escape_ruby_single_quoted(name)
        script = (
            f"p = IssuePriority.find_by(name: '{safe_name}'); "
            "p && { id: p.id, name: p.name, position: p.position, "
            "is_default: p.is_default, active: p.active }"
        )
        try:
            result = self._client.execute_json_query(script)
            return result if isinstance(result, dict) else None
        except Exception:
            self._logger.exception("Failed to find issue priority by name %s", name)
            return None

    def create_issue_priority(
        self,
        name: str,
        position: int | None = None,
        is_default: bool = False,
    ) -> dict[str, Any]:
        # Same security note as ``find_issue_priority_by_name`` —
        # untrusted ``name`` would otherwise interpolate as Ruby.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        pos_expr = "nil" if position is None else str(int(position))
        safe_name = escape_ruby_single_quoted(name)
        script = f"""
        p = IssuePriority.create!(name: '{safe_name}', position: {pos_expr}, is_default: {str(is_default).lower()}, active: true)
        {{ id: p.id, name: p.name, position: p.position, is_default: p.is_default, active: p.active }}
        """
        try:
            result = self._client.execute_json_query(script)
            return result if isinstance(result, dict) else {"id": None, "name": name}
        except Exception as e:
            msg = f"Failed to create issue priority {name}: {e}"
            raise QueryExecutionError(msg) from e
