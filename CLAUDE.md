# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Jira to OpenProject migration tool (j2o) that migrates project management data from Jira Server 9.11 to OpenProject 15. It follows a modular Extract-Map-Load pattern with each migration component (users, projects, issues, etc.) implementing the BaseMigration abstract class.

## Common Development Commands

### Setup and Installation
```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install in development mode
pip install -e .

# Install pre-commit hooks
pre-commit install
```

### Running Tests
```bash
# Run all tests
pytest

# Run specific test categories
pytest -m unit
pytest -m functional
pytest -m integration

# Run tests with coverage
pytest --cov=src --cov-report=html

# Run tests in parallel
pytest -n auto

# Run a single test file
pytest tests/unit/test_config_loader.py

# Run a single test
pytest tests/unit/test_config_loader.py::test_load_config
```

### Code Quality
```bash
# Format code with black
black src tests

# Sort imports
isort src tests

# Run linting
ruff check src tests

# Type checking
mypy src

# Run all pre-commit hooks
pre-commit run --all-files
```

### Running the Migration
```bash
# Main entry point - dry run
j2o migrate --dry-run

# Migrate specific components
j2o migrate --components users projects

# Force re-extraction
j2o migrate --force --components users

# Full migration
j2o migrate
```

## High-Level Architecture

### Layered Client Architecture
The system uses a hierarchical client architecture for interacting with OpenProject:

1. **OpenProjectClient** (Top Level)
   - Orchestrates all operations
   - Owns and initializes all other clients
   - Provides high-level API methods

2. **SSHClient** (Foundation Layer)
   - Handles all SSH operations
   - Provides connection pooling and retry logic
   - Used by DockerClient via dependency injection

3. **DockerClient** (Container Layer)
   - Manages Docker container operations
   - Receives SSHClient as constructor parameter
   - Handles file transfers to/from containers

4. **RailsConsoleClient** (Console Layer)
   - Interacts with Rails console via tmux sessions
   - Implements marker-based error detection
   - Independent of SSH/Docker concerns

### Migration Pattern
Each migration component follows the Extract-Map-Load pattern:

1. **Extract**: Fetch data from Jira API
2. **Map**: Transform data and create ID mappings
3. **Load**: Import data into OpenProject (via API or Rails console)

All components inherit from `BaseMigration` and implement these methods:
- `_extract()`: Retrieves data from source system
- `_map()`: Transforms data for target system
- `_load()`: Imports data into target system
- `_test()`: Validates the migration results

### Key Directories
- `src/migrations/`: Migration components (user, project, work_package, etc.)
- `src/clients/`: API and system interaction clients
- `var/data/`: Extracted data and mapping files
- `var/scripts/`: Generated Ruby scripts for Rails console
- `var/logs/`: Application and error logs

### Error Handling
The system implements comprehensive error handling:
- Retry logic with exponential backoff for API calls
- State preservation for resuming failed migrations
- Structured error reporting in `var/data/migration_issues.json`
- Marker-based error detection in Rails console operations

### Configuration
Configuration is loaded from multiple sources in priority order:
1. Environment variables
2. `.env.local` file
3. `.env` file
4. `config/config.yaml`

Access configuration via the global `config` object from `src.config`.

## Important Implementation Details

### Rails Console Integration
For operations not supported by the OpenProject API:
- Commands are executed via tmux sessions for persistence
- Uses unique marker system to distinguish errors from output
- Implements adaptive polling for performance optimization

### Two-Step Migration Approach
Several components use a two-step process:
1. Create basic entities with required fields
2. Enhance with metadata and relationships

This provides better error recovery and cleaner code organization.

### File Management
The `FileManager` class centralizes all file operations:
- Tracks temporary files for cleanup
- Creates debug sessions for troubleshooting
- Ensures consistent file handling across components