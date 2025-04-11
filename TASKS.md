# Migration Tasks

This document tracks the detailed tasks required for the Jira to OpenProject migration. It combines planning, implementation status, and testing/validation requirements.

## Phase 1: Planning & Setup

- [x] Define acceptance criteria
- [x] Clarify specifications
- [x] Identify impediments
- [x] Identify what to import and in which order
- [x] Select programming language (Python 3.13)
- [x] Create data mapping strategy (initial)
- [x] Set up test environment (Docker-based)
- [x] Create initial Rollback Strategy
- [x] Setup Project Structure (Python, Docker, Git)
- [x] Implement Configuration Loading
- [x] Implement basic API Clients (Jira, OpenProject)
- [x] Implement Core Migration Runner (`run_migration.py`)

## Phase 2: Component Implementation & Migration

Each component migration involves extraction, mapping, creation/update in OpenProject, and definition of testing steps.

- **Users** (`user_migration.py`)
    - [x] Extract Jira users
    - [x] Extract OpenProject users
    - [x] Implement User Mapping Strategy (AD/LDAP assumption)
        - [x] Load existing user mappings if available (`var/data/user_mapping.json`)
        - [x] Map users based on email/username
    - [x] Create/Update users in OpenProject via API
    - [x] Define testing steps for user migration
    - [x] Test user creation/update accuracy
    - [x] Test user mapping correctness

- **Custom Fields** (`custom_field_migration.py`)
    - [x] Extract Jira custom fields metadata (handling API limitations, consider ScriptRunner)
    - [x] Define OpenProject custom field equivalents strategy
    - [x] Implement Custom Field Mapping Strategy
        - [x] Map Jira field types to OpenProject field types
        - [x] Handle custom field options (potential Jira API bottleneck)
    - [x] Generate Rails script for OpenProject custom field creation (`--generate-ruby`)
    - [x] Implement direct Rails console execution (`--direct-migration`)
    - [x] Define execution steps for Rails script/direct execution
        - Option 1: Direct Migration (Automated):
          1. Ensure OpenProject container is running and Rails console is accessible
          2. Run `python run_migration.py --components custom_fields --direct-migration`
          3. The script will:
             - Connect to the Rails console directly
             - Create custom fields one by one
             - Update mapping file with new IDs automatically
        - Option 2: Ruby Script (Semi-Automated):
          1. Run `python run_migration.py --components custom_fields`
          2. Review the generated Ruby script in the `output` directory
          3. Execute via Docker: `docker exec -it CONTAINER_NAME rails runner path/to/script.rb`
          4. Run `python run_migration.py --update-mapping` to update mapping file
    - [x] Execute Rails script/direct command for custom field creation in test environment
        - Example: `python run_migration.py --components custom_fields --direct-migration`
        - Alternatively: Generate Ruby script and execute manually, then update mapping
    - [x] Implement logic to update mapping file (`var/data/custom_field_mapping.json`) with new custom field IDs
    - [x] Define testing steps for custom field creation
        1. Verify custom field count in OpenProject matches expected count
        2. Verify field names match expected names
        3. Verify field types are correctly mapped (text, list, date, etc.)
        4. For list fields, verify values are correctly populated
        5. Verify fields appear correctly in work package forms
        6. Verify the mapping file contains correct IDs for all fields
    - [x] Test custom field creation and type mapping

- **Companies** (`company_migration.py`)
    - [x] Define Company Mapping Strategy (e.g., specific Jira field -> OP top-level projects)
    - [x] Extract necessary Jira data for companies
    - [x] Extract existing OpenProject projects (for matching)
    - [x] Map Jira company data to OpenProject project structure
    - [x] Create/Update top-level projects (Companies) in OpenProject via API
    - [ ] Define testing steps for company migration
    - [ ] Test company creation and mapping

- **Accounts (Tempo)** (`account_migration.py` - may need rename/refactor)
    - [x] Define Account Mapping Strategy (Tempo Account -> OP Custom Field 'Tempo Account')
    - [x] Extract Tempo accounts from Jira via API
    - [x] Define 'Tempo Account' list custom field structure in OpenProject
    - [x] Implement Rails script generation/API call/direct execution for 'Tempo Account' custom field creation
    - [ ] Define execution steps for 'Tempo Account' CF creation
    - [ ] Execute custom field creation for 'Tempo Account' in test environment
    - [x] Implement mapping of Tempo accounts to custom field list values
    - [ ] Define testing steps for account custom field
    - [ ] Test account custom field creation
    - [ ] Test population of account values in the custom field

