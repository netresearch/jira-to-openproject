# Project Summary for AI Collaboration

This document provides a summary of the project context to assist AI agents in understanding the goals, structure, and development practices.

## 1. Project Goal

The primary objective of this project is to migrate business data from a Jira Server 9.11 instance to OpenProject 15.

## 2. Core Technology Stack

* **Backend Language**: Python 3.13+ (utilizing modern features like type hints, dataclasses, async/await, pattern matching, etc.)
* **Primary APIs**:
  * Jira Server 9.11 REST API (for data extraction)
  * OpenProject 15 API (for data import)
* **Fallback Mechanism**: OpenProject Ruby on Rails console (for data/operations not supported by the OpenProject API).
* **Containerization**: Docker (`compose.yaml`, `Dockerfile`)
* **Dependency Management**: `uv` and `requirements.txt`
* **Testing**: `pytest` (`pytest.ini` for configuration)
* **Linting/Formatting**: `ruff`, `black`
* **Logging**: `rich` library

## 3. Development Environment

* All development occurs within Docker containers managed by `docker compose`.
* Execute commands inside the application container using: `docker compose exec app <command>`
* Source files, logs, and other project files within the workspace directory are directly accessible from the host machine (bind mounts).
* Build container images using: `docker buildx bake -f compose.yml`
* A Python virtual environment (`.venv/`) is used within the container/host. Activate using `source .venv/bin/activate` or the provided `activate.sh` script if needed outside Docker.
* Environment configuration is managed via `.env` (default values, version controlled) and `.env.local` (local overrides, not version controlled).

## 4. Project Structure

* `src/`: Contains the main Python source code, organized modularly (e.g., models, services, API clients, utilities).
* `tests/`: Contains all tests written using `pytest`.
* `config/`: Project-specific configuration files.
* `docs/`: Supplementary documentation.
* `scripts/`: Utility and automation scripts.
* `examples/`: Example data files or usage scripts.
* `var/`: Variable data generated during runtime (logs, output, temporary files - specific subdirectories might be ignored per `.gitignore`). Check `var/data/*.example.json` for data structure examples.

## 5. Key Files

* `README.md`: **Source of Truth** for project overview, setup instructions, technical requirements, and architecture decisions.
* `PROGRESS.md`: Tracks current development status, upcoming tasks, and implementation notes.
* `run_migration.py`: The main script to execute the migration process.
* `compose.yaml`: Defines Docker services and configurations.
* `Dockerfile`: Defines the application's Docker image.
* `requirements.txt`: Lists Python dependencies.
* `.env`: Default environment variables.
* `.gitignore`: Specifies intentionally untracked files/directories.
* `.editorconfig`: Defines coding styles for editors.
* `pytest.ini`: Configuration for `pytest`.
* `renovate.json`: Configuration for the Renovate bot (dependency updates).

## 6. Development Guidelines

* **Python Coding**:
  * **ALWAYS** use type hints (Python 3.13+ syntax, `|` for unions, PEP 695).
  * **ALWAYS** add descriptive docstrings (PEP 257) to all functions, classes, and methods.
  * Write tests using `pytest` only; place them in the `tests/` directory. Ensure tests also have type hints and docstrings. Create `tests/__init__.py` if needed.
  * Adhere to PEP standards. Use `black` for formatting and `ruff` for linting.
  * Utilize `rich` for enhanced logging output.
  * Keep existing comments intact when modifying code.
* **Version Control (Git)**:
  * Write clear, descriptive commit messages.
  * Each commit should represent a single logical change.
  * **NEVER** commit credentials or sensitive data. Use `.env.local` for local secrets/settings.
* **Documentation**:
  * Maintain `README.md` as the primary source for stable project information.
  * Use `PROGRESS.md` for dynamic status updates and task tracking.
* **Project Developemnt modell/pattern/ethos/philosophy**:
  * Known as "Immutable Now", "YOLO Development", "Ephemeral Software" or "Rolling Release Model"
  * No need to maintain backward compatibility.
  * Document only the current functionality, refering only to the current state.
  * No versioning
  * No changelogs
  * No deprecation strategy
  * “We only care about now.”
  * Past versions are irrelevant or discarded.
  * APIs or interfaces can change at any time.

## 7. AI Agent Interaction Notes

* Refer to this document for project context.
* Adhere strictly to the Python coding standards mentioned above (typing, docstrings, testing).
* Consult `README.md` for setup, architecture, and requirements.
* Check `PROGRESS.md` for current tasks and status.
* Use the specified Docker commands for execution and environment management.
* Remember that `.env.local` overrides `.env` and is not version controlled.
