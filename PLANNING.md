# Initial Development Plan

This document outlines the initial plan followed during the development of the Jira to OpenProject migration tool.

## Phase 1: Foundation & Core Components

1.  **Project Setup:**
    *   Initialize Python project structure (`src`, `tests`, `scripts`, `docs`).
    *   Set up virtual environment (`venv`) and `requirements.txt`.
    *   Establish basic `README.md`.
    *   Configure Git repository and `.gitignore`.
    *   Set up Docker environment (`Dockerfile`, `compose.yaml`).
2.  **Configuration:**
    *   Design configuration strategy (YAML + Environment Variables).
    *   Implement `ConfigLoader` (`src/config_loader.py`).
    *   Create initial `config/config.yaml`, `.env`, `.env.local` examples.
3.  **API Clients:**
    *   Implement basic Jira API client (`src/clients/jira_client.py`) for core operations (fetching projects, issues, users).
    *   Implement basic OpenProject API client (`src/clients/openproject_client.py`) for core operations (creating/fetching projects, users, work packages).
4.  **Core Migration Framework:**
    *   Design base migration class (`src/migrations/base_migration.py`).
    *   Develop main runner script (`run_migration.py`) to orchestrate migrations, handle command-line arguments (e.g., `--components`, `--dry-run`).
    *   Implement basic logging and display utilities (`src/display.py`).
5.  **Initial Migrations (Proof of Concept):**
    *   Implement User migration (`user_migration.py`).
    *   Implement Project migration (`project_migration.py`).
    *   Implement basic Work Package migration (`work_package_migration.py`) focusing on core fields.

## Phase 2: Handling Complexities & API Limitations

1.  **Advanced Work Package Migration:**
    *   Handle hierarchy (Epics, Sub-tasks -> Parent/Child relationships).
    *   Migrate comments.
    *   Migrate attachments.
    *   Implement issue link migration (requires Link Type migration first).
2.  **Metadata Migration:**
    *   Implement Status migration (`status_migration.py`) and mapping.
    *   Implement Issue Type migration (`issue_type_migration.py`) - **Requires Rails Console**.
    *   Implement Link Type migration (`link_type_migration.py`) and mapping.
3.  **Custom Fields:**
    *   Implement Custom Field migration (`custom_field_migration.py`) - **Requires Rails Console**.
    *   Develop strategy for mapping Jira custom fields to OpenProject equivalents.
    *   Handle custom field value mapping in Work Package migration.
4.  **Rails Console Integration:**
    *   Develop OpenProject Rails Client (`src/clients/openproject_rails_client.py`) using SSH/Docker.
    *   Integrate Rails client into Custom Field and Issue Type migrations.
    *   Provide option to generate Ruby scripts as a fallback.
    *   Create test script for Rails connection (`scripts/test_rails_connection.py`).
5.  **Workflow Migration:**
    *   Implement Workflow analysis (`workflow_migration.py`) to map Jira workflows to OpenProject status transitions per type.
    *   Document manual steps required for workflow configuration in OpenProject.
6.  **Plugin-Specific Data:**
    *   Implement Tempo Account migration (`account_migration.py`, potentially renamed/refactored) by mapping to an OpenProject custom field.
    *   Implement Company migration (`company_migration.py`) based on defined mapping strategy (e.g., Jira field -> OP Project).

## Phase 3: Refinement, Testing & Documentation

1.  **Refinement:**
    *   Improve error handling, logging, and reporting across all components.
    *   Optimize API usage (batching, rate limiting).
    *   Refine data mapping strategies and allow for custom overrides.
    *   Implement `--force` option for re-extraction.
2.  **Testing:**
    *   Develop testing strategy (unit tests, integration tests, manual validation).
    *   Implement basic environment tests (`tests/test_environment.py`).
    *   Define comprehensive data validation steps (counts, spot checks).
    *   Define and execute User Acceptance Testing (UAT) scenarios.
3.  **Documentation:**
    *   Expand `README.md` with detailed setup, usage, and technical info.
    *   Create detailed `docs/configuration.md`.
    *   Create `docs/development.md` for contributors.
    *   Create READMEs for `src/`, `scripts/`, `tests/`.
    *   **Consolidate `PROGRESS.md` content into new structured documentation (TASKS.md, READMEs, etc.).**
    *   Document manual steps required for migration.
4.  **Rollback Strategy:**
    *   Define and document a rollback strategy.
    *   Test rollback procedures in a non-production environment.

## Phase 4: Production Migration Planning

1.  **Final Preparations:**
    *   Finalize all code and documentation.
    *   Perform full end-to-end testing in a staging environment mirroring production.
2.  **Scheduling:**
    *   Define production downtime window.
    *   Prepare communication plan for stakeholders.
3.  **Execution:**
    *   Execute final production migration.
    *   Perform post-migration verification checks.
