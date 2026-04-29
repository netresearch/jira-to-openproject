# j2o Architectural Review — Target Architecture & Migration Plan

**Date**: 2026-04-29
**Author**: Architecture review pass (Sebastian + Claude)
**Status**: Proposal — for discussion and incremental adoption
**Codebase baseline**: `main` @ commit `5324b4a`, 53,968 LOC under `src/`, 929 passing tests

---

## 0. TL;DR

The codebase is functionally solid (tests green, mypy clean on `src/`, ruff ALL clean) and the high-level shape — `clients/ → migrations/ → orchestrator → dashboard` — is the right shape for an ETL tool. The pain is concentrated in **four problems** that compound:

1. **God-modules**. Three single files exceed 2,800 lines (`openproject_client.py` 7,266 / `work_package_migration.py` 4,688 / `enhanced_user_association_migrator.py` 2,960 / `jira_client.py` 2,852). They mix transport, business mapping, Ruby-string templating, caching, and persistence.
2. **Untyped dict-as-DTO pipeline**. 784 `dict[str, Any]` in `src/`, only 6 pydantic models, and entities flow through `_extract → _map → _load` as bare dicts. The type-safety net at the seams (mypy boundary overrides on `src.clients.*` / `src.migrations.*` disabling 21 error codes) is a deliberate escape hatch — it's load-bearing today but it papers over a structural problem.
3. **`BaseMigration` is a god-class** (978 LOC). It owns lifecycle, ETL skeleton, change detection, snapshotting, thread-safe LRU caching, JSON IO, two CF helpers, batch-issue merging, and Jira-key parsing. Every subclass inherits the union.
4. **Import-time side effects in `src/config/__init__.py`**. `from src import config` mkdir's directories, attaches log handlers, prunes old logs, and instantiates a `ConfigLoader`. This is why 41 files need `TYPE_CHECKING` blocks (49 in `migration.py` alone) — circular dependency pressure radiates from the config module.

The recommended target keeps the existing ETL skeleton conceptually (`extract → map → load` is correct for this domain) but:
- Splits the four god-modules along **transport / business / templating** seams.
- Replaces the dict-pipeline with **typed domain models** (Pydantic v2) at the seams that matter.
- Extracts the cross-cutting concerns out of `BaseMigration` into single-responsibility services injected via constructor.
- Introduces a **`MappingRepository`** abstraction over the current Mappings class.
- Makes `config` an inert data module + a separate `bootstrap()` step.

**Big-bang DDD/hexagonal is explicitly rejected** — see §9. The plan is **8 incremental phases**, each PR-sized, each landing on a green build, each delivering value even if subsequent phases stop.

---

## 1. Current State (As-Is)

### 1.1 Module shape

```
src/
├── main.py                       340     CLI entry, singleton lock, db validation
├── migration.py                1,482     Orchestrator + arg parsing + helpers (49× TYPE_CHECKING)
├── config_loader.py              ~       YAML/.env loader (instantiated at module-load)
├── config/__init__.py            382     SIDE-EFFECTING: mkdir, log handlers, singletons
├── type_definitions.py           197     Good TypedDicts for config, dataclasses for 4 entities
├── display.py                    ~       Rich console + log setup
├── clients/                   ~12,000    HTTP, SSH, Docker, Rails, OP, Jira clients
│   ├── openproject_client.py  7,266     ◀ god-module
│   ├── jira_client.py         2,852     ◀ god-module
│   ├── enhanced_*.py            583     Subclasses adding "performance" features
│   ├── rails_console_client    1,023
│   ├── ssh_client.py            718
│   ├── docker_client.py         601
│   └── health_check_client.py   701
├── migrations/                ~24,000    41 ETL components
│   ├── base_migration.py        978     ◀ god-class
│   ├── work_package_migration   4,688    ◀ god-module
│   ├── user_migration           1,542
│   ├── company_migration        1,651
│   ├── project_migration        1,729
│   ├── ... (37 others, 88–1,160 LOC each)
├── utils/                     ~14,000    Cross-cutting (retry, rate-limit, cache, audit, etc.)
│   ├── enhanced_user_association_migrator  2,960   ◀ god-module (in utils/!)
│   ├── enhanced_audit_trail_migrator       1,181
│   ├── enhanced_timestamp_migrator           910
│   ├── markdown_converter                    775
│   ├── checkpoint_manager                    713
│   ├── progress_tracker                      624
│   ├── (~18 more)
├── mappings/                     ~       Mappings class — singleton-via-proxy
├── models/                       ~       3 pydantic models (ComponentResult, MigrationResult, MigrationError)
└── dashboard/                    ~       FastAPI + WebSocket, decoupled (good)
```

### 1.2 Layering as it actually is

```
┌────────────────────────────────────────────────────────────────┐
│  src.main  +  src.dashboard.app           (presentation)        │
└─────────────────────────┬──────────────────────────────────────┘
                          │
┌─────────────────────────▼──────────────────────────────────────┐
│  src.migration  (orchestration: phase order, factories,         │
│                  arg parsing, error handling, retries)          │
└─────────────────────────┬──────────────────────────────────────┘
                          │   subclasses Template Method
┌─────────────────────────▼──────────────────────────────────────┐
│  src.migrations.*       (41 ETL components)                     │
│  src.migrations.base_migration  (lifecycle + caching +          │
│                                  JSON IO + CF helpers + ...)    │
└──────────┬──────────────┬───────────────────┬──────────────────┘
           │              │                   │
┌──────────▼─────┐ ┌──────▼──────────┐ ┌─────▼──────────────────┐
│ src.utils.*    │ │ src.mappings    │ │ src.clients.*          │
│ (cross-cutting │ │ (singleton +    │ │ (Jira REST, OP API,    │
│  + a few       │ │  proxy)         │ │  Rails, SSH, Docker,   │
│  business      │ │                 │ │  health checks)        │
│  utilities)    │ └─────────────────┘ └────────┬───────────────┘
└────────────────┘                              │
                                       External: Jira / OP / SSH / Docker
```

The arrows go the right direction at the macro level. The leaks are in **what each layer contains**, not in the dependency direction:

- `clients/openproject_client.py` contains business mapping (Jira→OP entity transformations) and Ruby script templating, not just transport.
- `utils/enhanced_user_association_migrator.py` (2,960 LOC) is a *migration* that lives in `utils/`. So is `enhanced_audit_trail_migrator` and `enhanced_timestamp_migrator`.
- `migrations/base_migration.py` contains caching infrastructure (LRU + thread-safety + global stats) that no migration's business logic should care about.
- `config/__init__.py` is half data-module / half bootstrap.

### 1.3 Patterns already in place (the good)

- **Template Method**: `BaseMigration._run_etl_pipeline(name)` with `_extract / _map / _load`. Conceptually correct.
- **Registry**: `EntityTypeRegistry` + `@register_entity_types("labels")` decorator. Clean.
- **Decorator + Strategy combo**: `register_entity_types` decorates each migration class.
- **Proxy**: `_MappingsProxy` for testability — a real GoF Proxy, not just a name.
- **Lazy singleton with double-checked locking** in `get_mappings()`.
- **Lifespan context manager** in dashboard `lifespan(app)` (modern FastAPI).
- **Dependency injection (constructor-style)** for `jira_client` / `op_client` into migrations.
- **Factory dict** in `migration.py:_build_component_factories()` (lambdas keyed by component name).

### 1.4 Cross-cutting concerns inventory

| Concern              | Where                                                    | Status |
| -------------------- | -------------------------------------------------------- | ------ |
| Logging              | `display.py` + import-time setup in `config/__init__.py` | Works, but coupled to config |
| Retry / rate limit   | `utils/retry_manager.py`, `utils/rate_limiter.py`        | Tenacity + custom |
| Circuit breaker      | `pybreaker` via `utils/error_recovery.py`                | OK |
| Caching (entities)   | Embedded in `BaseMigration` (LRU + thread safety)        | **Misplaced** |
| Caching (mappings)   | `mappings/mappings.py`                                   | OK |
| Idempotency          | `utils/idempotency_manager.py` (Redis)                   | OK |
| Checkpoint / resume  | `utils/checkpoint_manager.py` (SQLite + SQLAlchemy)      | OK |
| Change detection     | `utils/change_detector.py` + hooks in `BaseMigration`    | Mixed concerns |
| Progress events      | `utils/progress_tracker.py` → Redis → dashboard          | Good (decoupled) |
| Metrics              | `utils/performance_optimizer.py`, psutil                 | OK |

### 1.5 The numbers that matter

| Metric                                        | Value     | Read |
| --------------------------------------------- | --------- | ---- |
| Total `src/` LOC                              | 53,968    | Big but ETL-tool sized |
| Files >1,000 LOC                              | 11        | Concentrated complexity |
| Files >2,000 LOC                              | 5         | God-module territory |
| `dict[str, Any]` occurrences in `src/`        | **784**   | The type-leak surface |
| Pydantic models in domain                     | 6         | (3 in `src/models/`, 3 in `health_check_client.py`) |
| `TYPE_CHECKING` blocks                        | 42 files  | Cyclic-import pressure |
| `TYPE_CHECKING` blocks in `migration.py`      | **49**    | Single file, ouch |
| Mypy boundary overrides (codes disabled)      | 21        | Load-bearing escape hatch on 6 module globs |
| Migrations that override `run()` (vs ETL)     | 13+       | Inconsistent |
| Direct `requests` outside `clients/`          | 5 files   | Layer leak |
| `getattr(fields, "labels", None)` style       | Pervasive | Duck typing on Jira library responses |
| Tests passing                                 | 929 / 929 | Great safety net |

---

## 2. Findings (architectural smells)

Ranked by ROI of fixing (impact × ease, descending). Each finding cites concrete code.

### F1. `BaseMigration` is a god-class — 🔴 high impact, medium effort

**File**: `src/migrations/base_migration.py` (978 LOC, 1 class).

It owns at least eight distinct responsibilities:

1. Lifecycle (`run`, `run_with_change_detection`)
2. Template Method skeleton (`_extract` / `_map` / `_load` / `_run_etl_pipeline`)
3. Change-detection hooks (`detect_changes`, `should_skip_migration`, `create_snapshot`)
4. Thread-safe LRU entity cache (lines 206–424, with class-level locks and global stats)
5. JSON file IO (`_load_from_json`, `_save_to_json`)
6. Domain helpers (`_issue_project_key`, `_resolve_wp_id`, `_merge_batch_issues`)
7. OpenProject CF helpers (`_ensure_wp_custom_field`, `_enable_cf_for_projects`) — embed Ruby script templates
8. Performance feature detection (`_setup_performance_features`)

A subclass that wants only #1 and #2 inherits all eight. Tests for the cache infrastructure are tangled with tests for migrations. This is the **single highest-leverage refactor** in the codebase.

### F2. `openproject_client.py` is 7,266 lines — 🔴 high impact, high effort

**File**: `src/clients/openproject_client.py`.

It is at minimum: an HTTP client (REST API), a Rails-script-templating engine (embeds Ruby code via f-strings and routes through `RailsConsoleClient`), an SSH/Docker file-transfer wrapper, a custom-field service, a work-package service, a query-execution facade. The 16 `TYPE_CHECKING` blocks in this file are the canary — splitting any one method from the file requires a forward reference because everything calls everything.

`enhanced_openproject_client.py` is a subclass that adds "performance" features by hijacking attributes — and contains `Noop*` stubs for SSHClient/DockerClient/RailsConsoleClient (a smell that signals "we want to disable these in tests but the type system doesn't help us" — DI of a real interface would solve this cleanly).

### F3. Untyped dict-as-DTO pipeline — 🟡 high impact, high effort (incremental)

**Evidence**: 784 `dict[str, Any]` in `src/`. Sample from `labels_migration.py:46–58`:

```python
wp_map = self.mappings.get_mapping("work_package") or {}            # dict[?, ?]
keys = [str(k) for k in wp_map]
issues = self._merge_batch_issues(keys)                              # dict[str, Any]
labels_by_key: dict[str, list[str]] = {}
for k, issue in issues.items():
    fields = getattr(issue, "fields", None)                          # duck-typed
    labels = getattr(fields, "labels", None)                         # duck-typed
    if isinstance(labels, list) and labels:
        labels_by_key[k] = [str(x) for x in labels if isinstance(x, str) and x.strip()]
```

