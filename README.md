# Jira to Openproject Migration

This project aims to migrate a company's project management from Jira Server on-premise 9.11 to Openproject 15.

For current migration status and progress, see [PROGRESS.md](PROGRESS.md).

## Project Setup

### Prerequisites

- Python 3.13
- Docker (required for standard development)
- Access to Jira Server API
- Access to OpenProject API
- Appropriate permissions in both systems

### Installation

#### Docker Setup (Required)

The project is designed to run in Docker containers, which provide a consistent environment with Python 3.13 and all dependencies.

@todo: copy and adapt compose.dev.yml to compose.override.yml

```bash
# Start the Docker environment
docker compose up -d

# Run commands in the container
docker exec -it jira-to-openproject python run_migration.py --dry-run
```

#### Python Virtual Environment (Fallback Only)

A local Python environment should only be used as a fallback for specific testing scenarios:

```bash
python -m venv venv
source venv/bin/activate  # On Windows, use: venv\Scripts\activate
pip install -r requirements.txt
```

> **Note**: The virtual environment approach requires Python 3.13 and may not provide the same environment consistency as Docker.

### Configuration

This project uses a configuration system combining environment variables and YAML files.

For detailed configuration information, see [Configuration Guide](./docs/configuration.md).

Key configuration points:
- Environment variables use the `J2O_` prefix
- `.env` contains version-controlled defaults
- `.env.local` contains non-versioned custom settings (for sensitive data)
- Required variables are listed in the Configuration Guide

## Technical Specifications

### Python 3.13 Features

This project leverages modern Python 3.13 features:

- Type hints with union types
- Pattern matching
- Dataclasses with slots
- Async/await for I/O operations
- f-strings with `=` operator
- Match statements
- PEP 695 type aliases
- Enhanced type parameter syntax

### API Limitations

OpenProject has certain API limitations that require special handling:

1. Custom Fields: Cannot be created via API
   - Integrated into the main migration framework
   - Supports direct execution via Rails console or script generation
   - Use `python run_migration.py --components custom_fields` for full migration
   - Or use standalone: `python -m src.migrations.custom_field_migration --direct-migration`

2. Issue Types/Work Package Types: Cannot be created via API
   - Integrated into the main migration framework
   - Supports direct execution via Rails console or script generation
   - Use `python run_migration.py --components issue_types` for full migration
   - Or use standalone: `python -m src.migrations.issue_type_migration --direct-migration`

Both approaches provide:
- Automatic extraction of data from Jira and OpenProject
- Smart mapping between systems
- Direct execution on Rails console via SSH/Docker
- Detailed progress tracking with rich console interface
- Option to generate Ruby scripts for manual execution

### Technology Stack

- **Python 3.13**: Primary programming language
  - Strong API libraries (requests, httpx)
  - Excellent data processing capabilities
  - Type hints and modern features
  - Async support for I/O operations

- **Docker**: Primary development environment
  - Consistent development environment
  - Simplified deployment
  - Integrated with OpenProject for Rails console access

For implementation details and current status, refer to [PROGRESS.md](./PROGRESS.md).

## Standalone Migration

If you want to run only specific migration components, you can use the following commands:

```bash
./run_migration.py --components [component]
```

Components are:

- companies
- accounts
- projects
- work_packages
- issue_types
- custom_fields
- link_types

## Rails Console Integration

For components that cannot be migrated directly via the API (custom fields and work package types), the migration tool can either:

1. Generate Ruby scripts to be run manually in the OpenProject Rails console
2. Execute the migration commands directly on the Rails console using SSH and Docker

### Test Rails Console Connection

Before running migrations that use the Rails console, you can test your connection with:

```bash
python scripts/test_rails_connection.py --host your-op-server.example.com
```

This script verifies:
- SSH connectivity to the server
- Access to the Docker container
- Ability to launch the Rails console
- Execution of simple commands

Use `--debug` flag for verbose logging:

```bash
python scripts/test_rails_connection.py --debug
```

### Custom Mapping

To use a custom mapping file for custom fields:

```bash
python -m src.migrations.custom_field_migration --mapping my_fields_mapping.json
```

To use a custom mapping file for work package types:

```bash
python -m src.migrations.issue_type_migration --mapping my_types_mapping.json
```

### Sample Data

To run with sample data (for testing):

```bash
python -m src.migrations.custom_field_migration --sample
```

```bash
python -m src.migrations.issue_type_migration --sample
```

## License

MIT License

## Basic Usage

```bash
# Dry run (no changes made to OpenProject)
python run_migration.py --dry-run

# Run specific components
python run_migration.py --components users projects work_packages

# Run with direct Rails console execution for custom fields and issue types
python run_migration.py --components custom_fields issue_types --direct-migration

# Force extraction of data even if it already exists
python run_migration.py --force
```
