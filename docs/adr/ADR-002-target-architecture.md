# ADR-002: Target architecture for j2o

## Status

Accepted (2026-04-29)

Companion to [ADR-003](ADR-003-rejected-architecture-patterns.md) which records what we explicitly chose **not** to do. Detailed rationale, findings, and migration plan: [`docs/decisions/2026-04-29-architecture-review.md`](../decisions/2026-04-29-architecture-review.md).

## Context

j2o is a 53,968-LOC Jira → OpenProject migration tool with 41 migration components, two large API clients, and a FastAPI dashboard. Tests are green (929 passing) and mypy is clean on `src/`. The codebase is functionally solid but has accumulated four structural problems that compound:

1. **God-modules**. Five files exceed 2,000 LOC: `openproject_client.py` (7,266), `work_package_migration.py` (4,688), `enhanced_user_association_migrator.py` (2,960), `jira_client.py` (2,852), and `project_migration.py` (1,729). Each mixes 3–5 concerns (transport, business mapping, Ruby script templating, caching, persistence).
2. **Untyped dict-as-DTO pipeline**. 784 occurrences of `dict[str, Any]` in `src/`, only 6 Pydantic models in the domain. The 21 mypy error codes disabled on `src.clients.*` / `src.migrations.*` are load-bearing — they paper over a structural problem rather than enabling pragmatism.
3. **`BaseMigration` is a god-class** (978 LOC). It owns lifecycle, ETL skeleton, change detection, snapshotting, thread-safe LRU caching, JSON IO, custom-field helpers, batch-issue merging, and Jira-key parsing. Every subclass inherits the union.
4. **Import-time side effects in `src/config/__init__.py`**. `from src import config` calls `mkdir`, attaches log handlers, and prunes old log files. This drives the 49 `TYPE_CHECKING` blocks in `migration.py` alone (42 files use `TYPE_CHECKING` overall) — circular-import pressure radiating from the config module.

## Decision

Adopt a four-layer architecture with a fifth bootstrap/wiring concern:

```
cli + dashboard          (presentation — thin)
    ↓
application              (orchestrator, ETL pipeline, components)
    ↓
domain                   (pure data + Pydantic models, no IO, no framework)
    ↓
infrastructure           (gateways, persistence, observability)
    ↓
config + bootstrap + container   (composition root, side-effects only here)
```

**Hard rule**: dependency arrows only go down. `domain` imports nothing from j2o except stdlib + Pydantic. `application` depends on Protocols from `domain`, never on concrete `infrastructure` classes. Concrete wiring lives in `container.py` only.

### Patterns adopted

| Pattern               | Where                                                            |
| --------------------- | ---------------------------------------------------------------- |
| **Template Method**   | `application/pipeline.py:ETLPipeline[S, T, R]` (generic)         |
| **Strategy**          | One file per component under `application/components/`           |
| **Adapter**           | `infrastructure/jira/gateway.py`, `infrastructure/openproject/api/`  |
| **Facade**            | `infrastructure/openproject/__init__.py` (thin, ≤ 200 LOC)       |
| **Decorator**         | `infrastructure/*/decorators.py` (replaces `Enhanced*Client` subclasses) |
| **Repository**        | `infrastructure/persistence/mapping_repo.py` over `MappingRepository` Protocol |
| **Builder**           | `infrastructure/openproject/rails/builder.py` for Ruby scripts   |
| **Observer**          | `ProgressEmitter` Protocol — already informally present          |
| **Memento**           | `infrastructure/persistence/snapshot.py`                          |
| **Result type**       | `domain/results.py` — discriminated union (Success / Skipped / Failed) |
| **Branded types**     | `domain/ids.py` — `JiraIssueKey`, `OpenProjectId`, `OpUserId`, etc. |

### Type-safety strategy

- Pydantic v2 models for **5–8 highest-traffic entities** at module boundaries (`JiraIssue`, `JiraProject`, `JiraUser`, `OpWorkPackage`, `OpProject`, `OpUser`, `OpCustomField`, mapping entries).
- Branded ID types (`NewType`) prevent ID mixing at type-check time, zero runtime cost.
- Discriminated-union `ComponentResult` replaces today's `success: bool + errors: list + details: dict` shape and the defensive `_component_has_errors` / `_extract_counts` helpers.
- Mypy strictness escalates per-package: full strict on `domain/` and `application/` after Phase 7; `infrastructure/jira` and `infrastructure/openproject/api` stay pragmatic (the JSON border).

### Migration plan (per-phase)

| Phase | Scope                                                                 | PRs | Effort   |
| ----- | --------------------------------------------------------------------- | --- | -------- |
| 0     | This ADR + ADR-003                                                    | 1   | ~1 day   |
| 1     | Decompose `BaseMigration` (extract `EntityCache`, `JsonStore`, CF helpers, change-detection runner) | 4   | ~1 week  |
| 2     | Split god-clients (`OpenProjectClient`, `JiraClient`) along transport/Rails/file-transfer seams | 4–5 | ~2 weeks |
| 3     | Domain models for top 5–8 entities + 3 reference components migrated end-to-end | 3–4 | ~1.5 weeks |
| 4     | Repository pattern: `MappingRepository` Protocol + JSON adapter        | 2   | ~1 week  |
| 5     | Layer reorganization (file moves: `migrations/` → `application/components/`, `clients/` → `infrastructure/`) | 3–4 | ~1.5 weeks |
| 6     | Inert `src/config` + explicit `bootstrap()` + drop now-unneeded `TYPE_CHECKING` blocks | 2   | ~3 days  |
| 7     | Migrate remaining 38 components to typed pipeline (incl. WP monster) | 5–8 | ~3 weeks |
| 8     | Polish: composed Decorators replacing `Enhanced*Client` subclasses; discriminated-union results | 2–3 | ~1 week  |

Each phase lands on a green build, is independently valuable (can stop after any phase), and is bounded by PR-sized units.

## Consequences

### Positive

- **God-modules disappear**. Largest single file ≤ 1,500 LOC (down from 7,266).
- **Type-safe seams**. `dict[str, Any]` count in `src/application/` ≤ 50 (down from 784 codebase-wide).
- **Tests get faster and simpler**. `FakeMappingRepository` and Protocol-based stubs replace global monkeypatching of `cfg.mappings`.
- **Cyclic-import pressure drops**. `TYPE_CHECKING` blocks reduce from 49 in the orchestrator to ≤ 5; from 42 files codebase-wide to ≤ 10.
- **Mypy escape hatch shrinks**. The 21 disabled error codes drop to ≤ 8, applied only at the JSON-border infrastructure modules.
- **`config` becomes inert**. `from src.config import Settings` has no IO side effects. Bootstrap is explicit at entry points.
- **Future storage swap is one file**. `JsonFileMappingRepository` → `SQLiteMappingRepository` is a Protocol implementation, not a rewrite.

### Negative

- **Eight phases, ~10 weeks**. Real engineering investment, sustained against a moving codebase. Each phase must merge cleanly with concurrent work.
- **One-time data normalization** in Phase 3 to unify the polymorphic `wp_map[key]: dict | int` shape into `WorkPackageMappingEntry`. Persisted JSON in users' `var/data/` directories needs a forward-only migration script.
- **Brief code duplication during Phase 2**. The Facade `OpenProjectClient` co-exists with new gateway classes for backward compatibility before old call sites are migrated.
- **`config.mappings` proxy stays through Phase 6** as a deprecated shim. Removed in Phase 7.
- **Review bandwidth**. ~30 PRs over 10 weeks. Requires sustained reviewer attention.

### Neutral

- **No public API change** for the CLI or dashboard until Phase 5 file moves; even then, `src.main:main` and `src.dashboard.app:app` entry points are stable.
- **Test count must stay ≥ 929 at every phase boundary** as the hard gate.

## Implementation entry points

- Full review with findings, target diagrams, and pattern matrix: [`docs/decisions/2026-04-29-architecture-review.md`](../decisions/2026-04-29-architecture-review.md)
- Companion rejection ADR: [ADR-003](ADR-003-rejected-architecture-patterns.md)
- Phase 1 starts with extracting `EntityCache` from `BaseMigration` into `src/utils/entity_cache.py`.