And `_load`:

```python
entry = wp_map.get(jira_key)
if not (isinstance(entry, dict) and entry.get("openproject_id")):    # dict OR int — see F4
    continue
wp_id = int(entry["openproject_id"])
```

`wp_map[key]` can be `dict | int` (per `BaseMigration._resolve_wp_id`) — the shape isn't a type, it's a contract enforced by `isinstance` at every call site. This is the pattern that drives the 21 disabled mypy error codes on `src.clients.*` and `src.migrations.*`.

### F4. Mapping shape is polymorphic and undocumented — 🟡 high impact, low effort

**Evidence**: `BaseMigration._resolve_wp_id` (lines 818–834) handles `entry: dict | int`. No type. `wp_map.get(key)` may return `None`, an int (legacy), or a dict with keys `{openproject_id, openproject_project_id, ...}`. This is two schemas in one mapping.

A typed `WorkPackageMappingEntry` Pydantic model would force a one-time normalization and eliminate dozens of `isinstance` checks scattered across migrations.

### F5. Import-time side effects in `src/config/__init__.py` — 🟡 medium impact, low effort

**Evidence**: lines 23–110:

- Line 23: `_config_loader = ConfigLoader()` — reads YAML + .env
- Lines 50–62: creates 10 directories on disk
- Lines 69–106: configures a logger, attaches a per-run file handler, **prunes old log files**

Just `from src import config` does all of this. Importing the module for type discovery (or in a doctest) has the side effect of writing log files. This is also why the test suite needs careful fixture management around config mocking, and it explains the 49 `TYPE_CHECKING` blocks in `migration.py` — every type import would otherwise drag the whole bootstrap with it.

### F6. `utils/` contains business migrations — 🟡 medium impact, low effort

**Evidence**: `utils/enhanced_user_association_migrator.py` is **2,960 LOC**, name ends in `_migrator`, contains business mapping logic. Same for `enhanced_audit_trail_migrator.py` (1,181) and `enhanced_timestamp_migrator.py` (910). These belong in `migrations/` or in a new `application/` layer; `utils/` should be reserved for cross-cutting utilities.

### F7. Cyclic import pressure radiates from many sources — 🟡 medium impact, low effort

**Evidence**: 42 files use `TYPE_CHECKING`, peaking at 49 blocks in `migration.py`. The pattern is structural: orchestrator imports all migration types for factory wiring; each migration imports clients; clients sometimes need to know about Mapping types; everything imports `config`. The fix is **interfaces (Protocol) at the boundaries**, so the orchestrator depends on `MigrationComponent` (abstract) and not on each concrete class.

### F8. `migration.py` orchestrator does too much — 🟡 medium impact, low effort

**File**: `src/migration.py` (1,482 LOC).

It does: argparse, component factory wiring (40+ lambdas), phase ordering, dry-run handling, error envelope, profile dispatch ("full" / "metadata_refresh"), pre-flight, post-flight, summary emission, and handles the 39-step DEFAULT_COMPONENT_SEQUENCE. Three of these are orthogonal: **CLI parsing**, **wiring (factories)**, **execution**.

### F9. Mappings type-confusion and side-effects — 🟢 low impact, low effort

**Evidence**: `Mappings.get_mapping("work_package")` returns `dict[str, dict | int] | None`. Per F4, this is the root cause of `_resolve_wp_id`'s dispatch. The Mapping class also caches and persists to disk — those concerns should split (see Repository pattern, §5).

### F10. "Enhanced*" subclassing pattern — 🟢 low impact, medium effort

**Evidence**: `EnhancedJiraClient(JiraClient)`, `EnhancedOpenProjectClient(OpenProjectClient)`. Subclasses add features (`performance_optimizer`) detected via `hasattr(self.jira_client, "performance_optimizer")` in `BaseMigration._setup_performance_features`. This is a Decorator pattern in disguise — and `hasattr`-feature-flags is a smell. Either compose a `PerformanceOptimizer` into the client unconditionally, or provide a no-op implementation.

### F11. Ruby-as-strings is a separate sub-language — 🟢 low impact, deferred

**Evidence**: `_ensure_wp_custom_field` (`base_migration.py:836–864`) emits Ruby code as Python f-strings. `escape_ruby_single_quoted` is the only escape hatch. There's no syntax checking, no parameterization beyond string interpolation. This works, and tests cover injection cases — but it's a sub-language hidden in Python source. **Defer**: extracting to `src/ruby/` (already exists) with template files (Jinja2 with whitelisted vars) is a future improvement, not a structural blocker.

### F12. 13+ migrations override `run()` directly, bypassing `_run_etl_pipeline` — 🟢 low impact, low effort

**Evidence**: From the agent map, classes like `RelationMigration`, `WatcherMigration`, `AttachmentsMigration`, etc. override `run()`. Some are transformation-only (no `_extract`), some have idiosyncratic flows. The Template Method's optional steps aren't expressive enough.

---

## 3. Target Architecture

### 3.1 Layering — strict but pragmatic

