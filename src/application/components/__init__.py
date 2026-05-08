"""Migration scripts package for the Jira to OpenProject migration.

Every component submodule is imported below so that future
``@register_component`` decorators (added in Phase 2 of the
``migration.py`` split) fire on package import. See
``claudedocs/refactoring/migration-py-split-plan.md``.

Imports are intentionally side-effect-only; the public API of this
package is the :mod:`src.application.components.registry` module.
"""

from src.application.components import (  # noqa: F401  (side-effect imports for decorator registration)
    account_migration,
    admin_scheme_migration,
    affects_versions_migration,
    agile_board_migration,
    attachment_provenance_migration,
    attachment_recovery_migration,
    attachments_migration,
    category_defaults_migration,
    company_migration,
    components_migration,
    custom_field_migration,
    customfields_generic_migration,
    estimates_migration,
    group_migration,
    inline_refs_migration,
    issue_type_migration,
    labels_migration,
    link_type_migration,
    native_tags_migration,
    priority_migration,
    project_migration,
    relation_migration,
    remote_links_migration,
    reporting_migration,
    resolution_migration,
    security_levels_migration,
    simpletasks_migration,
    sprint_epic_migration,
    status_migration,
    story_points_migration,
    tempo_account_migration,
    time_entry_migration,
    user_mapping_backfill_migration,
    user_migration,
    versions_migration,
    votes_migration,
    watcher_migration,
    work_package_content_migration,
    work_package_migration,
    work_package_skeleton_migration,
    workflow_migration,
    wp_defaults,
    wp_metadata_backfill_migration,
)
