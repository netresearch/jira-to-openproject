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

#### OpenProject has certain API limitations that require special handling

- Allowed or dissallowed chars in username is not documented
- You can not set all fields for workpackages, like author/creator, creation date aso., which are improtant for a full migration.
- no custom relation types

1. Custom Fields: Cannot be created via API
   - Integrated into the main migration framework
   - Supports direct execution via Rails console (`--direct-migration` flag with `run_migration.py`)
   - Use `python run_migration.py --components custom_fields [--direct-migration]`

2. Issue Types/Work Package Types: Cannot be created via API
   - Integrated into the main migration framework
   - Supports direct execution via Rails console (`--direct-migration` flag with `run_migration.py`)
   - Use `python run_migration.py --components issue_types [--direct-migration]`

Both components provide:

- Automatic extraction of data from Jira and OpenProject
- Smart mapping between systems
- Direct execution on Rails console via SSH/Docker (when using `--direct-migration`)
- Detailed progress tracking with rich console interface
- Option to generate Ruby scripts for manual execution (if direct migration fails or is not used)

#### Jira has much more API limitations

### Jira API Expansion and Field Retrieval Limitations

- The `expand` parameter is not consistently supported across all Jira Server editions/versions
- Field selection is inconsistently implemented in different API endpoints
- Limiting the fields retrieved is crucial for performance and reducing payload size
- Some endpoints ignore field selection parameters entirely
- Rate limiting can be triggered by large response payloads
- Workarounds include:
  - Using ScriptRunner to define custom REST API endpoints with precise field control
  - Using the `/rest/api/2/search` endpoint with explicit field selection where possible

### Custom Field Options

In Jira (9.11+), retrieving custom field options requires accessing the createmeta endpoint for each combination of custom field, issue type, and project. This approach has several limitations:

- Requires multiple API calls (one per combination)
- May trigger rate limiting or CAPTCHA challenges
- Not all field options may be visible in every project/issue type context

The migration tool handles this by:

- Caching field metadata to reduce API calls

Alternatives for more efficient custom field option retrieval:

1. Using ScriptRunner to create a custom REST endpoint that exports all field options at once:

   - Install ScriptRunner for Jira if not already installed
   - Navigate to Jira Administration > Manage apps > ScriptRunner > REST Endpoints
   - Click "Add new endpoint" and create a new REST service
   - Name it "Custom Field Options Exporter" (or similar)
   - Set the endpoint path to something like "/getAllCustomFieldsWithOptions"
   - Paste the Groovy script from `scripts/scriptrunner_api_endpoint_field.groovy` in this repository
   - Save the endpoint
   - The endpoint will be available at: `https://your-jira-instance.com/rest/scriptrunner/latest/custom/getAllCustomFieldsWithOptions`
   - Configure this endpoint in your migration configuration file:

     ```yaml
     # In config.yaml
     jira:
       # Other Jira configuration...
       scriptrunner:
         enabled: true
         custom_field_options_endpoint: "https://your-jira-instance.com/rest/scriptrunner/latest/custom/getAllCustomFieldsWithOptions"
     ```

   - The migration tool will automatically use this endpoint to retrieve custom field options when running:

     ```bash
     python run_migration.py --components custom_fields
     ```

2. Manually exporting field options via the Jira admin interface and importing the data
3. Using the Jira database directly (if you have access) to query field options

For large Jira instances with many custom fields, consider using one of these alternatives to improve migration performance and reliability.

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

## Rails Console Integration

For components that cannot be migrated directly via the API (custom fields and work package types), the migration tool can either:

1. Execute the migration commands directly on the Rails console using SSH and Docker (via the `--direct-migration` flag).
2. Generate Ruby scripts to be run manually in the OpenProject Rails console (if direct migration fails or is not used).

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

Custom mapping files (e.g., for users, projects, custom fields, issue types) are typically loaded automatically from the `var/data/` directory if they exist. You can pre-populate or modify these JSON files to influence the migration mapping.

### Sample Data

Support for running with sample data is not currently implemented via the main `run_migration.py` script.

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
