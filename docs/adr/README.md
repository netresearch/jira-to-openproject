# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) documenting significant technical decisions made during the development of the j2o migration tool.

## Index

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [ADR-001](ADR-001-two-phase-work-package-migration.md) | Two-Phase Work Package Migration | Accepted | 2024-12-11 |
| [ADR-002](ADR-002-target-architecture.md) | Target architecture for j2o | Accepted | 2026-04-29 |
| [ADR-003](ADR-003-rejected-architecture-patterns.md) | Rejected architecture patterns | Accepted | 2026-04-29 |
| [ADR-004](ADR-004-audience-safe-field-history.md) | Audience-safe Jira field history as a structured value | Accepted | 2026-11-20 |
| [ADR-005](ADR-005-project-access-after-memberless-staging.md) | Grant project access after memberless staging | Accepted | 2026-11-20 |
| [ADR-006](ADR-006-lock-inactive-jira-users.md) | Inactive Jira users become locked OpenProject users | Accepted | 2026-11-20 |
| [ADR-007](ADR-007-protected-comments-and-local-acceptance.md) | Protected comments stay out of native journals; acceptance runs locally | Accepted | 2026-11-20 |
| [ADR-008](ADR-008-project-selection-and-fail-closed-anonymous-proofs.md) | One source for the project selection; fail-closed anonymous proofs | Accepted | 2026-11-20 |
| [ADR-009](ADR-009-security-levels-as-private-sub-projects.md) | Issues with a Jira security level become private OpenProject sub-projects | Accepted | 2026-11-20 |
| [ADR-010](ADR-010-level-versions-as-sub-project-copies.md) | Versions of level work packages are copies in their sub-project | Accepted | 2026-11-21 |
| [ADR-011](ADR-011-jira-keys-as-semantic-identifiers.md) | Jira keys become native OpenProject semantic work package identifiers | Accepted | 2026-11-21 |
| [ADR-012](ADR-012-late-access-grant-of-level-sub-projects.md) | Late access grant of the level sub-projects | Accepted | 2026-11-21 |
| [ADR-013](ADR-013-content-rewrites-only-what-j2o-wrote.md) | A re-run writes a value again only while it holds what J2O wrote last | Accepted | 2026-11-22 |
| [ADR-014](ADR-014-tempo-billing-on-time-entries.md) | Tempo billable hours and approval status are time entry custom fields | Accepted | 2026-11-22 |
| [ADR-015](ADR-015-rollback-by-snapshot.md) | A migration run is undone by restoring a snapshot | Accepted | 2026-11-22 |
| [ADR-016](ADR-016-unsafe-attachment-names-stored-safely.md) | An attachment name the target refuses is stored under a safe name | Accepted | 2026-11-23 |
| [ADR-017](ADR-017-list-field-options-from-written-values.md) | List field options come from the values J2O writes | Accepted | 2026-11-23 |

## What is an ADR?

An Architecture Decision Record captures a significant architectural decision made along with its context and consequences. ADRs help:

- Document the "why" behind technical choices
- Provide historical context for future developers
- Enable informed decision-making when revisiting past choices

## ADR Format

Each ADR follows this structure:

1. **Status**: Proposed, Accepted, Deprecated, Superseded
2. **Context**: The situation and constraints that led to the decision
3. **Decision**: The change or choice being made
4. **Consequences**: The results of the decision (both positive and negative)

## Related Documentation

- [Migration Components Catalog](../MIGRATION_COMPONENTS.md) - Component details
- [Entity Mapping Reference](../ENTITY_MAPPING.md) - Jira→OpenProject field mappings
- [Architecture Overview](../ARCHITECTURE.md) - System design
- [Developer Guide](../DEVELOPER_GUIDE.md) - Development standards
