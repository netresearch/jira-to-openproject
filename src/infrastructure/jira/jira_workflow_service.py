"""Jira workflow configuration queries.

Phase 3b of ADR-002 continues the jira_client.py decomposition. The
workflow-related methods (workflow scheme listing, transition lookup,
status lookup) move into a focused service.

The service is exposed on ``JiraClient`` as ``self.workflows`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like ``JiraProjectService`` this is HTTP-only — calls go
through the ``jira`` SDK or ``JiraClient._make_request`` — so there is
no Ruby-script escaping to worry about.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from requests import exceptions

from src import config
from src.infrastructure.jira.jira_client import (
    HTTP_NOT_FOUND,
    JiraApiError,
    JiraClient,
    JiraConnectionError,
)


def _is_workflow_404(exc: BaseException) -> bool:
    """Return True if *exc* or any exception in its ``__cause__`` chain is a Jira 404.

    In production ``JiraClient._patch_jira_client`` wraps every exception
    (including ``JIRAError``) into ``JiraApiError`` so the outer exception is
    never a ``JIRAError`` directly.  The original ``JIRAError(status_code=404)``
    is stored as ``exc.__cause__``.  Walking the chain makes the check work
    for both the bare-``JIRAError`` path (unit-test stub, some alternate code
    paths) and the production-wrapping path.

    The *seen* set guards against pathological cycles where ``__cause__`` is
    set to the exception itself.
    """
    from jira.exceptions import JIRAError as _JIRAError

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _JIRAError) and getattr(current, "status_code", None) == HTTP_NOT_FOUND:
            return True
        current = current.__cause__
    return False


class JiraWorkflowService:
    """Workflow-domain queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_workflow_schemes(self) -> list[dict[str, Any]]:
        """Return configured Jira workflow schemes with issue type mappings."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        url = f"{client.base_url}/rest/api/2/workflowscheme"
        self._logger.info("Fetching Jira workflow schemes")

        try:
            response = client.jira._session.get(url)
            response.raise_for_status()
            payload = response.json()
            values = payload.get("values") if isinstance(payload, dict) else None
            schemes = values if isinstance(values, list) else []
            self._logger.info("Retrieved %s workflow schemes", len(schemes))
            return schemes
        except exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 405:
                self._logger.warning(
                    "GET /rest/api/2/workflowscheme unsupported, falling back to per-project workflow inspection",
                )
                return self._get_workflow_schemes_per_project()
            error_msg = f"Failed to fetch workflow schemes: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc
        except JiraApiError as exc:
            # ``JiraClient._handle_response`` raises ``JiraApiError`` with
            # messages like ``"HTTP Error 405: ..."`` (note the word
            # "Error"), but the patched session in some paths raises
            # ``"HTTP 405: ..."`` directly. Match both forms so the
            # per-project fallback fires regardless of which path
            # produced the exception. Pre-extraction code only checked
            # the second form, which silently never matched in
            # production.
            exc_text = str(exc)
            if "HTTP Error 405" in exc_text or "HTTP 405" in exc_text:
                self._logger.warning(
                    "Workflow scheme endpoint returned 405; using per-project fallback",
                )
                return self._get_workflow_schemes_per_project()
            raise
        except Exception as exc:
            error_msg = f"Failed to fetch workflow schemes: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc

    def _get_workflow_schemes_per_project(self) -> list[dict[str, Any]]:
        """Fallback that assembles workflow schemes via project endpoints."""
        client = self._client
        project_keys: list[str] = []
        try:
            project_mapping = config.mappings.get_mapping("project") or {}
            project_keys = [str(key) for key in project_mapping]
        except Exception:
            project_keys = []

        if not project_keys:
            try:
                projects = client.get_projects()
                project_keys = [str(p.get("key")) for p in projects if p.get("key")]
            except Exception:
                project_keys = []

        schemes_by_id: dict[str, dict[str, Any]] = {}
        for key in project_keys:
            if not key:
                continue
            try:
                response = client._make_request(f"/rest/api/2/project/{key}/workflowscheme")
                if response.status_code == HTTP_NOT_FOUND:
                    continue
                response.raise_for_status()
                payload = response.json() or {}
            except Exception as exc:
                self._logger.debug("Failed to fetch workflow scheme for project %s: %s", key, exc)
                continue

            scheme = payload.get("workflowScheme") or payload
            if not isinstance(scheme, dict):
                continue

            scheme_id = str(scheme.get("id") or scheme.get("name") or key)
            existing = schemes_by_id.get(scheme_id)
            if existing:
                mappings = existing.setdefault("issueTypeMappings", {})
                if isinstance(mappings, dict):
                    new_mappings = scheme.get("issueTypeMappings") or {}
                    if isinstance(new_mappings, dict):
                        mappings.update(new_mappings)
                existing.setdefault("projects", set()).add(key)
            else:
                entry = dict(scheme)
                entry["projects"] = {key}
                schemes_by_id[scheme_id] = entry

        for entry in schemes_by_id.values():
            projects = entry.get("projects")
            if isinstance(projects, set):
                entry["projects"] = sorted(projects)

        self._logger.info(
            "Discovered %s workflow schemes via per-project fallback",
            len(schemes_by_id),
        )
        return list(schemes_by_id.values())

    def get_workflow_transitions(self, workflow_name: str) -> list[dict[str, Any]]:
        """Return transitions for a given Jira workflow name."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        safe_name = quote(workflow_name, safe="")
        url = f"{client.base_url}/rest/api/2/workflow/{safe_name}/transitions"
        self._logger.debug("Fetching Jira workflow transitions for '%s'", workflow_name)

        try:
            response = client.jira._session.get(url)
            # Defensive check for callers that return a response object
            # with status_code rather than raising.  The ``jira`` library's
            # ResilientSession raises JIRAError before reaching this point,
            # so this branch is a belt-and-suspenders guard only.
            if getattr(response, "status_code", None) == HTTP_NOT_FOUND:
                self._logger.debug(
                    "Workflow '%s' returned 404 for transitions; treating as empty",
                    workflow_name,
                )
                return []
            response.raise_for_status()
            payload = response.json()
            transitions = payload.get("transitions") if isinstance(payload, dict) else payload
            if not isinstance(transitions, list):
                self._logger.warning("Unexpected workflow transitions payload for %s", workflow_name)
                return []
            self._logger.debug(
                "Workflow '%s' returned %s transitions",
                workflow_name,
                len(transitions),
            )
            return transitions
        except Exception as exc:
            # The ``jira`` library's ResilientSession raises JIRAError(status_code=404)
            # before returning any response object, so the status-code check above
            # never fires for real 404s.  Catch it here instead and suppress at
            # DEBUG level — these endpoints do not exist on many Server/DC versions
            # and 404 is expected, not an error worth alarming on.
            #
            # In production JiraClient._patch_jira_client wraps every exception
            # into JiraApiError, so exc is never a JIRAError directly — the
            # original JIRAError(status_code=404) lives in exc.__cause__.
            # _is_workflow_404 walks the __cause__ chain to handle both paths.
            if _is_workflow_404(exc):
                self._logger.debug(
                    "Workflow '%s' transitions endpoint returned 404 (not available on this server); treating as empty",
                    workflow_name,
                )
                return []
            error_msg = f"Failed to fetch transitions for workflow '{workflow_name}': {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc

    def get_workflow_statuses(self, workflow_name: str) -> list[dict[str, Any]]:
        """Return statuses referenced by a workflow."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        safe_name = quote(workflow_name, safe="")
        url = f"{client.base_url}/rest/api/2/workflow/{safe_name}"
        self._logger.debug("Fetching Jira workflow definition for '%s'", workflow_name)

        try:
            response = client.jira._session.get(url)
            # Defensive check for callers that return a response object
            # with status_code rather than raising.  The ``jira`` library's
            # ResilientSession raises JIRAError before reaching this point,
            # so this branch is a belt-and-suspenders guard only.
            if getattr(response, "status_code", None) == HTTP_NOT_FOUND:
                self._logger.debug(
                    "Workflow '%s' returned 404 for definition; treating as empty",
                    workflow_name,
                )
                return []
            response.raise_for_status()
            workflow = response.json()
            if isinstance(workflow, dict):
                statuses = workflow.get("statuses")
                if isinstance(statuses, list):
                    return statuses
            self._logger.warning(
                "Unexpected workflow status payload for %s (type=%s)",
                workflow_name,
                type(workflow).__name__,
            )
            return []
        except Exception as exc:
            # The ``jira`` library's ResilientSession raises JIRAError(status_code=404)
            # before returning any response object, so the status-code check above
            # never fires for real 404s.  Catch it here instead and suppress at
            # DEBUG level — these endpoints do not exist on many Server/DC versions
            # and 404 is expected, not an error worth alarming on.
            #
            # In production JiraClient._patch_jira_client wraps every exception
            # into JiraApiError, so exc is never a JIRAError directly — the
            # original JIRAError(status_code=404) lives in exc.__cause__.
            # _is_workflow_404 walks the __cause__ chain to handle both paths.
            if _is_workflow_404(exc):
                self._logger.debug(
                    "Workflow '%s' definition endpoint returned 404 (not available on this server); treating as empty",
                    workflow_name,
                )
                return []
            error_msg = f"Failed to fetch workflow definition for '{workflow_name}': {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc
