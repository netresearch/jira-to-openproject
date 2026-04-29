# ADR-003: Rejected architecture patterns

## Status

Accepted (2026-04-29)

Companion to [ADR-002](ADR-002-target-architecture.md) which records the architecture we **are** adopting. This ADR records the architectures and patterns we considered and **explicitly rejected**, so future contributors don't re-litigate them.

## Context

When a codebase shows architectural drift, the temptation is to reach for the most ambitious available pattern: full hexagonal/clean architecture, DDD aggregates, CQRS, a DI framework, a big-bang rewrite. Each of these would solve some real problem in j2o. None of them is the right tool for j2o's actual shape.

This ADR captures **why** each rejected option was rejected, on the record, so the team can point at it next time the discussion comes up.

## Decisions

### 1. Reject DDD aggregates / value objects with invariants

**What was considered**: Treat Jira and OpenProject entities as DDD aggregates with rich behaviour and enforced invariants — e.g., `JiraIssue.assign(user)` validating that the user has the right role, or `OpWorkPackage.transition_to(status)` enforcing workflow rules.

**Why rejected**:
- j2o is an integration tool, not a domain-rich product. The invariants we care about live in the source and target systems (Jira and OpenProject), not in j2o.
- Our job is to transport data shape correctly. Forcing aggregates would create ceremony without insight.
- DDD pays off when "an Order with no LineItems is invalid" or "a Customer's credit limit must hold at checkout." j2o's invariants are mostly "this Jira issue key maps to that OP work package id" — that's a persistence concern, not a domain one.

**What we do instead**: `domain/` holds Pydantic models for shape validation at boundaries. No methods beyond what Pydantic gives us for free.

### 2. Reject CQRS / event sourcing

**What was considered**: Separate read and write models. Treat each migration as a stream of events. Replay events to reconstruct state.

**Why rejected**:
- ETL is inherently command-shaped (extract → transform → load). There is no read/write asymmetry to exploit.
- The current checkpoint/snapshot mechanism already provides the relevant resume capability for a fraction of the cost.
- Event sourcing adds an entire infrastructure layer (event store, projections, replay) for a use case where one-shot batch runs and a SQLite checkpoint table cover the requirement.

**What we do instead**: keep the existing checkpoint/snapshot pattern. Formalize the `SnapshotStore` as Memento pattern in `infrastructure/persistence/`.

### 3. Reject a DI framework

**What was considered**: Adopt `dependency-injector`, `lagom`, `wireup`, or similar.

**Why rejected**:
- Frameworks pay off at ~100+ wiring points or when runtime swap of implementations is needed. j2o has ~50 wiring points (10 infrastructure adapters × ~5 component groups).
- A 200-line hand-written `container.py` composition root is more readable than any framework's config DSL — and easier to step through in a debugger.
- Adds a learning curve and a constraint on the team for marginal value.

**What we do instead**: a single `src/container.py` composition-root module that builds and wires everything once at startup, exposes a typed `AppContainer` dataclass. Constructors take their dependencies as parameters; tests pass fakes directly.

### 4. Reject full hexagonal architecture (ports + adapters everywhere)

**What was considered**: Wrap every external interaction in a Port (Protocol) and an Adapter, including logging, file IO, time, environment variables.

**Why rejected**:
- Diminishing returns past the high-traffic boundaries. Wrapping `time.time()` or `pathlib.Path.exists()` in a Protocol adds layers without preventing real bugs.
- Test suite already runs in <30s. We don't need every dependency mockable to maintain test speed.
- Over-application of hexagonal becomes its own form of coupling — every change requires touching the Port, the Adapter, and the call site.

**What we do instead**: Protocols only at the **high-traffic, integration-shaped boundaries**: `JiraGateway`, `OpenProjectGateway`, `MappingRepository`, `CheckpointStore`, `EntityCache`, `ProgressEmitter`. Direct stdlib usage everywhere else.

### 5. Reject a big-bang rewrite

**What was considered**: One mega-PR (or a long-lived branch) that lands the entire target architecture at once.

**Why rejected**:
- The 929-test safety net works *because* changes are small enough to debug. A 50k-LOC rewrite hides too many ways to break things.
- Each of the 8 phases delivers value alone. After Phase 3 we have typed domain models for 3 components — already a measurable improvement, even if Phases 4–8 never happen.
- A 50-PR mega-diff gets rubber-stamped or rejected. Eight focused phase-PRs get real review.
- The codebase is being actively developed. Long-lived rewrite branches get out of sync within weeks.

**What we do instead**: 8 phases, 30+ PRs, each landing on green tests, each independently valuable. See [ADR-002](ADR-002-target-architecture.md) for the phase plan.

### 6. Reject async-first rewrite

**What was considered**: Migrate the orchestrator and components to `async def`, integrate with `aiohttp`/`httpx` async clients.

**Why rejected**:
- The bottleneck is external API latency (Jira REST, OpenProject API, Rails console), not Python event-loop throughput.
- Existing `tenacity` retry, `pybreaker` circuit-breaker, and `RailsConsoleClient` (synchronous tmux interaction) are battle-tested and synchronous.
- The dashboard is already async (FastAPI + WebSocket); migration code is already sync. The boundary works.
- An async migration of 41 components × ~50 sync API call sites each is weeks of churn for marginal latency improvement.

**What we do instead**: keep migration code sync. Dashboard stays async. Optimize via batching and caching at the gateway layer (already in place via `EnhancedJiraClient` / `EnhancedOpenProjectClient` — to be replaced with composed `Decorators` in Phase 8).

### 7. Reject replacing the Ruby-on-Rails-console interaction

**What was considered**: Replace the tmux-based Rails console RPC with a proper RPC mechanism (HTTP, gRPC, message queue).

**Why rejected**:
- The Rails console interaction is the only way to reach OpenProject internals not exposed via the REST API (custom field creation across projects, journal writes, certain workflow operations).
- Building a parallel RPC layer would require modifying OpenProject itself, a fork, or a plugin.
- `RailsConsoleClient` (1,023 LOC) plus `escape_ruby_single_quoted` + tests cover injection cases. It works.

**What we do instead**: in Phase 2 and Phase 8, extract the Ruby script construction into a `OpRubyScriptBuilder` (Builder pattern) that templates Ruby with whitelisted parameters. The transport (tmux-via-paramiko) stays as is.

### 8. Reject GraphQL or new public API surface

**What was considered**: Expose j2o functionality via GraphQL or REST for programmatic access.

**Why rejected**:
- j2o is a CLI tool that runs to completion. There is no "live" service to query.
- Programmatic access is already available via the Python module (`from src.application.orchestrator import Orchestrator`).
- Adds attack surface and operational complexity for an unstated need.

**What we do instead**: keep the CLI + dashboard surface. The dashboard's WebSocket is the read-only "API" for live progress.

### 9. Reject changing the test framework

**What was considered**: Move from pytest to nose, ward, or a property-based-only setup with hypothesis.

**Why rejected**:
- 929 passing pytest tests are the safety net for the entire migration plan. Changing the runner during the rewrite would mean changing the safety net during the operation that depends on it.
- pytest fixtures are working well; pytest-xdist already enables parallel execution.

**What we do instead**: keep pytest. Add `hypothesis` only if a specific component benefits from property-based testing (Phase 3+ if the typed domain models reveal good properties to test).

## Consequences

### Positive

- **Future architecture discussions can point at this ADR** instead of re-litigating.
- **Scope discipline**. Each rejection rules out a specific direction and frees engineering time for the chosen one.
- **Team alignment**. New contributors can read the rejections and understand the trade-offs without reverse-engineering them from the codebase.

### Negative

- **Lock-in to the chosen direction**. If a rejection turns out wrong (e.g., j2o's domain becomes richer over time), this ADR will need to be superseded.
- **Risk of dogma**. "ADR-003 says no DI framework" should not become an argument-stopper if a future situation genuinely warrants one. Supersede the ADR; don't appeal to it as authority.

### Neutral

- **No code changes from this ADR alone**. It documents a decision; the chosen direction is implemented per ADR-002.

## Revisiting this ADR

Re-examine this ADR if any of the following becomes true:

- j2o gains rich domain behaviour (e.g., real-time bidirectional sync between Jira and OP, with conflict resolution invariants).
- The number of wiring points crosses ~100 — the threshold where a DI framework starts paying off.
- The team grows past ~5 active contributors, where convention-over-configuration via a framework reduces coordination cost.
- A regulatory or audit requirement mandates event sourcing for traceability.

Until then, this ADR stands.
