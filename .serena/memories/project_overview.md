# Project Overview - j2o (Jira to OpenProject Migration Tool)

## Purpose
Robust, modular migration toolset for transferring project management data from Jira Server 9.x to OpenProject 16.x. Built with Python 3.13, handles complete migration including users, projects, work packages, custom fields, statuses, workflows, attachments, time entries, and agile boards.

## Key Capabilities
- **Batch Processing**: Configurable batch sizes for large datasets
- **Progress Tracking**: Real-time progress monitoring with detailed logging
- **Error Recovery**: Comprehensive error handling with retry mechanisms (tenacity, pybreaker)
- **Security**: Input validation and injection attack prevention
- **Flexible Deployment**: Local execution or Docker containerization
- **Rails Console Integration**: Direct OpenProject ActiveRecord manipulation via tmux-backed Rails console

## Architecture Overview

### Layered Client Architecture
```
Local Migration Tool (Python)
    ↓ SSH Connection (SSHClient)
Remote OpenProject Server
    ↓ Docker Commands (DockerClient)
OpenProject Container
    ↓ tmux + Rails Console (RailsConsoleClient)
OpenProject Application (ActiveRecord)
```

### Core Components
- **src/main.py**: CLI entry point (`j2o` command)
- **src/migration.py**: Migration orchestration and pipeline
- **src/clients/**: Layered client hierarchy (SSH, Docker, Rails Console, OpenProject)
- **src/migrations/**: 40+ migration modules following BaseMigration pattern (extract→map→load)
- **src/mappings/**: Data transformation and mapping logic
- **src/models/**: Pydantic data models for validation
- **src/utils/**: Enhanced utilities (timestamp, audit trail, user association)
- **src/dashboard/**: Optional FastAPI web dashboard for monitoring

### Migration Flow
1. **Extract**: Data from Jira via REST API
2. **Transform**: Data mapping and validation
3. **Load**: Data insertion via OpenProject Rails console (JSON → Ruby → ActiveRecord)
4. **Verify**: Validation and error reporting

## Migration Components
- **Users**: Jira users → OpenProject with J2O provenance, locale→language mapping, avatar backfills
- **Groups**: Jira groups → OpenProject with role-based memberships
- **Projects**: Jira projects → OpenProject sub-projects with lead assignment, module enablement, provenance
- **Work Packages**: Issues → Work packages with metadata, start-date derivation from custom fields or status transitions
- **Custom Fields**: Field definitions and values migration
- **Status & Workflow**: Status creation and workflow configuration
- **Attachments**: Issue attachments → Work package files
- **Time Entries**: Tempo worklogs → OpenProject time entries
- **Agile Boards**: Jira boards → OpenProject saved queries with sprint mapping
- **Admin Schemes**: Jira role memberships → OpenProject project memberships
- **Reporting**: Saved filters & dashboards → OpenProject queries and wiki summaries

## Supported Platforms
- **Jira Server/Data Center 9.x** (validated on 9.11)
- **OpenProject 16.x** (validated on 16.0)
- **Python 3.13+**
- **Docker & Docker Compose** for development/testing
- **tmux** for Rails console interaction

## Work Tracking
**Important**: Use `bd` (not markdown) for all work tracking and task management.
- Run `bd quickstart` for interactive workflow guide
- See root AGENTS.md for bd command reference