```
┌───────────────────────────────────────────────────────────────────────┐
│ src.cli           Thin: argparse → bootstrap → run                     │
│ src.dashboard     Read-only consumer of progress events (unchanged)    │
└────────────┬──────────────────────────────────────────────────────────┘
             │
┌────────────▼──────────────────────────────────────────────────────────┐
│ src.application                                                        │
│   .orchestrator        Phase ordering, profile dispatch                │
│   .pipeline            Generic ETL skeleton: ETLPipeline[S, T, R]      │
│   .components          One module per migration component (was         │
│                        src.migrations.*) — pure use-cases              │
│   .progress            ProgressEmitter Protocol                        │
└────────────┬──────────────────────────────────────────────────────────┘
             │  depends on Protocols only
┌────────────▼──────────────────────────────────────────────────────────┐
│ src.domain          Pure data + invariants, no IO, no framework        │
│   .jira             JiraIssue, JiraProject, JiraUser, JiraIssueLink,…  │
│   .openproject      OpWorkPackage, OpProject, OpUser, OpCustomField,…  │
│   .mapping          MappingEntry, WorkPackageMappingEntry,…            │
│   .results          ComponentResult, MigrationResult, MigrationError   │
│   .ids              JiraIssueKey, JiraProjectKey, OpenProjectId (…)    │
└────────────┬──────────────────────────────────────────────────────────┘
             │  used by infrastructure adapters
┌────────────▼──────────────────────────────────────────────────────────┐
│ src.infrastructure                                                    │
│   .jira              JiraGateway (HTTP), serializers ↔ domain.jira    │
│   .openproject                                                         │
│      .api            OpenProjectAPIGateway (HTTP REST)                 │
│      .rails          RailsScriptRunner + Ruby template registry        │
│      .ssh            SSHTransport (paramiko)                           │
│      .docker         DockerTransport                                   │
│   .persistence                                                         │
│      .checkpoint     CheckpointStore (SQLite + SQLAlchemy)             │
│      .mapping_repo   MappingRepository (was Mappings class)            │
│      .snapshot       SnapshotStore (JSON files on disk)                │
│      .cache          EntityCache (was the LRU bits of BaseMigration)   │
│   .observability                                                       │
│      .logging        LoggerFactory, structured logging                 │
│      .progress       RedisProgressEmitter                              │
│      .metrics        Counter, Gauge wrappers                           │
└───────────────────────────────────────────────────────────────────────┘
             │
┌────────────▼──────────────────────────────────────────────────────────┐
│ src.config           Inert data: Settings (Pydantic). No IO.           │
│ src.bootstrap        Wiring: read env/yaml → Settings → Container      │
│ src.container        Composition root: wires Protocols → impls         │
└───────────────────────────────────────────────────────────────────────┘
```

**Hard rule**: arrows only go **down**. `domain` imports nothing from j2o except stdlib + Pydantic. `application` imports `domain` and Protocols, never concrete `infrastructure` classes. Concrete wiring lives in `container.py` only.

### 3.2 Why this shape (and not full hexagonal)

- **Domain has no aggregates / invariants beyond shape**. Jira issues don't have business rules we enforce — we just need their data to round-trip cleanly. So `domain` is closer to "rich types" than DDD aggregates. We use Pydantic for validation at the seam, not for entity behaviour.
- **No use-case interactor objects**. Application-layer components stay close to today's `BaseMigration` subclasses — we just clean up the inheritance tree.
- **No CQRS, no event sourcing, no repository-per-aggregate**. One `MappingRepository`, one `CheckpointStore`. These are integration concerns.
- **No DI framework**. `container.py` is a hand-rolled composition root — a 200-line module that builds and wires everything once, exposes a typed `AppContainer`. Easier to read than `dependency-injector` or `lagom`.

### 3.3 Component template (target)

```python
# src/application/components/labels.py
from __future__ import annotations
from src.application.pipeline import ETLPipeline, ExtractResult, MapResult
from src.domain.results import ComponentResult
from src.domain.mapping import WorkPackageMappingEntry
from src.application.protocols import (
    JiraGateway, OpenProjectGateway, MappingRepository, EntityCache, Logger,
)


class LabelsComponent(ETLPipeline[
    ExtractResult[dict[str, list[str]]],   # extracted shape
    MapResult[dict[str, str]],             # mapped shape
    ComponentResult,                       # load result
]):
    name = "labels"

    def __init__(
        self,
        jira: JiraGateway,
        op: OpenProjectGateway,
        mappings: MappingRepository,
        cache: EntityCache,                # injected, not inherited
        logger: Logger,
    ) -> None:
        self.jira, self.op, self.mappings, self.cache, self.logger = jira, op, mappings, cache, logger

    def _extract(self) -> ExtractResult[dict[str, list[str]]]:
        wp_entries: dict[str, WorkPackageMappingEntry] = self.mappings.work_packages()
        issues = self.jira.batch_get_issues(list(wp_entries))
        labels: dict[str, list[str]] = {
            issue.key: list(issue.fields.labels) for issue in issues if issue.fields.labels
        }
        return ExtractResult.ok(data=labels)

    def _map(self, extracted): ...
    def _load(self, mapped): ...
```

Compared to today:
- No `BaseMigration` god-class. Inherits a generic `ETLPipeline[S, T, R]`.
- Cache is **injected**, not inherited.
- `wp_entries` is `dict[str, WorkPackageMappingEntry]` — `entry.openproject_id` instead of `isinstance(entry, dict) and entry.get("openproject_id")`.
- `issue.fields.labels` is typed at the seam (`JiraIssue` Pydantic model), no `getattr(fields, "labels", None)`.
- Dependencies are Protocols, so tests inject fakes without monkeypatching globals.

---

## 4. Pattern Application Map

Cross-reference of refactoring.guru patterns against j2o needs. **Adopt** = recommended for j2o; **Reject** = explicitly out of scope; **Already** = already present in codebase.

### Creational

| Pattern        | Verdict       | Notes |
| -------------- | ------------- | ----- |
| **Factory Method** | Adopt | One factory module per gateway family (`jira_factory.py`, `op_factory.py`); replaces inline lambdas in `migration.py:_build_component_factories` |
| **Abstract Factory** | Reject | Single product family per side; over-modelling |
| **Builder** | Adopt (narrow) | For Ruby script construction (`OpRubyScriptBuilder`) — replaces f-string Ruby in `_ensure_wp_custom_field` |
| **Prototype** | Reject | No deep-copy entity needs |
| **Singleton** | Already (Mappings) | Keep, but encapsulate behind `MappingRepository` |

### Structural

| Pattern        | Verdict       | Notes |
| -------------- | ------------- | ----- |
| **Adapter** | Adopt | `JiraGateway` adapts the `jira` PyPI library to domain types; `OpenProjectAPIGateway` adapts REST shapes |
| **Bridge** | Reject | No varying-implementation × varying-abstraction matrix |
| **Composite** | Reject | Components are flat list, not tree |
| **Decorator** | Adopt | Replace `Enhanced*Client` subclasses with composed decorators (`CachedJiraGateway(JiraGateway)`, `RetryingOpGateway(OpGateway)`) |
| **Facade** | Adopt | `OpenProjectGateway` is a facade over (HTTP + Rails + SSH) — but a thin one, not the 7,266-line monolith |
| **Flyweight** | Reject | Memory not the bottleneck |
| **Proxy** | Already (`_MappingsProxy`) | Keep; clarify intent comment |

### Behavioral

| Pattern        | Verdict       | Notes |
| -------------- | ------------- | ----- |
| **Chain of Responsibility** | Adopt (narrow) | Error-recovery pipeline: classify → retry → circuit-break → escalate |
| **Command** | Reject | Component `run()` is enough; no undo/queue/log requirement that justifies command objects |
| **Iterator** | Already | Pagination iterators in clients |
| **Mediator** | Reject | Orchestrator is fine as a coordinator without a Mediator object |
| **Memento** | Already (snapshots) | `SnapshotStore` formalizes this |
| **Observer** | Adopt | `ProgressEmitter` Protocol; dashboard subscribes via Redis. Already exists informally — formalize |
| **State** | Reject | No entity state machines we model |
| **Strategy** | Adopt | Each ETL component is a Strategy. Already implicit; formalize via `ETLPipeline` generic base |
| **Template Method** | Already (`_run_etl_pipeline`) | Keep; tighten with generics |
| **Visitor** | Reject | Domain is closed (Jira → OP), no need for double dispatch |

**Beyond the GoF set, also adopt**:
- **Repository** — `MappingRepository` as primary abstraction over current `Mappings` class
- **Result type** — `Result[T, E]` discriminated union for component outcomes; eliminates `success: bool + errors: list[str]` ambiguity
- **NewType / branded types** — `JiraIssueKey`, `OpenProjectId` to prevent ID mixing

---

## 5. Type-safety strategy

### 5.1 Where to apply Pydantic models

Not everywhere — **only at seams** where a dict crosses module boundaries.

| Boundary                                            | Today                          | Target |
| --------------------------------------------------- | ------------------------------ | ------ |
| Jira REST → application                             | Bare `jira.Issue` objects, `getattr(fields, …)` | `JiraIssue` Pydantic model with explicit fields |
| OpenProject REST → application                      | `dict[str, Any]`               | `OpWorkPackage`, `OpProject`, `OpUser`, `OpCustomField` |
| Mapping store ↔ application                         | `dict[str, dict \| int]`       | `WorkPackageMappingEntry`, `ProjectMappingEntry`, … |
| ETL stage hand-off (extract → map → load)           | `ComponentResult.data: dict`   | Generic `ETLPipeline[S, T, R]` with typed S/T/R |
| Component result ↔ orchestrator                     | `ComponentResult` (already)    | Keep, but switch to discriminated union (Success / Skipped / Failed) |

### 5.2 Where NOT to apply Pydantic models

- Internal helper return types — keep tuples/dataclasses
- Cache internals — `dict[str, Any]` is fine inside `EntityCache` as long as it doesn't escape
- Ruby script templates — those are strings; type the *inputs* to the builder, not the script

### 5.3 Branded ID types

```python
# src/domain/ids.py
from typing import NewType
JiraIssueKey   = NewType("JiraIssueKey", str)        # "PROJ-123"
JiraProjectKey = NewType("JiraProjectKey", str)      # "PROJ"
OpenProjectId  = NewType("OpenProjectId", int)
OpUserId       = NewType("OpUserId", int)
```

Today: `wp_id = int(entry["openproject_id"])` — nothing prevents passing it where a `JiraIssueKey` is expected. Branded types catch this at type-check time without runtime cost.

### 5.4 Discriminated union for component results

```python
# src/domain/results.py
from typing import Literal
from pydantic import BaseModel

class Success(BaseModel):
    kind: Literal["success"] = "success"
    success_count: int; updated: int = 0; details: dict[str, Any] = {}

class Skipped(BaseModel):
    kind: Literal["skipped"] = "skipped"
    reason: str; change_report: ChangeReport | None = None

class Failed(BaseModel):
    kind: Literal["failed"] = "failed"
    failed_count: int; errors: list[str]

ComponentResult = Annotated[Success | Skipped | Failed, Field(discriminator="kind")]
```

Replaces today's `ComponentResult(success=True, message=..., details=..., success_count=..., failed=..., errors=...)` where every field is optional — eliminating the `_component_has_errors` / `_extract_counts` defensive helpers in `migration.py:154–280`.

### 5.5 Mypy strategy evolution

- **Phase 1–3**: keep current overrides (the 21 disabled codes on `src.clients.*` / `src.migrations.*`). Don't fight two battles at once.
- **Phase 4** (after domain models land): drop `arg-type`, `assignment`, `dict-item`, `typeddict-item`, `union-attr`, `index`, `return-value`, `attr-defined` for `src.application.*`. Keep them on `src.infrastructure.*` (transport boundary).
- **Phase 6**: target full strict on `src.domain.*` and `src.application.*`. Infrastructure stays pragmatic.
- **Never strict on `src.infrastructure.jira` / `src.infrastructure.openproject.api`** — those are the JSON border, mypy strict there is a fight against reality.

---

## 6. Repository Pattern Treatment

### 6.1 The problem

`src/mappings/mappings.py` is a class that:
- Holds in-memory dicts of mappings
- Persists them to JSON files on disk
- Is accessed via singleton-with-proxy (`config.mappings`)
- Returns polymorphic shapes (`dict[str, dict | int]`)

Application code calls `self.mappings.get_mapping("work_package").get(jira_key)` — leaks both the storage format and the lookup mechanics.

### 6.2 The split

```python
# src/application/protocols/mapping_repository.py  (the Port)
from typing import Protocol
from src.domain.mapping import WorkPackageMappingEntry, ProjectMappingEntry, ...
from src.domain.ids import JiraIssueKey, JiraProjectKey, OpenProjectId

class MappingRepository(Protocol):
    def get_work_package(self, key: JiraIssueKey) -> WorkPackageMappingEntry | None: ...
    def set_work_package(self, key: JiraIssueKey, entry: WorkPackageMappingEntry) -> None: ...
    def all_work_packages(self) -> dict[JiraIssueKey, WorkPackageMappingEntry]: ...

    def get_project(self, key: JiraProjectKey) -> ProjectMappingEntry | None: ...
    # ... per entity type

    def commit(self) -> None: ...   # explicit persistence; today it's implicit
```

