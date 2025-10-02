# ADR: Project & Issue Metadata Validation Strategy

**Status:** Accepted

## Context

Project and work-package migrations now enrich OpenProject with Jira provenance, lead memberships, module enablement, and start-date propagation. Manual SRVAC rehearsals show the flows work end-to-end, but automated validation is still thin:

- Start-date precedence (customfield_18690/12590/11490/15082) relies on integration runs; no unit coverage ensures the migrator chooses the right field or gracefully skips missing values.
- Project lead role assignment and module enablement depend on OpenProject API responses; current code paths are untested and may regress without alert.
- `scripts/data_qa.py` reports module names and start-date stats only when caches are fresh; stale artefacts make rehearsal reviews noisy.
- Docs mention richer metadata but stop short of explaining the validation checks operators should run.

## Decision

1. Add focused unit tests for start-date extraction and mapping so precedence, normalization, and missing-field behavior stay deterministic.
2. Introduce unit tests around project lead membership assignment (happy-path, missing mapping, repeated runs) using fakes/mocks for the OpenProject client.
3. Harden `scripts/data_qa.py` to surface when cached project snapshots lack module data or when start-date coverage stays at 0%, nudging operators to rerun the relevant components.
4. Update README and scoped AGENTS guidance with the new validation expectations (data QA script usage, start-date precedence, module enablement) so rehearsal operators follow the same checklist.

## Consequences

- Requires new fixtures/mocks under `tests/unit` to emulate Jira issues and OpenProject responses.
- Slightly longer local test runtime (new unit tests only; still seconds).
- QA script becomes louder when caches are stale; documentation must call out the remedy (`migrate --components projects`/`work_packages`).

## Action Items

- [x] Add start-date precedence tests in `tests/unit/test_enhanced_timestamp_migrator.py`.
- [x] Add work-package level test ensuring `_resolve_start_date` respects precedence and integrates with migrator output.
- [x] Add project migration tests validating `_assign_project_lead` idempotency and module enablement invocation.
- [x] Extend `scripts/data_qa.py` with warnings for missing module snapshots / zero start-date coverage.
- [x] Refresh `README.md` and `src/AGENTS.md` with explicit validation guidance.
- [x] Mark this ADR *Accepted* once tests/docs land.
