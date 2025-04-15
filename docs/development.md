# Development Guide

This guide provides instructions for setting up the development environment, running tests, and contributing to the Jira to OpenProject migration tool. It is up to date with the current codebase and practices.

## Development Environment Setup

Docker is the **required** development environment to ensure consistency and simplify interaction with the OpenProject Rails console if needed.

### Prerequisites

*   Docker Desktop or Docker Engine/CLI
*   Docker Compose
*   Git
*   An editor with Python support (like VS Code with the recommended extensions in `.devcontainer/devcontainer.json`)

### Steps

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd jira-to-openproject-migration
    ```

2.  **Configure Local Environment:**
    *   Copy `.env` to `.env.local`: `cp .env .env.local`
    *   Edit `.env.local` and provide **test/development** Jira and OpenProject instance details. **Do NOT use production credentials for development.**
    *   If you plan to test the `--direct-migration` feature, configure the SSH/Docker access variables for your **test** OpenProject instance in `.env.local`.
        *   `J2O_OPENPROJECT_SERVER`: Hostname or IP of the server running the OP Docker container.
        *   `J2O_OPENPROJECT_SSH_USER`: SSH username for the server.
        *   `J2O_OPENPROJECT_SSH_KEY_PATH`: Path to your SSH private key (e.g., `~/.ssh/id_rsa`).
        *   `J2O_OPENPROJECT_CONTAINER`: Name of the OpenProject web/app container (often `openproject-web-1` or similar).
        *   `J2O_OPENPROJECT_RAILS_PATH`: Path to the OpenProject app within the container (usually `/app`).

3.  **Build and Start Docker Container:**
    ```bash
    # Build and start the service in detached mode
    docker compose up -d --build
    ```
    This uses the `compose.yaml` and `Dockerfile` to create the `j2o-app` service.

4.  **Accessing the Container:**
    *   **Shell Access:**
        ```bash
        docker exec -it j2o-app /bin/bash
        ```
    *   **Running Commands:** Execute commands directly:
        ```bash
        docker exec -it j2o-app python src/main.py --help
        ```
    *   **VS Code Dev Container:** If using VS Code, open the command palette (Ctrl+Shift+P) and select "Remote-Containers: Reopen in Container". This will automatically build/start the container and connect your editor.

## Running Tests

Tests are run using `pytest` inside the Docker container. The test suite covers environment validation, migration components, utilities, and end-to-end processes. See [tests/README.md](../tests/README.md) for details.

```bash
# Run all tests
docker exec -it j2o-app pytest

# Run specific test file
docker exec -it j2o-app pytest tests/test_environment.py

# Run tests with verbose output
docker exec -it j2o-app pytest -v
```

## Coding Standards & Guidelines

*   **Language:** Python 3.13. Follow modern Python practices.
*   **Style:**
    *   Use `black` for code formatting.
    *   Use `isort` for import sorting.
    *   Follow PEP 8 guidelines.
    *   Use `ruff` or `flake8` for linting.
*   **Type Hinting:** Use type hints extensively for clarity and static analysis.
*   **Logging:** Use Python's standard `logging` module. Configure levels via environment variables (`J2O_LOG_LEVEL`).
*   **Configuration:** Access all configuration via the `src.config` module. Do not access environment variables directly outside `src.config_loader`.
*   **Error Handling:** Implement robust error handling, especially around API calls and file I/O.
*   **Modularity:** Keep migration components focused and independent where possible.
*   **Dependencies:** Add new dependencies to `requirements.txt` and rebuild the Docker image (`docker compose build`).
*   **Documentation:**
    *   Use docstrings for modules, classes, and functions.
    *   Keep README files (`README.md`, `src/README.md`, etc.) updated.
    *   Update `TASKS.md` as features are developed or bugs fixed.

## Key Development Tasks

Refer to [TASKS.md](TASKS.md) for the list of pending implementation and testing tasks.

*   **Implementing Test Cases:** Expand the test suite (`tests/`) as new features are added.
*   **Refining Error Handling:** Make the migration process more resilient to API errors, network issues, and unexpected data.
*   **Improving Validation:** Add more automated checks to validate the migrated data.
*   **Optimizing Performance:** Investigate bottlenecks in API interaction and data processing.

## Contribution Process

1.  Ensure you have a development environment setup.
2.  Create a new branch for your feature or bug fix: `git checkout -b feature/my-new-feature` or `fix/issue-123`.
3.  Implement your changes, adhering to coding standards.
4.  Add tests for your changes.
5.  Ensure all tests pass: `docker exec -it j2o-app pytest`.
6.  Update relevant documentation (`TASKS.md`, READMEs, docstrings).
7.  Commit your changes with clear messages.
8.  Push your branch to the repository.
9.  Open a Pull Request for review.
