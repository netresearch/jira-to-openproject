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
