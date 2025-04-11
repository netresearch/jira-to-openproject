# Jira to OpenProject Migration Tool

This project provides a toolset for migrating project management data from Jira Server 9.11 to OpenProject 15.

**Key Documentation:**

*   **Project Goals:** [PROJECT_GOALS.md](PROJECT_GOALS.md)
*   **Tasks & Status:** [TASKS.md](TASKS.md)
*   **Configuration:** [docs/configuration.md](docs/configuration.md)
*   **Development Setup & Guidelines:** [docs/development.md](docs/development.md)
*   **Initial Plan:** [PLANNING.md](PLANNING.md)
*   **Source Code Overview:** [src/README.md](src/README.md)
*   **Scripts Overview:** [scripts/README.md](scripts/README.md)
*   **Tests Overview:** [tests/README.md](tests/README.md)
*   **Status Migration:** [docs/status_migration.md](docs/status_migration.md)

## Introduction

Migrating between complex project management systems like Jira and OpenProject requires careful handling of data mapping, API limitations, and potential inconsistencies. This tool aims to automate much of this process, providing a configurable and robust solution.

It leverages Python 3.13, Docker for environment consistency, and direct integration with the OpenProject Rails console for operations not supported by the standard API.

## Quick Start

### Prerequisites

*   Docker and Docker Compose
*   Access credentials for both your Jira Server 9.11 API and your OpenProject 15 API.
*   (Optional but Recommended) SSH access to the server hosting the OpenProject Docker container if you intend to use `--direct-migration` for custom fields/types.

### Installation & Setup

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd jira-to-openproject-migration
    ```

2.  **Configure Environment:**
    *   Copy the default environment file: `cp .env .env.local`
    *   Edit `.env.local` and fill in your specific Jira and OpenProject URLs, API tokens/credentials, and any necessary SSH details for direct Rails console access.
    *   Review `config/config.yaml` for other migration settings (batch sizes, rate limits, etc.) and adjust if needed.
    *   See the [Configuration Guide](docs/configuration.md) for full details.

3.  **Build and Start Docker Container:**
    ```bash
    docker compose up -d --build
    ```

### Basic Usage

Run commands inside the Docker container:

```bash
# Install the tool (development mode)
pip install -e .

# The tool can be run using the 'j2o' command:

# Perform a dry run migration (simulates migration, no changes made)
j2o migrate --dry-run

# Run migration for specific components (e.g., users and projects)
j2o migrate --components users projects

# Run migration including components requiring Rails console access
# (Ensure SSH/Docker access is configured in .env.local)
j2o migrate --components custom_fields issue_types --direct-migration

# Run the full migration (use with caution!)
j2o migrate

# Force re-extraction of data from Jira/OpenProject
j2o migrate --force --components users

# Export work packages for bulk import
j2o export --projects PROJECT1 PROJECT2

# Import work packages from exported JSON files
j2o import --project PROJECT1
```

**Alternatively, you can run the scripts directly:**

```bash
# Using the main entry point
python -m src.main migrate --dry-run

# Using the legacy scripts
python run_migration.py --dry-run
python export_work_packages.py --projects PROJECT1 PROJECT2
```

**Important:** Always perform a `--dry-run` and test thoroughly in a staging environment before running a full migration on production data.

## Key Concepts

*   **Components:** The migration is broken down into logical components (users, projects, work_packages, etc.) that can often be run independently.
*   **Mappings:** The tool relies on mapping files (usually stored in `var/data/*.json`) to translate IDs and values between Jira and OpenProject. Some are generated automatically, others might require manual review or creation.
*   **Rails Console Integration:** For creating Custom Fields and Work Package Types in OpenProject (which lack API support), the tool can:
    *   Directly execute commands on the Rails console via SSH/Docker (`--direct-migration`). Requires proper configuration in `.env.local`.
    *   Generate Ruby scripts (`--generate-ruby` option for relevant components) that you can manually run on the OpenProject server's Rails console.
*   **Status Migration:** The migration of Jira statuses to OpenProject includes:
    *   Automatic status extraction and mapping between Jira and OpenProject
    *   Rails console integration for creating new statuses in OpenProject
    *   Detailed documentation in [docs/status_migration.md](docs/status_migration.md)
    *   Testing and validation to ensure status mapping correctness
*   **Dry Run:** The `--dry-run` flag prevents the tool from making any creating/updating calls to the OpenProject API or Rails console.
*   **Configuration:** See [docs/configuration.md](docs/configuration.md).
*   **Data Directory:** The `var/` directory stores extracted data, logs, generated scripts, and mapping files. It's crucial for the migration process.

## Technical Specifications

*   **Language:** Python 3.13
*   **Environment:** Docker
*   **Key Libraries:** `requests`, `httpx`, `rich`, `python-dotenv`, `pyyaml`
*   **APIs Used:** Jira Server REST API v2, OpenProject API v3

### API Limitations & Workarounds

The tool incorporates strategies to handle known limitations:

*   **OpenProject:**
    *   *Custom Fields & Work Package Types:* Created via Rails Console Integration (direct execution or generated scripts).
    *   *Non-settable Fields:* Certain fields like author/creation date on work packages cannot be set via API and will default to the migration user/time.
*   **Jira:**
    *   *API Expansion/Field Selection:* The tool attempts efficient data fetching but performance can vary. Using the Jira ScriptRunner Add-On with custom endpoints (see `scripts/scriptrunner_api_endpoint_field.groovy`) is recommended for large instances, especially for fetching custom field options efficiently.

## Development

See [docs/development.md](docs/development.md) for instructions on setting up a development environment, coding standards, and contribution guidelines.

## License

MIT License
