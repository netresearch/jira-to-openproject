# Jira to OpenProject Migration Tool

This project provides a robust, modular, and configurable toolset for migrating project management data from Jira Server 9.11 to OpenProject 15. It is designed for one-time, current-state migrations, with no backward compatibility or changelogs, following the "Immutable Now" philosophy.

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
*   **Workflow Configuration:** [docs/workflow_configuration.md](docs/workflow_configuration.md)

## Introduction

This tool automates the migration of users, projects, issues (work packages), statuses, workflows, custom fields, issue types, link types, attachments, comments, and plugin-specific data (e.g., Tempo Accounts) from Jira to OpenProject. It handles API limitations by integrating with the OpenProject Rails console (via SSH/Docker) and can generate Ruby scripts for manual execution if needed.

- **Language:** Python 3.13
- **Environment:** Docker (required for development and recommended for production)
- **Key Libraries:** `requests`, `httpx`, `rich`, `python-dotenv`, `pyyaml`
- **APIs Used:** Jira Server REST API v2, OpenProject API v3

## Quick Start

### Prerequisites

* Docker and Docker Compose
* Python 3.13
* Access credentials for Jira Server 9.11 and OpenProject 15
* (Optional) SSH access to the OpenProject server for direct Rails console integration

### Installation & Setup

1. Clone the repository and enter the directory:
    ```bash
    git clone <repository-url>
    cd jira-to-openproject-migration
    ```
2. Set up Python environment:
    ```bash
    python -m venv .venv
    source .venv/bin/activate  # On Windows: .venv\Scripts\activate
    pip install -e .
    ```
3. Configure environment variables:
    * Copy `.env` to `.env.local` and fill in your credentials and server details.
    * Edit `config/config.yaml` for migration settings if needed.
4. Build and start Docker container:
    ```bash
    docker compose up -d --build
    ```

### Usage

Run the migration tool using the CLI or directly via Python:

```bash
# Dry run (no changes made)
j2o migrate --dry-run
# Migrate specific components
j2o migrate --components users projects
# Migrate with Rails console integration (for custom fields/types)
j2o migrate --components custom_fields issue_types --direct-migration
# Full migration (use with caution!)
j2o migrate
# Force re-extraction
j2o migrate --force --components users
```

Or run directly:
```bash
python src/main.py --dry-run
```

**Important:** Always test in a staging environment before production migration.

## Key Concepts

* **Components:** Migration is modular; each component (users, projects, etc.) can be run independently.
* **Mappings:** Mapping files in `var/data/*.json` translate IDs and values between systems.
* **Rails Console Integration:** For entities not supported by the OpenProject API, the tool can:
    - Execute commands via SSH/Docker (`--direct-migration`)
    - Generate Ruby scripts for manual execution
* **Dry Run:** The `--dry-run` flag simulates migration without making changes.
* **Data Directory:** The `var/` directory stores extracted data, logs, scripts, and mapping files.

## API Limitations & Workarounds

* **OpenProject:** Custom fields and work package types are created via Rails console integration. Some fields (e.g., author/creation date) cannot be set via API.
* **Jira:** For large instances, use the ScriptRunner Add-On for efficient custom field option extraction.

## Development

See [docs/development.md](docs/development.md) for setup, coding standards, and contribution process.

## License

MIT License
