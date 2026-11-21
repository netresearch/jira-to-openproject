# Jira to OpenProject Migration Tool

[![CI](https://github.com/netresearch/jira-to-openproject/actions/workflows/ci.yml/badge.svg)](https://github.com/netresearch/jira-to-openproject/actions/workflows/ci.yml)
[![Security](https://github.com/netresearch/jira-to-openproject/actions/workflows/security.yml/badge.svg)](https://github.com/netresearch/jira-to-openproject/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)

A robust, modular migration toolset for transferring project management data from Jira Server 9.x to OpenProject 17.3+. Built with Python 3.14, this tool handles users, projects, work packages, custom fields, statuses, workflows, and attachments.

## Features

**Migration Components:**
- **Customer Migration:** Tempo Customers → Top-level OpenProject projects
- **Account Migration:** Tempo Accounts → Custom field values on projects
- **Project Migration:** Jira projects → Sub-projects under customer hierarchy with lead membership, adaptive module enablement, and Jira provenance (key/ID/category/type/URL/avatar/display name)
- **Work Package Migration:** Issues → Work packages with full metadata
- **User Migration:** Jira users → OpenProject users with role mapping, full J2O provenance (origin system/ID/key/URL), locale→language syncing, and avatar backfills. Legacy "Jira user key" / "Tempo Account" custom fields are retired automatically.
- **Custom Field Migration:** Field definitions and values
- **Status & Workflow Migration:** Status creation and workflow configuration guidance
- **Attachment Migration:** Issue attachments → Work package files
- **Time Log Migration:** Tempo worklogs → OpenProject time entries
- **Workflow Automation:** Jira workflow transitions → OpenProject workflow entries per type/role
- **Agile Boards & Sprints:** The regular import creates private queries with each board’s mapped work packages and maps sprints to versions. An [optional native board import](docs/ENTITY_MAPPING.md#native-basic-board-snapshots) preserves column layout and card order on OpenProject basic boards.
- **Admin Schemes:** Jira role memberships → OpenProject project memberships
- **Reporting Artefacts:** Jira saved filters & dashboards → OpenProject queries and wiki summaries

For confidential Tempo work attributes, set `J2O_TEMPO_ATTRIBUTE_IDENTITY` to a private age X25519 identity file owned by the recovery operator (mode 0600 on Unix). The normal time-entry migration encrypts those values before saving its extraction and verifies the encrypted OpenProject field after writing each matched Jira worklog. Keep the identity for recovery. Decode the field’s Base64 content and decrypt the resulting age file with that identity to recover its JSON.

To include unused definitions, set `J2O_TEMPO_CATALOGUE_PROJECT` to the identifier of a private OpenProject project. The time-entry component archives the entire Jira instance’s Tempo Core catalogue there, including the choices returned by each static list. The field `Jira instance Tempo work attributes (age)` contains a Base64-encoded age file with the source URL and a history of complete catalogue snapshots. Recover it with the same operator identity. Dynamic providers are recorded by their source definition and URL. Their externally supplied choices are outside the archive.

**Key Capabilities:**
- **Batch Processing:** Configurable batch sizes for large datasets
- **Progress Tracking:** Real-time progress monitoring with detailed logging
- **Error Recovery:** Comprehensive error handling with retry mechanisms
- **Security:** Input validation and injection attack prevention
- **Flexible Deployment:** Local execution or Docker containerization
- **Testing Infrastructure:** Comprehensive test suite with multiple test types

## Quick Start

> ⚠️ **Scope**: J2O is designed for a trusted administrator running migrations with Jira/OpenProject API tokens. Interactive user authentication and the former "advanced security" stack have been removed to keep the tool lightweight for single-operator runs.

### Supported Targets

- Jira Server/Data Center 9.x (validated on 9.11)
- OpenProject migration target: **17.9.7**. Database round trips on 17.9.7 and 17.3.0 cover work-package creation, descriptions, mapped fields, comment authors and timestamps, repeat imports, board queries, sprint versions, parent links and workflow transitions.

To repeat this check against a disposable target with OpenProject sample data, run
`J2O_TEST_OPENPROJECT_CONTAINER=<container> uv run pytest --live-docker tests/integration/test_openproject_agile_roundtrip.py`. The test runs its database writes in a rolled-back transaction.

Earlier or later releases may work, but they are not part of the supported/tested matrix.

### Installation

The project uses [uv](https://docs.astral.sh/uv/) for dependency management and Python 3.14+.

```bash
# Clone
git clone https://github.com/netresearch/jira-to-openproject.git
cd jira-to-openproject

# Install dependencies (creates .venv automatically)
uv sync --frozen --extra dev --extra test

# Configure environment
cp .env.example .env
# Edit .env with your Jira and OpenProject credentials

# Verify installation
uv run pytest tests/unit -q          # ~1600 unit tests
uv run ruff check .                  # lint
uv run mypy src/                     # type check
uv run lint-imports                  # architecture contract
```

### Development Environment

```bash
# Start development stack (app + default services)
make up

# Run tests (test profile)
make test

# Run the unit suite inside Docker (ensures native deps like libffi/aiohttp)
make container-test

# Run the integration suite against mock Jira/OpenProject services
make container-test-integration

# Code quality checks
make lint
make type-check

# Start and attach to a local tmux session linked to remote Rails console
make start-rails ATTACH=true
```

The compose `app` service bind-mounts the repository over `/app`, so source edits on the host take effect in the container without a rebuild. Its Python environment lives in the image at `/opt/venv`, outside the mount; a host `.venv` is neither needed nor used inside the container. After a dependency change in `pyproject.toml`/`uv.lock`, rebuild the image with `make install`.

### Migration Execution

```bash
# Run specific migration components
uv run python -m src.main migrate --components users,projects,workpackages --no-confirm

# Run the predefined full profile (covers workflows, agile metadata, admin schemes, reporting).
# With a restricted-issue inventory configured (docs/operator-restricted-issues.md), it also runs
# security_level_projects after projects, the level work packages in their sub-projects, and
# security_level_access after admin_schemes, which grants each level's readers access last.
uv run python -m src.main migrate --profile full --no-confirm

# Run with custom configuration
uv run python -m src.main migrate --config custom_config.yaml --components all --no-confirm

# Dry run mode
uv run python -m src.main migrate --dry-run --components users --no-confirm

# Reset work-package checkpoints before rerunning delta migrations
uv run python -m src.main migrate --components work_packages --reset-wp-checkpoints

# After each run a focused summary covering workflows, agile boards/sprints, admin schemes,
# and reporting artefacts is written to `var/results/migration_summary_<timestamp>.json`.
```

### Rehearsal Workflow

Use a fresh, private OpenProject target and freeze Jira writes and permission changes for the final snapshot. The default keeps comment bodies in Jira. The **J2O Origin URL** on each new work package leads readers back to Jira, where their own permissions apply. For a Jira shutdown, run the [protected comment archive](docs/protected-comments.md) on the frozen project. It stores independently encrypted reader views on OpenProject work packages. Without the archive the source must remain available for these comments. The content migration reports how many comments visible to the migration account were retained at source in `comments_held`. An incomplete source read is an error.

For an internet-public Jira, set `J2O_COPY_PUBLIC_COMMENTS=1` to copy comments that Jira also returns over HTTPS to a credential-free request identified as anonymous. Each copied ID and text must match both reads. Use the default if a VPN, network allowlist or authenticating proxy limits Jira access. Group, role, internal Service Management and unknown comment restrictions stay at source. Project membership alone does not establish equivalent access.

Historical field changes without an identifiable non-comment field stay in Jira as well. Before opening the target, run the content step against the frozen source. It stops if previously marked target comments differ from the currently approved texts. Regenerate the Jira inventories used for the run. Older `jira_issues_*_backup_*.json` files can contain private revisions. Restrict access to those backups under your existing retention policy. Earlier imports could write unmarked journals. Restore a clean pre-import snapshot before using this version on such a target. A completed copy is a snapshot of Jira permissions at cutover. Later permission changes in Jira do not revoke an OpenProject journal.

Compare counts for issues, copied and retained comments, attachments and logged hours. Repeat the import on the private target and check for duplicates and reported field mapping failures.

1. **Seed rehearsal data**: snapshot Jira/OpenProject using your existing backup process.
2. **Configure credentials**: point `.env` at rehearsal endpoints/API tokens.
3. **Run a rehearsal**: invoke the CLI with `--dry-run` or a limited component list to verify mappings, or use
   `python scripts/run_rehearsal.py --collect --stop` to orchestrate the container mocks automatically.
4. **Review artefacts**: inspect `var/data`, `var/logs`, and the generated mapping JSON for discrepancies.
5. **Reset state**: restore snapshots or wipe the rehearsal OpenProject instance before the next run.
6. **Production cutover**: swap `.env` to production credentials and rerun the migration without `--dry-run`.

### Migrating projects in stages

[Capture unresolved Jira issue links and epic references](docs/deferred-references.md) before retiring Jira. The operator can restore the encrypted snapshot and apply native links when both endpoints have been migrated.

### Metadata Validation Checklist

- Run `uv run --active --no-cache python scripts/data_qa.py --projects <KEY>` after rehearsals. Investigate any `⚠️` warnings about missing project modules or zero start-date coverage—rerun `migrate --components projects`/`work_packages` to refresh caches when needed.
- Jira start-date precedence is `customfield_18690` → `customfield_12590` → `customfield_11490` → `customfield_15082`; when they are empty we fall back to the first Jira status transition whose category is *In Progress*. Confirm Jira histories include that transition or relax expectations when the project never moved into that category.
- Confirm project modules (`work_package_tracking`, `wiki`, `time_tracking`, `costs`, `calendar`, `news`) stay enabled post-migration; the project component now enables time tracking/costs for Tempo-linked projects and calendar/news when categories or project types are present, then persists provenance (`Jira Project Lead`, `Jira Project Category`, `Jira Project Type`, `Jira Project URL`, `Jira Project Avatar URL`).
- If you need to rebuild the work-package fast-forward database (e.g., after a rehearsal restore or when corruption is detected) run `uv run python -m src.main migrate --reset-wp-checkpoints --components work_packages`.

[Operator-encrypted field history](docs/operator-field-history.md) describes how to retain Jira field changes for recovery after shutdown.

[Operator archive of restricted Jira issues](docs/operator-restricted-issues.md) describes how to inventory issues with a security level and keep those that cannot be migrated with their level's static readers.

### Watch a migration

From the configured checkout, start the dashboard in one terminal:

```sh
mkdir -p -m 700 var/dashboard
export J2O_DASHBOARD_PROGRESS_FILE="$PWD/var/dashboard/progress.json"
uv run uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8001
```

Open [the local dashboard](http://127.0.0.1:8001/). In the terminal where you run your normal `j2o migrate` command, export the same absolute `J2O_DASHBOARD_PROGRESS_FILE` first. Give each concurrent migration its own file. Only the producer writes it. The dashboard reads completed snapshots and keeps the most recent result available after the process exits. Container processes need a shared volume for this path.

The standard time-entry component compares worklogs carrying Tempo attributes for each selected project. The target count includes entries whose stored attributes the importer decrypted and verified. Missing or duplicate source worklog IDs leave the affected project comparison unmeasured. The dashboard publishes aggregate counts only.

During a time-entry import, the dashboard counts selected Jira issues and shows how many returned a worklog list with valid IDs. Empty lists count as checked. Failed requests and malformed lists leave the checked count below the selected count. The source and target comparison appears after the import verifies its results.

A reconciliation producer can publish per-data-type counts, for example `"comparison_counts": {"Attachments": {"source": 12, "target": 12}, "Comments": {"source": 30}}`. Count the same population and unit on each side. Omit a side until measured. The dashboard shows zero for a measured zero and “Not checked” for a missing observation. Existing `verified_counts` stay available for other measurements. For a multi-project run, publish the same per-data-type structure under `project_comparisons`, keyed by project. The project selector keeps each comparison within its source scope.


A pilot that separately verifies the target can publish its own aggregate counts and pending checks through `src.utils.dashboard_progress.write_progress`. Use a separate file for each independent producer. Keep issue text, filenames, tokens and person identifiers out of these snapshots. The UI labels system RAM and process RAM separately. Missing measurements appear as unavailable.

### Optional Tooling

- **Web dashboard** (`src/dashboard`): Local web interface for viewing a migration as it runs. It shows the active phase, reported counts, pending checks and resource usage. See [Watch a migration](#watch-a-migration).
- **Automated testing suite** (`src/utils/automated_testing_suite.py`): scaffolding for generating test stubs. Experimental—enable only if you plan to grow automated regression coverage.
- **Integration testing framework** (`src/utils/integration_testing_framework.py`): utilities for mock/real environment setup. Use when rehearsals need scripted data generation.
- **Comprehensive logging** (`src/utils/comprehensive_logging.py`): structured JSON logging, metrics collection, and health checks. Optional enhancement over the standard logging already in place.
- **Rails metadata writes**: `migration.enable_rails_meta_writes` (default `true`) must remain enabled to preserve author attribution, timestamps, and audit trails in OpenProject via the enhanced Rails helpers.
- **Data QA script** (`scripts/data_qa.py`): summarises cached migration artefacts (issue/work-package counts, attachments). Run it after rehearsals/cutover to spot mismatches quickly.

## Post-Migration Cleanup

After the final cutover:

1. Switch OpenProject to semantic work package identifiers when the migration ran with `migration.semantic_identifiers: true` ([ADR-011](docs/adr/ADR-011-jira-keys-as-semantic-identifiers.md)). j2o never changes this setting.
   - Before: the last run's `semantic_identifiers` result is successful with `refused` and `failed` at 0 (a refused work package gets a new number from the switch, not its key), and `not_returned` is explained. In the OpenProject container, this count of migrated work packages whose identifier is not their Jira key is 0: `bundle exec rails runner 'f = WorkPackageCustomField.find_by!(name: "J2O Origin Key"); puts CustomValue.where(customized_type: "WorkPackage", custom_field: f).where.not(value: [nil, ""]).joins("JOIN work_packages wp ON wp.id = custom_values.customized_id").where("wp.identifier IS DISTINCT FROM custom_values.value").count'`. No j2o run is in progress, none follows the switch, and a database backup exists.
   - Switch: in the administration, under work package identifiers (`/admin/settings/work_packages_identifier`), choose semantic. OpenProject converts every project in a background job and sets the setting afterwards; if the conversion fails, it reverts to classic and keeps the written data.
   - After: the setting shows semantic; each migrated project has its Jira key as identifier; a migrated work package opens by its Jira key and by a key it had before a move; a new work package in a migrated project gets a number above every migrated key; a level work package opens by its Jira key for a member of its sub-project (it displays the sub-project's own identifier), and a member of the parent project only gets "not found".
2. Rotate/revoke Jira and OpenProject API tokens that were used during the migration.
3. Archive `var/logs`, `var/data`, and `var/results` if you need an audit trail, then purge local copies.
4. Remove temporary rehearsal snapshots and any encrypted configuration backups you created.
5. Tear down the local `.venv` or containers, and archive this repository if no further migrations are planned.

## Architecture

### System Overview

The migration tool uses a **layered client architecture** for reliable remote operations:

```
Local Migration Tool
    ↓ (SSH Connection)
Remote OpenProject Server
    ↓ (Docker Commands)
OpenProject Container
    ↓ (Rails Console)
OpenProject Application
```

### Source Layout (Cosmic Python layers)

The Python source follows a [Cosmic Python](https://www.cosmicpython.com/) layered structure, enforced in CI by [import-linter](https://import-linter.readthedocs.io/):

| Layer | Path | Responsibility |
|---|---|---|
| Entrypoints | `src/main.py`, `src/dashboard/` | CLI and FastAPI dashboard — argument parsing, request handling |
| Services | `src/application/components/`, `src/application/transformers/` | Migration orchestration, ETL pipelines, pure data transformations |
| Adapters | `src/infrastructure/jira/`, `src/infrastructure/openproject/`, `src/infrastructure/persistence/` | I/O: Jira/OpenProject clients, SSH, Rails console, persistence |
| Models | `src/domain/`, `src/models/` | Domain abstractions (Protocols, IDs, results) and data classes |

The `architecture` job in CI (`uv run lint-imports`) enforces the dependency direction: higher layers may import from lower layers, never the reverse.

### Client Components

- **OpenProjectClient**: High-level orchestration and API
- **SSHClient**: Foundation layer for remote connections
- **DockerClient**: Container operations via SSH
- **RailsConsoleClient**: Rails console interaction via tmux

### Migration Flow

1. **Extract**: Data extracted from Jira via REST API
2. **Transform**: Data mapping and validation
3. **Load**: Data insertion via OpenProject Rails console
4. **Verify**: Validation and error reporting

## Configuration

### Environment Variables

Critical settings in `.env`:

```bash
# Jira Configuration
J2O_JIRA_URL=https://your-jira-server
J2O_JIRA_USERNAME=your-username
J2O_JIRA_API_TOKEN=your-api-token

# OpenProject Configuration
J2O_OPENPROJECT_URL=https://your-openproject
J2O_OPENPROJECT_API_KEY=your-api-key

# SSH Connection (for remote OpenProject)
SSH_HOST=openproject-server
SSH_USER=username
SSH_KEY_FILE=/path/to/ssh/key

# Docker Configuration
DOCKER_CONTAINER_NAME=openproject-web-1
TMUX_SESSION_NAME=rails_console
```

### YAML Configuration

Structured settings in `config/config.yaml`:

```yaml
migration:
  batch_size: 100
  parallel_workers: 4
  enable_dry_run: false
  skip_validation: false

logging:
  level: INFO
  format: detailed
  file: migration.log
```

## Documentation

### Core Documentation

- **[Entity Mapping Reference](docs/ENTITY_MAPPING.md)** - Comprehensive Jira→OpenProject field mappings
- [Native sprint states and goals](docs/native-completed-sprints.md) on OpenProject 17.9, with every historic membership retained
- **[Migration Components Catalog](docs/MIGRATION_COMPONENTS.md)** - Module listing with development state
- **[Developer Guide](docs/DEVELOPER_GUIDE.md)** - Development standards, testing, and compliance
- **[System Architecture](docs/ARCHITECTURE.md)** - Client architecture and component design
- **[Security Documentation](docs/SECURITY.md)** - Security measures and vulnerability prevention
- **[Configuration Guide](docs/configuration.md)** - Detailed configuration options
- **[Workflow & Status Guide](docs/WORKFLOW_STATUS_GUIDE.md)** - Status migration and workflow setup
- **[Architecture Decisions](docs/adr/)** - ADRs documenting key technical decisions
- **[Decision Records](docs/decisions/)** - Long-form rationale for non-trivial design choices

### Quick Reference

**Development:**
```bash
# Quick unit tests (~30s)
python scripts/test_helper.py quick

# Full test suite (~5-10min)
python scripts/test_helper.py full

# Type checking
mypy src/
```

**Migration:**
```bash
# Status and workflow migration
python src/main.py migrate --components status,workflow --force

# Full migration with progress tracking
python src/main.py migrate --components all --verbose
```

## Development

### Prerequisites

- **Python 3.14+**
- **uv** (`pip install uv` or [install instructions](https://docs.astral.sh/uv/getting-started/installation/))
- **Docker & Docker Compose** (for development environment)
- **SSH access** to OpenProject server (for production migration)
- **tmux** (for Rails console interaction)

### Development Standards

- **Exception-based error handling** (no return codes)
- **Optimistic execution** (validate in exception handlers)
- **Modern Python typing** (built-in types, pipe operators)
- **Comprehensive testing** (unit, functional, integration)
- **Security-first** (input validation, injection prevention)

### Testing

```bash
# Test organization
tests/
├── unit/          # Fast, isolated component tests
├── functional/    # Component interaction tests
├── integration/   # External service tests
├── end_to_end/    # Complete workflow tests
└── utils/         # Shared testing utilities
```

**Running tests:**

| Command | Description |
|---------|-------------|
| `make dev-test` | Unit tests locally (fastest feedback) |
| `make container-test` | Unit tests in Docker (full deps, ~1600 tests) |
| `make container-test-integration` | Integration tests in Docker (live-service-gated, mostly skipped without env flags) |
| `make test-slow` | Integration + E2E tests in Docker |
| `make test-verbose` | Tests with verbose output |
| `make lint` | Run linting (ruff + mypy) |
| `make check-architecture` | Enforce Cosmic Python layered architecture (import-linter) |

**Test environment variables:**
- `J2O_RUN_INTEGRATION=true` — Enable integration tests locally
- `J2O_RUN_ALL_TESTS=true` — Enable all test categories
- `J2O_ENABLE_SSH=true` — Enable SSH-dependent tests

**Running specific tests:**
```bash
# Run a specific test pattern
make container-test TEST_OPTS="-k test_user_migration"

# Run with live SSH connections (requires infrastructure)
make test-live-ssh
```

## Security

### Input Validation

All user inputs (especially Jira keys) are validated:
- Character whitelisting (`A-Z`, `0-9`, `-` only)
- Length limits (max 100 characters)
- Control character blocking

### Injection Prevention

- Ruby script generation uses safe `inspect` method
- No direct string interpolation in generated code
- Comprehensive validation before execution

## Performance

### Optimization Features

- **Batch processing** with configurable sizes
- **Connection pooling** for SSH connections
- **Adaptive polling** for console operations
- **Parallel processing** for independent operations
- **Progress tracking** with minimal overhead

### Monitoring

- Real-time progress reporting
- Detailed logging with configurable levels
- Error aggregation and reporting
- Performance metrics collection

## Troubleshooting

### Common Issues

**Connection Problems:**
- Verify SSH key permissions (`chmod 600`)
- Check OpenProject container status
- Validate tmux session configuration

**Migration Errors:**
- Review logs in `var/logs/`
- Check data validation reports
- Verify OpenProject permissions

**Performance Issues:**
- Adjust batch sizes in configuration
- Monitor system resources
- Check network connectivity

### Debug Commands

```bash
# Test connections
python scripts/test_connections.py

# Validate configuration
python scripts/validate_config.py

# Debug specific migration
python src/main.py migrate --debug --components users --limit 10

# Install .irbrc into remote OpenProject container (stabilizes Rails console IRB)
make install-irbrc

# Start tmux session for Rails console (uses J2O_OPENPROJECT_* env)
python scripts/start_rails_tmux.py --attach
```

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

1. **Follow development standards** outlined in [Developer Guide](docs/DEVELOPER_GUIDE.md)
2. **Write comprehensive tests** for all changes
3. **Validate security** for any user input processing
4. **Update documentation** as needed
5. **Run full test suite** before submitting changes

For architecture changes, see [System Architecture](docs/ARCHITECTURE.md) for design patterns and component responsibilities.


### Upgrading the local Compose database

The migration tool's Compose database uses the PostgreSQL 18 Alpine image. An existing PostgreSQL 17 data volume requires a major-version upgrade before starting the new database service. Keep the old database and a verified logical backup, restore it into a fresh 18 cluster and compare the restored databases, roles and application tables before switching. The explicit `PGDATA` keeps the old mount visible so startup fails on incompatible data. The Compose database is separate from OpenProject. Preserve the migration tool’s SQLite checkpoints and mappings in the mounted `var/` directory independently.

Compose follows the Valkey 9, PostgreSQL 18 and Prism 5 image tags. Both Dockerfiles follow Python 3 and Dockerfile syntax 1. The uv binary comes from its upstream rolling image because the registry has no major-only uv tag. Use `docker compose pull` and `docker compose build --pull` to refresh locally cached images. The migration's Python dependencies remain locked in `uv.lock`.

Valkey uses a new `valkey_data` volume. For an existing stack, finish the active migration or stop at a verified checkpoint before switching. Stop and remove the old Redis service container using the previous Compose file, and retain its `redis_data` volume. A fresh run starts with an empty cache. To resume with cached idempotency results, transfer the application's Redis string keys with their remaining lifetimes and check the results before resuming. Keep the application's `var/` checkpoints as well. The new service accepts the existing `redis` network alias and both clients honor `REDIS_URL`.