```python
# src/infrastructure/persistence/mapping_repo.py  (the Adapter)
class JsonFileMappingRepository:
    def __init__(self, data_dir: Path) -> None: ...
    # implements MappingRepository, persists JSON on commit()
```

```python
# src/infrastructure/persistence/mapping_repo_sqlite.py  (alternative Adapter — future)
class SQLiteMappingRepository:
    """Same Protocol, swaps storage. Not built now, but the door is open."""
```

### 6.3 What this buys

- **Type-safe lookup**: `repo.get_work_package(key)` returns `WorkPackageMappingEntry | None`, not `dict | int | None`.
- **Test isolation**: tests pass a `FakeMappingRepository` instance instead of monkeypatching `cfg.mappings`. Today's monkeypatch pattern (per memory) works, but it's a workaround for global state.
- **Storage-format independence**: today's JSON files keep working; tomorrow's SQLite or Redis is a 1-file change.
- **Eliminates the `_resolve_wp_id` helper** because the repository returns `WorkPackageMappingEntry` and `entry.openproject_id` is `OpenProjectId | None` directly.

### 6.4 Backward compatibility

The current monkeypatch pattern (`monkeypatch.setattr(cfg, "mappings", DummyMappings())`) and the `config.mappings` proxy must keep working through the migration. Plan: keep `config.mappings` as a thin proxy that delegates to `container.mapping_repository` for the duration of phases 1–4; remove in phase 7.

---

## 7. Migration Plan

Each phase is a single PR (or 2-3 PRs for the larger ones), lands on green tests, and **is independently valuable**. If we stop after phase 3, the codebase is materially better. If we stop after phase 5, we have most of the benefit. Phases 6–8 are polish.

### Phase 0 — Baseline (1 PR, ~1 day)

**Goal**: establish the architectural decision record and freeze the baseline so subsequent diffs are reviewable against an explicit target.

- Land this document in `claudedocs/ARCHITECTURE_REVIEW.md` (done by writing it).
- Create `docs/adr/0001-target-architecture.md` summarizing §3, §4, §5, §6 in one page.
- Create `docs/adr/0002-rejected-patterns.md` documenting **what we explicitly chose NOT to do** (no DDD aggregates, no CQRS, no DI framework, no big-bang rewrite).
- No code changes.

**Exit criteria**: ADRs merged. Team agreement on direction.

### Phase 1 — Decompose `BaseMigration` (3-4 PRs, ~1 week)

**Goal**: kill the god-class. Extract single-responsibility services. No behaviour change.

- **PR 1.1**: Extract `EntityCache` to `src/utils/entity_cache.py`. Inject into `BaseMigration.__init__` as `cache: EntityCache | None = None` with a default factory. ~400 LOC out of `base_migration.py`.
- **PR 1.2**: Extract JSON IO (`_load_from_json` / `_save_to_json`) to `src/utils/json_store.py`. Each migration that uses it gets a `json_store: JsonStore` parameter. ~50 LOC out.
- **PR 1.3**: Move CF helpers (`_ensure_wp_custom_field`, `_enable_cf_for_projects`) onto `OpenProjectClient` (where they belong by Single Responsibility). ~80 LOC out.
- **PR 1.4**: Extract change-detection wiring (`should_skip_migration`, `run_with_change_detection`) into a `ChangeDetectionMixin` or a separate `ChangeAwareRunner` strategy. ~150 LOC out.

**Exit criteria**: `base_migration.py` ≤ 350 LOC. All 41 migrations still work. Tests green. No public API change.

### Phase 2 — Decompose god-clients (4-5 PRs, ~2 weeks)

**Goal**: split `openproject_client.py` (7,266) and `jira_client.py` (2,852) along functional seams.

- **PR 2.1**: Identify natural seams in `openproject_client.py`. Likely: `OpenProjectAPIGateway` (HTTP/REST), `OpenProjectRailsRunner` (Rails console + Ruby builder), `OpenProjectFileTransfer` (SSH/Docker), `OpenProjectCustomFieldService`, `OpenProjectWorkPackageService`. Keep `OpenProjectClient` as a Facade composing the above for backward compat.
- **PR 2.2**: Extract `OpenProjectAPIGateway` (the pure REST methods). ~1,500 LOC.
- **PR 2.3**: Extract `OpenProjectRailsRunner` + `OpRubyScriptBuilder`. ~1,500 LOC.
- **PR 2.4**: Extract `OpenProjectFileTransfer` (file upload/download wrappers). ~800 LOC.
- **PR 2.5**: Same treatment for `jira_client.py` — `JiraAPIGateway` + `JiraSearchService`. ~1,500 LOC moved.

**Exit criteria**: each new module ≤ 1,500 LOC. The Facade `OpenProjectClient` is a 200-line composition. Tests still call `op_client.create_work_package(...)` unchanged.

### Phase 3 — Domain models for the most-used entities (3-4 PRs, ~1.5 weeks)

**Goal**: introduce typed domain models for the 5–8 highest-traffic entities. Replace `dict[str, Any]` at the seams that hurt most.

- **PR 3.1**: Create `src/domain/jira/` with Pydantic v2 models for `JiraIssue`, `JiraProject`, `JiraUser`, `JiraIssueLink`, `JiraComment`. Add `JiraGateway` Protocol returning these.
- **PR 3.2**: Create `src/domain/openproject/` with `OpWorkPackage`, `OpProject`, `OpUser`, `OpCustomField`, `OpStatus`.
- **PR 3.3**: Create `src/domain/mapping/` with `WorkPackageMappingEntry`, `ProjectMappingEntry`, `UserMappingEntry`. **One-time data migration** for any persisted mapping JSON that has the legacy `int`-only shape.
- **PR 3.4**: Update **3 representative migrations** end-to-end (`labels`, `priorities`, `relations`) to use the new domain models. Prove the pattern works. Don't migrate all 41 yet — that's gradual after this phase.

