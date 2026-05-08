"""Component registry for migration component factories.

Phase 1 scaffold: this module provides a registration API alongside the
existing direct-import factory in :mod:`src.migration`. Components are
not yet decorated with ``@register_component`` — that happens in Phase 2.

For now ``DEFAULT_COMPONENT_SEQUENCE`` and ``PREDEFINED_PROFILES`` live
here so future orchestrator extraction can import them from the
application layer rather than the legacy ``src.migration`` god-file.
``src.migration`` re-exports both names for backward compatibility.

See ``claudedocs/refactoring/migration-py-split-plan.md`` for the full
phased plan.
"""

from __future__ import annotations

from collections.abc import Callable

from src.application.components.base_migration import BaseMigration
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.type_definitions import ComponentName

ComponentFactory = Callable[[JiraClient, OpenProjectClient], BaseMigration]

_REGISTRY: dict[ComponentName, ComponentFactory] = {}


def register_component(name: ComponentName) -> Callable[[type], type]:
    """Class decorator that registers a migration component under ``name``.

    The decorated class must accept ``jira_client`` and ``op_client`` as
    keyword arguments — the same shape every existing
    :class:`~src.application.components.base_migration.BaseMigration`
    subclass already uses.
    """

    def _decorator(cls: type) -> type:
        _REGISTRY[name] = lambda j, o: cls(jira_client=j, op_client=o)
        return cls

    return _decorator


def register_factory(name: ComponentName, factory: ComponentFactory) -> None:
    """Register an explicit factory function under ``name``.

    Useful for components that need non-default construction, or for
    tests that want to substitute a fake.
    """
    _REGISTRY[name] = factory


def build_factories(
    jira: JiraClient,
    op: OpenProjectClient,
) -> dict[ComponentName, Callable[[], BaseMigration]]:
    """Bind every registered factory to the given clients.

    Returns a dict mapping component name to a zero-arg factory the
    orchestrator can call lazily, matching the shape of the legacy
    ``_build_component_factories`` in :mod:`src.migration`.
    """
    return {name: (lambda f=f: f(jira, op)) for name, f in _REGISTRY.items()}


def known_components() -> set[ComponentName]:
    """Return the set of component names currently registered."""
    return set(_REGISTRY)


# === Component sequencing ===
#
# Two-Phase Migration Sequence for Proper Attachment URL Conversion
# ==================================================================
# Phase 1 (work_packages_skeleton): Creates WP structure without content
# Phase 2 (attachments): Uploads files and creates attachment_mapping.json
# Phase 3 (work_packages_content): Populates descriptions/comments with resolved attachment URLs
#
# This ensures !image.png! references in Jira convert to proper OP API URLs:
# /api/v3/attachments/{id}/content
#
DEFAULT_COMPONENT_SEQUENCE: list[ComponentName] = [
    # === Foundation: Users & Groups ===
    "users",
    # Retroactively pull missing Jira identities into the user mapping
    # before any consumer tries to resolve them. Sources: previous
    # migration_results' ``unmapped_users`` lists + cached issue
    # author/assignee/reporter/watcher fields. Idempotent — re-runs
    # only add what's not already mapped.
    "user_mapping_backfill",
    "groups",
    # === Metadata: Field Definitions ===
    "custom_fields",
    "priorities",
    "link_types",
    "issue_types",
    "status_types",
    "resolutions",
    # === Organization: Companies & Accounts (Tempo) ===
    "companies",
    "accounts",
    # === Structure: Projects ===
    "projects",
    # === Agile: Workflows, Boards, Sprints ===
    "workflows",
    "agile_boards",
    "sprint_epic",
    # === Phase 1: Work Package Skeletons (no content) ===
    "work_packages_skeleton",
    # === Phase 2: Attachments (creates mapping for URL conversion) ===
    "attachments",
    "attachment_provenance",
    # Re-attempt attachments missing in OP after the main attachments
    # run (transient mid-batch errors leave the partial-success
    # classifier green; the original migration is idempotent
    # Rails-side, so re-running it for the affected jira_keys fills
    # the gap). Idempotent: no-op when OP already has every file.
    "attachment_recovery",
    # === Phase 3: Work Package Content (with resolved attachment URLs) ===
    "work_packages_content",
    # Backfill assignee + provenance CFs on existing WPs (closes Bug A
    # for pre-#175 WPs and the under-populated CF warnings flagged by
    # the audit; idempotent — only sets fields where the OP value is
    # currently null/blank).
    "wp_metadata_backfill",
    # === Post-WP Data: Versions, Components, Labels ===
    "versions",
    "components",
    "labels",
    "native_tags",
    # === WP Metadata: Estimates, Story Points, Security ===
    "story_points",
    "estimates",
    "security_levels",
    "affects_versions",
    "customfields_generic",
    # === WP Relationships ===
    "relations",
    "remote_links",
    "inline_refs",
    # === WP Engagement: Watchers, Votes ===
    "watchers",
    "votes_reactions",
    # === Time Tracking ===
    "time_entries",
    # === Finalization ===
    "category_defaults",
    "admin_schemes",
    "reporting",
]

PREDEFINED_PROFILES: dict[str, list[ComponentName]] = {
    "full": DEFAULT_COMPONENT_SEQUENCE.copy(),
    "metadata_refresh": [
        "projects",
        "issue_types",
        "status_types",
        "workflows",
        "agile_boards",
        "sprint_epic",
        "admin_schemes",
        "reporting",
    ],
}
