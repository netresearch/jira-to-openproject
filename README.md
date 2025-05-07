# Jira to OpenProject Migration Tool

This project provides a robust, modular, and configurable toolset for migrating project management data from Jira Server 9.11 to OpenProject 15. It is designed for one-time, current-state migrations, with no backward compatibility or changelogs, following the "Immutable Now" philosophy.

**Key Documentation:**

* **Project Goals:** [PROJECT_GOALS.md](PROJECT_GOALS.md)
* **Tasks & Status:** [TASKS.md](TASKS.md)
* **Configuration:** [docs/configuration.md](docs/configuration.md)
* **Development Setup & Guidelines:** [docs/development.md](docs/development.md)
* **Client Architecture:** [docs/client_architecture.md](docs/client_architecture.md)
* **Initial Plan:** [PLANNING.md](PLANNING.md)
* **Source Code Overview:** [src/README.md](src/README.md)
* **Scripts Overview:** [scripts/README.md](scripts/README.md)
* **Tests Overview:** [tests/README.md](tests/README.md)
* **Status Migration:** [docs/status_migration.md](docs/status_migration.md)
* **Workflow Configuration:** [docs/workflow_configuration.md](docs/workflow_configuration.md)

## Architecture

This project uses a layered architecture for interacting with OpenProject. For a detailed overview with visual diagrams of the client architecture, see [docs/client_architecture.md](docs/client_architecture.md).

```plain
┌───────────────────────────────────────┐
│          OpenProjectClient            │  High-level API & Main Orchestrator
└───┬──────────────┬─────────────┬──────┘
    │              │             │
    │ owns         │ owns        │ owns
    ▼              ▼             ▼
┌─────────┐   ┌──────────┐  ┌────────────────┐
│SSHClient│◄──┤DockerClient│ │RailsConsoleClient│
└─────────┘   └──────────┘  └────────────────┘
    │
┌───┴─────────────────────────────────┐
│              FileManager            │  Manages file operations and tracking
└─────────────────────────────────────┘
```

Each layer has a specific responsibility in the dependency hierarchy:

* **OpenProjectClient**: The top-level orchestrator that initializes and owns all client components. It provides high-level methods for record management while coordinating the workflow between components.

* **SSHClient**: The foundation layer that handles all SSH operations including connection management, command execution, and file transfers. It provides robust error handling and retry logic for network operations.

* **DockerClient**: Uses SSHClient (via dependency injection) to execute Docker commands. It's responsible for container operations and file transfers to/from the Docker container.

* **RailsConsoleClient**: Focused exclusively on interacting with Rails console via tmux sessions. It implements a sophisticated marker-based error detection system to reliably distinguish between actual errors and text in command outputs.

* **FileManager**: Centralized utility for managing file operations, including tracking temporary files and creating debug sessions.

The architecture follows a clear dependency injection pattern, where:
1. OpenProjectClient initializes and owns all other clients
2. DockerClient receives SSHClient as a constructor parameter
3. Each component has a single, well-defined responsibility

This layered design ensures clean separation of concerns and makes the system highly testable and maintainable.

## Introduction

This tool automates the migration of users, projects, issues (work packages), statuses, workflows, custom fields, issue types, link types, attachments, comments, and plugin-specific data (e.g., Tempo Accounts) from Jira to OpenProject. It handles API limitations by integrating with the OpenProject Rails console (via SSH/Docker) and can generate Ruby scripts for manual execution if needed.

* **Language:** Python 3.13
* **Environment:** Docker (required for development and recommended for production)
* **Key Libraries:** `requests`, `httpx`, `rich`, `python-dotenv`, `pyyaml`
* **APIs Used:** Jira Server REST API v2, OpenProject API v3

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

### Persistent Rails Console with tmux

For operations requiring Rails console access, a persistent tmux session is recommended to maintain connection stability during long-running migrations:

```sh
# Create a new named tmux session with logging
tmux new-session -s rails_console \; \
  pipe-pane -o 'cat >>~/rails_console.log' \; \
  send-keys 'ssh -t [OPENPROJECT_HOST] "docker exec -ti openproject-web-1 bundle exec rails console"' \
  C-m

# Reconnect to an existing session
tmux attach-session -t rails_console

# Detach from session (without killing it)
# Use Ctrl+b, then d

# List all sessions
tmux list-sessions

# Kill a session when done
tmux kill-session -t rails_console
```

**Session Management Tips:**

* Use `Ctrl+b, d` to detach from the session without terminating it
* If the connection drops, simply reattach using `tmux attach-session -t rails_console`
* Split panes with `Ctrl+b, "` (horizontal) or `Ctrl+b, %` (vertical) for monitoring multiple processes
* The migration tool handles Rails 3.4's Reline library compatibility issues with IO handling

**Note on Ruby 3.4 compatibility:**
If you encounter Rails console errors related to "ungetbyte failed (IOError)" in Ruby 3.4's Reline library, the migration tool now includes workarounds to stabilize console state after command execution. These fixes help prevent IO errors during migration operations.

### Usage

Run the migration tool using the CLI or directly via Python:

```bash
# Dry run (no changes made)
j2o migrate --dry-run
# Migrate specific components
j2o migrate --components users projects
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
* **Project Hierarchy:** Projects are organized hierarchically with Tempo companies as top-level projects and Jira projects as their sub-projects.
* **Rails Console Integration:** For entities not supported by the OpenProject API, the tool can:
  * Execute commands via SSH/Docker
  * Generate Ruby scripts for manual execution
* **Dry Run:** The `--dry-run` flag simulates migration without making changes.
* **Data Directory:** The `var/` directory stores extracted data, logs, scripts, and mapping files.

## API Limitations & Workarounds

* **OpenProject:**
  * Custom fields and work package types are created via Rails console integration.
  * Some fields (e.g., author/creation date) cannot be set via API.
  * Link types (relations) cannot be retrieved via API or created/modified. The migration tool:
    * Maps Jira link types to OpenProject's five default relation types (relates, blocks, duplicates, precedes, includes)
    * Allows users to customize mappings via a JSON configuration file
    * Falls back to creating custom fields for unmapped link types
* **Jira:** For large instances, use the ScriptRunner Add-On for efficient custom field option extraction.

## Development

See [docs/development.md](docs/development.md) for setup, coding standards, and contribution process.

## License

MIT License