**Exit criteria**: `_resolve_wp_id` deleted. The 3 migrated components have zero `dict[str, Any]` in their `_extract → _map → _load` flow. Tests green. Mypy strict on `src.domain.*`.

### Phase 4 — Repository pattern for Mappings (2 PRs, ~1 week)

**Goal**: introduce `MappingRepository` Protocol + `JsonFileMappingRepository` adapter. Keep `config.mappings` proxy working.

- **PR 4.1**: Create `src/application/protocols/mapping_repository.py` (the Protocol) and `src/infrastructure/persistence/mapping_repo.py` (the adapter wrapping current `Mappings` class). Wire it into `container.py`.
- **PR 4.2**: Update the 3 migrations from PR 3.4 to receive `MappingRepository` via constructor instead of using `self.mappings`. Add a deprecation comment to the proxy.

**Exit criteria**: 3 migrations no longer touch `config.mappings`. The Protocol has a `FakeMappingRepository` for tests, used in at least 5 unit tests.

### Phase 5 — Layer reorganization (3-4 PRs, ~1.5 weeks)

**Goal**: physically move files into the target layout. Mostly mechanical moves + import updates.

- **PR 5.1**: Move `src/migrations/` → `src/application/components/`. Update 41 imports in `src/migration.py` plus tests. Use the move-and-redirect pattern (re-export from old path during transition).
- **PR 5.2**: Move `src/clients/` → `src/infrastructure/`. Split per gateway (jira/, openproject/, ssh/, docker/).
- **PR 5.3**: Move misnamed migrations out of `utils/`: `enhanced_user_association_migrator.py`, `enhanced_audit_trail_migrator.py`, `enhanced_timestamp_migrator.py` → `src/application/components/` (or wherever they fit).
- **PR 5.4**: Split `src/migration.py` — orchestrator (`src/application/orchestrator.py`), CLI (`src/cli/main.py`), factory wiring (`src/container.py`).

**Exit criteria**: `src/utils/` contains only true cross-cutting utilities (~6 files). `src/migrations/` is gone. `src/clients/` is gone. All 929 tests still pass.

### Phase 6 — Inert config + explicit bootstrap (2 PRs, ~3 days)

**Goal**: kill import-time side effects.

- **PR 6.1**: Replace `src/config/__init__.py` module-level code with a pure `Settings` Pydantic model. The `mkdir`, log handler, log pruning logic moves to `src/bootstrap.py:bootstrap_runtime(settings) -> AppContainer`. Existing entry points (`main.py`, `dashboard/app.py`) call `bootstrap()` once at startup.
- **PR 6.2**: Audit and remove `TYPE_CHECKING` blocks made unnecessary by the new layering. Expect to drop 30+ of the current 42.

**Exit criteria**: `from src.config import Settings` is side-effect-free. The 49 `TYPE_CHECKING` blocks in `migration.py` (now `orchestrator.py`) are at most 5.

### Phase 7 — Migrate remaining components to typed pipeline (5-8 PRs, ~3 weeks)

**Goal**: extend the Phase 3 pattern to all 41 components, in batches grouped by complexity.

- **PR 7.1**: Small components (≤ 200 LOC): `priorities`, `resolutions`, `labels` (already done), `votes`, `inline_refs`, `simpletasks`. Easy wins.
- **PR 7.2**: Medium components (200–500 LOC): `versions`, `components`, `time_entries`, `watchers`, `relations`.
- **PR 7.3**: Large components (500–1,500 LOC): `users`, `projects`, `companies`, `groups`.
- **PR 7.4**: The monsters: `work_package_migration` (4,688 LOC) and `work_package_skeleton` / `work_package_content`. These need their own decomposition (probably 3 sub-components each).
- **PR 7.5**: Drop the `config.mappings` proxy. Remove the `disable_error_code` mypy override on `src.application.*`.

**Exit criteria**: All 41 components use `MappingRepository` + typed domain models. Mypy strict on `src.domain.*` and `src.application.*`. `dict[str, Any]` count in `src/` ≤ 100.

### Phase 8 — Polish (2-3 PRs, ~1 week)

- Replace `Enhanced*Client` subclasses with composed Decorators.
- Convert Result type to discriminated union (`ComponentResult = Success | Skipped | Failed`).
- Drop `_component_has_errors` / `_extract_counts` defensive helpers.
- Audit and prune `utils/` (some of those 24 files are likely consolidatable).

**Exit criteria**: orchestrator → component contract is fully type-safe end-to-end.

---

### 7.x Cumulative outcome (after all phases)

| Metric                                          | Today    | Target   |
| ----------------------------------------------- | -------- | -------- |
| `dict[str, Any]` in `src/`                      | 784      | < 100    |
| Files > 2,000 LOC                               | 5        | 0        |
| Files > 1,000 LOC                               | 11       | ≤ 3      |
| `TYPE_CHECKING` blocks total                    | 42 files | ≤ 10     |
| `TYPE_CHECKING` blocks in orchestrator          | 49       | ≤ 5      |
| Mypy `disable_error_code` codes                 | 21       | ≤ 8 (infra only) |
| Pydantic domain models                          | 6        | ~30      |
| Test count                                      | 929      | ≥ 929    |
| `src/utils/` files                              | 24       | ≤ 8      |

Risk: any of the LOC targets are negotiable. The structural targets (no god-modules, typed seams, repository pattern, side-effect-free config) are the ones that matter.

---

## 8. Risks and Out-of-Scope

### 8.1 Risks

| Risk                                                       | Likelihood | Mitigation |
| ---------------------------------------------------------- | ---------- | ---------- |
| Phase 7 stalls on `work_package_migration.py` (4,688 LOC)  | High       | Treat as 3 sub-components from PR 7.4 onwards. Allocate explicit budget. |
| Mapping data format change in Phase 3 corrupts existing runs | Medium     | One-time normalization migration script + JSON schema validation. Tests against a recorded fixture. |
| `config.mappings` proxy removal breaks user scripts        | Low        | Keep proxy with `DeprecationWarning` for two minor releases. |
| Mypy strictness changes cause `# type: ignore` proliferation | Medium     | Land strict mode per-package, not globally. Block PRs that add `# type: ignore` without an issue link. |
| Performance regression from extra Pydantic validation      | Low        | Pydantic v2 with `model_config = ConfigDict(frozen=True, validate_assignment=False)` is fast enough. Benchmark in CI on the largest entity types. |
| Phase 5 file moves break IDEs / blame history              | Low        | Use `git mv` cleanly. CI green is the source of truth, not blame. |
| Disagreement on layer boundaries mid-flight                | Medium     | ADRs (Phase 0) are the contract. PRs that violate them require an ADR amendment first. |