- **Projects** (`project_migration.py`)
    - [x] Extract Jira projects metadata (key, name, description, etc.)
    - [x] Extract OpenProject projects (for mapping/avoiding duplicates)
    - [x] Define Project Mapping Strategy (Jira Project -> OP Project)
        - [x] Map project attributes (name, identifier, description)
        - [x] Handle parent project relationships (if applicable)
    - [x] Map Jira projects to OpenProject projects (`var/data/project_mapping.json`)
    - [x] Create/Update projects in OpenProject via API
    - [ ] Define testing steps for project migration
    - [ ] Test project creation and attribute mapping
    - [ ] Test project hierarchy mapping

- **Link Types (Relations)** (`link_type_migration.py`)
    - [x] Extract Jira issue link types
    - [x] Extract OpenProject relation types
    - [x] Define Link Type Mapping Strategy (Jira Link -> OP Relation)
    - [x] Map Jira link types to OpenProject relation types (`var/data/link_type_mapping.json`)
    - [x] Create/Update relation types in OpenProject via API (if needed, based on mapping)
    - [ ] Define testing steps for link type/relation migration
    - [ ] Test relation type creation/mapping
    - [ ] Test relation usage in work package migration

- **Issue Types (Work Package Types)** (`issue_type_migration.py`)
    - [x] Extract Jira issue types
    - [x] Define OpenProject work package type equivalents strategy
    - [x] Implement Work Package Type Mapping Strategy (`var/data/issue_type_mapping.json`)
    - [x] Generate Rails script for OpenProject work package type creation (`--generate-ruby`)
    - [x] Implement direct Rails console execution (`--direct-migration`)
    - [x] Define execution steps for Rails script/direct execution
        - Option 1: Direct Migration (Automated):
          1. Ensure OpenProject container is running and Rails console is accessible
          2. Run `python run_migration.py --components issue_types --direct-migration`
          3. The script will:
             - Connect to the Rails console directly
             - Create work package types one by one
             - Update mapping file with new IDs automatically
        - Option 2: Ruby Script (Semi-Automated):
          1. Run `python run_migration.py --components issue_types`
          2. Review the generated Ruby script in the `output` directory
          3. Execute via Docker: `docker exec -it CONTAINER_NAME rails runner path/to/script.rb`
          4. Run `python run_migration.py --update-mapping` to update mapping file
    - [x] Execute Rails script/direct command for work package type creation in test environment
        - Example: `python run_migration.py --components issue_types --direct-migration`
        - Alternatively: Generate Ruby script and execute manually, then update mapping
    - [x] Implement logic to update mapping file with new work package type IDs
    - [x] Define testing steps for work package type creation
        1. Verify work package type count in OpenProject matches expected count
        2. Verify type names match expected names
        3. Verify types appear correctly in work package forms
        4. Verify the mapping file contains correct IDs for all types
        5. Check if types are correctly associated with projects
    - [ ] Test work package type creation and mapping

- **Statuses** (`status_migration.py`)
    - [x] Extract Jira statuses
    - [x] Extract OpenProject statuses
    - [x] Define Status Mapping Strategy
    - [x] Map Jira statuses to OpenProject statuses (`var/data/status_mapping.json`)
    - [ ] Define steps to create/update statuses in OpenProject (likely manual config or Rails, document clearly)
    - [ ] Configure OpenProject statuses based on mapping
    - [ ] Define testing steps for status mapping
    - [ ] Test status mapping correctness

- **Workflows** (`workflow_migration.py`)
    - [x] Extract Jira workflows (statuses and transitions per issue type)
    - [x] Analyze OpenProject workflow capabilities (status transitions per type)
    - [x] Define Workflow Mapping Strategy (preserving basic lifecycle)
    - [x] Map Jira workflow transitions/statuses to OpenProject equivalents per Type (`var/data/workflow_mapping.json`)
    - [ ] Define steps for configuring OpenProject workflows (manual config or Rails, document clearly)
    - [ ] Configure OpenProject workflows based on mapping
    - [ ] Define testing steps for workflow migration
    - [ ] Test workflow state transitions for different work package types

