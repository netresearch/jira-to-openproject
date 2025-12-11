# Repository Structure Index

## Project Root
```
j2o/
├── src/                    # Main source code
├── tests/                  # Test suite (unit, integration, e2e)
├── docs/                   # Documentation
├── config/                 # Configuration files
├── scripts/                # Utility scripts
├── var/                    # Runtime data (logs, cache, checkpoints)
├── contrib/                # Contributed scripts and tools
├── examples/               # Example configurations
├── api-specs/              # API specifications
└── .serena/                # Serena MCP memories
```

## Source Code Structure (`src/`)

### Entry Points
- `src/main.py` - CLI entry point (`j2o` command)
- `src/migration.py` - Migration orchestration (Migration class, run_migration)

### Client Layer (`src/clients/`)
```
SSHClient (Foundation)
    ↓
DockerClient (Container Operations)
    ↓
RailsConsoleClient (Console Interaction)
    ↓
OpenProjectClient (Orchestration)
```

| Client | File | Purpose |
|--------|------|---------|
| SSHClient | `ssh_client.py` | SSH connections, remote commands |
| DockerClient | `docker_client.py` | Container lifecycle, file transfers |
| RailsConsoleClient | `rails_console_client.py` | tmux + Rails console management |
| OpenProjectClient | `openproject_client.py` | High-level OP operations |
| EnhancedJiraClient | `enhanced_jira_client.py` | Jira API with pagination |
| JiraClient | `jira_client.py` | Basic Jira operations |

### Migration Components (`src/migrations/`)

#### Base Class
- `base_migration.py` - `BaseMigration` abstract class (extract→map→load pattern)

#### Core Migrations
| Component | File | Description |
|-----------|------|-------------|
| Users | `user_migration.py` | Jira users → OP users |
| Groups | `group_migration.py` | Jira groups → OP groups |
| Projects | `project_migration.py` | Jira projects → OP projects |
| Work Packages | `work_package_migration.py` | Issues → Work packages |
| WP Skeleton | `work_package_skeleton_migration.py` | Phase 1: WP structure |
| WP Content | `work_package_content_migration.py` | Phase 2: WP content |

#### Metadata Migrations
| Component | File | Description |
|-----------|------|-------------|
| Issue Types | `issue_type_migration.py` | Issue types → WP types |
| Statuses | `status_migration.py` | Jira statuses → OP statuses |
| Priorities | `priority_migration.py` | Priorities mapping |
| Workflows | `workflow_migration.py` | Workflow configuration |
| Custom Fields | `custom_field_migration.py` | Custom field definitions |
| Resolutions | `resolution_migration.py` | Resolution mapping |

#### Content Migrations
| Component | File | Description |
|-----------|------|-------------|
| Attachments | `attachments_migration.py` | File attachments |
| Time Entries | `time_entry_migration.py` | Tempo worklogs |
| Relations | `relation_migration.py` | Issue links |
| Watchers | `watcher_migration.py` | Issue watchers |
| Labels | `labels_migration.py` | Issue labels |
| Versions | `versions_migration.py` | Fix versions |
| Components | `components_migration.py` | Jira components |

#### Specialized Migrations
| Component | File | Description |
|-----------|------|-------------|
| Agile Boards | `agile_board_migration.py` | Boards → Saved queries |
| Sprint/Epic | `sprint_epic_migration.py` | Sprint and epic data |
| Story Points | `story_points_migration.py` | Estimation data |
| Admin Schemes | `admin_scheme_migration.py` | Role memberships |
| Reporting | `reporting_migration.py` | Filters → Queries |
| Remote Links | `remote_links_migration.py` | External links |
| Inline Refs | `inline_refs_migration.py` | Description links |

### Utilities (`src/utils/`)

#### Core Utilities
| Utility | File | Purpose |
|---------|------|---------|
| Checkpoint Manager | `checkpoint_manager.py` | Migration state persistence |
| Batch Processor | `batch_processor.py` | Batch operation handling |
| Rate Limiter | `rate_limiter.py` | API rate limiting |
| Retry Manager | `retry_manager.py` | Retry with backoff |
| Error Recovery | `error_recovery.py` | Error handling patterns |

#### Data Utilities
| Utility | File | Purpose |
|---------|------|---------|
| Data Handler | `data_handler.py` | JSON/data manipulation |
| File Manager | `file_manager.py` | File I/O operations |
| Validators | `validators.py` | Input validation |
| Markdown Converter | `markdown_converter.py` | Jira→Markdown conversion |
| Timezone | `timezone.py` | Timezone handling |

#### Migration Utilities
| Utility | File | Purpose |
|---------|------|---------|
| Enhanced Audit Trail | `enhanced_audit_trail_migrator.py` | Journal migration |
| Enhanced User Association | `enhanced_user_association_migrator.py` | User mapping |
| Enhanced Timestamp | `enhanced_timestamp_migrator.py` | Timestamp handling |
| Time Entry Transformer | `time_entry_transformer.py` | Worklog conversion |
| Change Detector | `change_detector.py` | Delta detection |
| Staleness Manager | `staleness_manager.py` | Cache invalidation |
| Idempotency | `idempotency_manager.py` | Idempotent operations |

### Models (`src/models/`)
| Model | File | Purpose |
|-------|------|---------|
| Migration Results | `migration_results.py` | Result containers |
| Migration Error | `migration_error.py` | Error types |
| Component Results | `component_results.py` | Component status |
| Mapping | `mapping.py` | ID mapping models |

### Configuration (`src/config/`)
| File | Purpose |
|------|---------|
| `__init__.py` | Config exports |
| `error_recovery_config.py` | Error recovery settings |

### Mappings (`src/mappings/`)
| File | Purpose |
|------|---------|
| `mappings.py` | Data transformation maps |

### Dashboard (`src/dashboard/`)
| File | Purpose |
|------|---------|
| `app.py` | FastAPI dashboard |
| `templates/` | Jinja2 templates |
| `static/` | CSS/JS assets |

## Documentation

### Core Documentation Files
- `docs/ENTITY_MAPPING.md` - Comprehensive Jira→OpenProject entity mapping reference (NEW)
- `docs/MIGRATION_COMPONENTS.md` - Module catalog with development state (UPDATED v2.0)
- `docs/ARCHITECTURE.md` - System architecture with client layer hierarchy
- `docs/CLIENT_API.md` - Client API reference
- `docs/DEVELOPER_GUIDE.md` - Development standards and testing
- `docs/WORKFLOW_STATUS_GUIDE.md` - Workflow configuration
- `docs/SECURITY.md` - Security measures
- `docs/QUICK_START.md` - Getting started guide
- `docs/configuration.md` - Configuration options
- `docs/adr/README.md` - ADR index (NEW)
- `docs/adr/ADR-001-two-phase-work-package-migration.md` - Two-phase WP migration architecture