### 8.2 Explicitly out of scope

- **DDD aggregates / value objects with invariants**. The domain has shape, not behaviour.
- **CQRS / event sourcing**. ETL doesn't need command/query separation.
- **DI framework** (`dependency-injector`, `lagom`, `wireup`). A composition-root module is enough.
- **GraphQL or new API surface**.
- **Replacing the Ruby script approach**. The Rails-console-via-tmux pipeline works. F11 is deferred.
- **Async-first rewrite**. Today's mix of async dashboard + sync migrations is fine.
- **Replacing pytest with another test runner**. Existing 929 tests are the safety net.
- **Python 3.15 features** (when 3.15 lands). Stay on the 3.14 baseline through this work.
- **Touching `dashboard/`**. It's already decoupled. Leave it alone.

---

## 9. Why we're not doing the "ideal" thing

> **What if we did full hexagonal + DDD aggregates from day 1?**

We'd be wrong. j2o is an ETL/integration tool. The domain is "Jira data shape" and "OpenProject data shape" — there's no rich behaviour to encapsulate. DDD is the right tool when you have invariants like "an Order with no LineItems is invalid" or "a Customer's credit limit must be respected at checkout." j2o's invariants are mostly "this Jira issue key maps to that OP work package id" — that's persistence, not domain. Forcing aggregates would create ceremony without insight.

> **What if we used a DI framework?**

The codebase has 41 components and ~10 infrastructure adapters. A 200-line composition root in `container.py` is more readable than a `dependency-injector` config DSL. Frameworks pay off at ~100+ wiring points or when runtime swap is needed. Neither applies here.

> **What if we did the rewrite in one mega-PR?**

Three reasons not to:
1. The 929-test safety net works *because* changes are small enough to debug. A 53k-LOC rewrite hides a lot of ways to break things.
2. Each phase delivers value alone. After Phase 3 we've got typed domain models for 3 components — that's already a measurable improvement, even if Phases 4–8 never happen.
3. Review fatigue. A 50-PR shape-changing diff gets rubber-stamped or rejected. Eight focused PRs per phase get real review.

---

## 10. Success Metrics (per phase, not just final)

| Phase | Headline metric                                  | Threshold |
| ----- | ------------------------------------------------ | --------- |
| 1     | `base_migration.py` LOC                          | ≤ 350     |
| 2     | Largest single client file LOC                   | ≤ 1,500   |
| 3     | Component using domain models end-to-end         | ≥ 3       |
| 4     | Components depending on `MappingRepository`      | ≥ 3       |
| 5     | Files left in `src/utils/`                       | ≤ 12      |
| 6     | `TYPE_CHECKING` blocks in orchestrator           | ≤ 5       |
| 7     | `dict[str, Any]` in `src/application/`           | ≤ 50      |
| 8     | Mypy `disable_error_code` codes                  | ≤ 8       |

Tests passing ≥ 929 is a hard gate at every phase.

---

## 11. Appendix — Pattern → File Map

For navigation when implementing. Maps refactoring.guru patterns to the concrete j2o files where they will live.

| Pattern        | Today                                       | After             |
| -------------- | ------------------------------------------- | ----------------- |
| Template Method | `BaseMigration._run_etl_pipeline`           | `application/pipeline.py:ETLPipeline[S,T,R]` |
| Strategy        | `BaseMigration` subclasses (implicit)       | `application/components/*.py` |
| Adapter         | `clients/jira_client.py` (mixed)            | `infrastructure/jira/gateway.py` |
| Facade          | `clients/openproject_client.py` (god-class) | `infrastructure/openproject/__init__.py` (thin) |
| Decorator       | `enhanced_*_client.py` (subclass)           | `infrastructure/*/decorators.py` (composed) |
| Repository      | `mappings/mappings.py` (singleton)          | `infrastructure/persistence/mapping_repo.py` |
| Builder         | f-string Ruby                               | `infrastructure/openproject/rails/builder.py` |
| Proxy           | `config._MappingsProxy`                     | (keep until Phase 7 removal) |
| Registry        | `EntityTypeRegistry`                        | (keep) |
| Singleton       | `_config_loader`, `_mappings`               | `container.py` (single composition root) |
| Observer        | `progress_tracker.py` → Redis → dashboard   | formalize as `ProgressEmitter` Protocol |
| Memento         | `change_detector.py` snapshots              | `infrastructure/persistence/snapshot.py` |
| Chain of Responsibility | `error_recovery.py` (partial)        | `application/error_pipeline.py` |
| Result type     | `ComponentResult` (mixed shape)             | `domain/results.py` (discriminated union) |
| New / branded types | (none)                                  | `domain/ids.py` |

---

## 12. Decision log (for the team to fill in)

| Decision                                                          | Status   | Owner | Date |
| ----------------------------------------------------------------- | -------- | ----- | ---- |
| Adopt target architecture (§3)                                    | proposed |       |      |
| Adopt phased migration plan (§7)                                  | proposed |       |      |
| Reject DDD aggregates / CQRS / DI framework (§9)                  | proposed |       |      |
| Adopt Pydantic v2 for domain models                               | proposed |       |      |
| Adopt branded ID types (`JiraIssueKey`, `OpenProjectId`)          | proposed |       |      |
| Adopt discriminated-union ComponentResult                         | proposed |       |      |
| Drop `Enhanced*Client` subclassing → composed Decorators          | proposed |       |      |
| Side-effect-free `src/config` (Phase 6)                           | proposed |       |      |
| Keep `config.mappings` proxy through Phase 6, remove in Phase 7   | proposed |       |      |