- **Work Packages (Issues)** (`work_package_migration.py`)
    - [x] Implement extraction of Jira issues (including sub-tasks, epics)
        - [x] Handle batching/pagination for large issue counts
    - [x] Define Work Package Mapping Strategy
        - [x] Map Issue -> WP, Epic -> Epic, Sub-task -> Child WP
        - [x] Map core fields (subject, description, assignee, reporter, dates, etc.)
        - [x] Map status based on Status mapping
        - [x] Map type based on Issue Type mapping
        - [x] Map custom fields based on Custom Field mapping
        - [x] Map Tempo Account custom field value
    - [x] Implement Issue Link mapping (using Link Type mapping)
    - [x] Implement Attachment handling
        - [x] Download attachments from Jira
        - [x] Upload attachments to OpenProject
    - [x] Implement Comment handling
        - [x] Extract comments from Jira
        - [x] Create comments in OpenProject
    - [x] Implement Work Package creation/update in OpenProject
        - [x] Handle batching for API efficiency
        - [x] Set parent work package links (for hierarchy)
        - [x] Create work package relations (for links)
    - [ ] Define testing steps for work package migration
        - [ ] Test basic field mapping accuracy (spot checks)
        - [ ] Test work package hierarchy (Epics, children)
        - [ ] Test work package relations (links)
        - [ ] Test attachment migration
        - [ ] Test comment migration
        - [ ] Test status and type mapping
        - [ ] Test custom field value migration (including Tempo Account)
        - [ ] Perform data validation (counts, specific examples)

## Phase 3: Refinement, Testing & Validation

- [ ] **Refine Migration Scripts:**
    - [ ] Enhance error handling and resilience
    - [ ] Improve logging and progress reporting
    - [ ] Optimize performance (API calls, data processing)
    - [ ] Refine mapping logic and add configuration options
- [x] **Implement Comprehensive Testing & Validation:**
    - [x] Define Data Validation Strategy
    - [x] Implement Automated Validation Checks (counts, key fields, use `src/cleanup_openproject.py`?)
    - [ ] Perform Manual Spot Checks across all migrated components
    - [ ] Validate Migrated Data (Component-wise & End-to-End)
- [ ] **Perform User Acceptance Testing (UAT):**
    - [ ] Define UAT Scenarios covering key migration aspects
    - [ ] Schedule and conduct UAT Sessions with stakeholders
    - [ ] Collect and Address UAT Feedback
- [ ] **Refine & Test Rollback Strategy:**
    - [ ] Refine Rollback Procedures based on testing
    - [ ] Test Rollback Procedures thoroughly in the test environment

## Phase 4: Documentation & Production Migration

- [ ] **Finalize Documentation:**
    - [x] Update `README.md` (ongoing)
    - [x] Update `docs/configuration.md` (ongoing)
    - [ ] Update/Create `docs/development.md`
    - [ ] Update/Create `src/README.md`
    - [ ] Update/Create `scripts/README.md`
    - [ ] Update/Create `tests/README.md`
    - [ ] Ensure all manual steps are clearly documented
    - [ ] **Remove `PROGRESS.md` after content transfer.**
- [ ] **Schedule Production Migration:**
    - [ ] Define Downtime Window
    - [ ] Prepare Communication Plan
- [ ] **Execute Production Migration:**
    - [ ] Perform final dry run
    - [ ] Execute migration during scheduled window (`python run_migration.py`)
    - [ ] Monitor progress closely
- [ ] **Post-Migration:**
    - [ ] Perform post-migration verification checks in production
    - [ ] Address any immediate issues
    - [ ] Handover documentation and procedures

## Manual Steps Required During Migration

_(Consolidated from PROGRESS.md - Ensure these are detailed in the final runbook/documentation)_

1.  **Custom Fields Import (if not using `--direct-migration`):**
    *   Generate Ruby script (`--generate-ruby`).
    *   Review script.
    *   Run via Rails console.
    *   Update `custom_field_mapping.json` if needed.
2.  **Work Package Types Import (if not using `--direct-migration`):**
    *   Generate Ruby script (`--generate-ruby`).
    *   Review script.
    *   Run via Rails console.
    *   Update `issue_type_mapping.json` if needed.
3.  **Statuses and Workflows Configuration:**
    *   Manually configure Statuses in OpenProject Admin based on `status_mapping.json`.
    *   Manually configure Workflows in OpenProject Admin based on `workflow_mapping.json` analysis.
4.  **'Tempo Account' Custom Field Creation (if needed):**
    *   Execute necessary Rails commands/script (potentially generated by `account_migration.py`).
    *   Ensure the CF is enabled for relevant projects/types.
