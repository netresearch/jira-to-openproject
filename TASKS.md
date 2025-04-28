# Migration Tasks

This document tracks the detailed tasks required for the Jira to OpenProject migration. It combines planning, implementation status, and testing/validation requirements.

## Phase 1: Planning & Setup

* [x] Define acceptance criteria
* [x] Clarify specifications
* [x] Identify impediments
* [x] Identify what to import and in which order
* [x] Select programming language (Python 3.13)
* [x] Create data mapping strategy (initial)
* [x] Set up test environment (Docker-based)
* [x] Create initial Rollback Strategy
* [x] Setup Project Structure (Python, Docker, Git)
* [x] Implement Configuration Loading
* [x] Implement basic API Clients (Jira, OpenProject)
* [x] Implement Core Migration Runner (now in `src/migration.py` with `src/main.py` entry point)

## Phase 2: Component Implementation & Migration

Each component migration involves extraction, mapping, creation/update in OpenProject, and definition of testing steps.

* **Users** (`user_migration.py`)
  * [x] Extract Jira users
  * [x] Extract OpenProject users
  * [x] Implement User Mapping Strategy (AD/LDAP assumption)
    * [x] Load existing user mappings if available (`var/data/user_mapping.json`)
    * [x] Map users based on email/username
  * [x] Create/Update users in OpenProject via API
  * [x] Define testing steps for user migration
  * [x] Test user creation/update accuracy
  * [x] Test user mapping correctness

* **Custom Fields** (`custom_field_migration.py`)
  * [x] Extract Jira custom fields metadata (handling API limitations, consider ScriptRunner)
  * [x] Define OpenProject custom field equivalents strategy
  * [x] Implement Custom Field Mapping Strategy
    * [x] Map Jira field types to OpenProject field types
    * [x] Handle custom field options (potential Jira API bottleneck)
  * [x] Generate Rails script for OpenProject custom field creation (`--generate-ruby`)
  * [x] Implement direct Rails console execution (`--direct-migration`)
  * [x] Define execution steps for Rails script/direct execution
    * Option 1: Direct Migration (Automated):
      1. Ensure OpenProject container is running and Rails console is accessible
      2. Run `python src/main.py --components custom_fields --direct-migration`
      3. The script will:
          * Connect to the Rails console directly
          * Create custom fields one by one
          * Update mapping file with new IDs automatically
    * Option 2: Ruby Script (Semi-Automated):
      1. Run `python src/main.py --components custom_fields`
      2. Review the generated Ruby script in the `output` directory
      3. Execute via Docker: `docker exec -it CONTAINER_NAME rails runner path/to/script.rb`
      4. Run `python src/main.py --update-mapping` to update mapping file
  * [x] Execute Rails script/direct command for custom field creation in test environment
    * Example: `python src/main.py --components custom_fields --direct-migration`
    * Alternatively: Generate Ruby script and execute manually, then update mapping
  * [x] Implement logic to update mapping file (`var/data/custom_field_mapping.json`) with new custom field IDs
  * [x] Define testing steps for custom field creation
    1. Verify custom field count in OpenProject matches expected count
    2. Verify field names match expected names
    3. Verify field types are correctly mapped (text, list, date, etc.)
    4. For list fields, verify values are correctly populated
    5. Verify fields appear correctly in work package forms
    6. Verify the mapping file contains correct IDs for all fields
  * [x] Test custom field creation and type mapping

* **Companies** (`company_migration.py`)
  * [x] Define Company Mapping Strategy (e.g., specific Jira field -> OP top-level projects)
  * [x] Extract necessary Jira data for companies
  * [x] Extract existing OpenProject projects (for matching)
  * [x] Map Jira company data to OpenProject project structure
  * [x] Create/Update top-level projects (Companies) in OpenProject via API
  * [x] Define testing steps for company migration
    1. Verify company extraction from Tempo API
    2. Verify correct mapping between Tempo companies and OpenProject projects
    3. Verify company creation in OpenProject with correct attributes
    4. Test the migration process for creating unmatched companies
    5. Test the analysis functionality for reporting on mapping status
  * [x] Test company creation and mapping

* **Accounts (Tempo)** (`account_migration.py` - may need rename/refactor)
  * [x] Define Account Mapping Strategy (Tempo Account -> OP Custom Field 'Tempo Account')
  * [x] Extract Tempo accounts from Jira via API
  * [x] Define 'Tempo Account' list custom field structure in OpenProject
  * [x] Implement Rails script generation/API call/direct execution for 'Tempo Account' custom field creation
  * [x] Define execution steps for 'Tempo Account' CF creation
    * Option 1: Direct Migration (Automated):
      1. Ensure OpenProject container is running and Rails console is accessible
      2. Run `python src/main.py --components accounts --direct-migration`
      3. The script will:
          * Connect to the Rails console directly
          * Check if 'Tempo Account' custom field already exists
          * Create the custom field if it doesn't exist
          * Populate it with all Tempo account names as possible values
          * Make the custom field available to all work package types
          * Update mapping file with the custom field ID automatically
    * Option 2: Manual Configuration (Semi-Automated):
        1. Run `python src/main.py --components accounts`
        2. Manually create a 'Tempo Account' list custom field in OpenProject admin interface
        3. Add all Tempo account names as possible values for the list
        4. Make the custom field available to all work package types
        5. Update the analysis file with the custom field ID: `python src/migrations/account_migration.py --analyze`
  * [x] Execute custom field creation for 'Tempo Account' in test environment
  * [x] Define testing steps for account custom field
        1. Verify the 'Tempo Account' custom field exists in OpenProject admin interface
        2. Verify the custom field is of type 'List' with correct possible values
        3. Verify all Tempo account names are available as options in the list
        4. Verify the field is available for all work package types
        5. Create a test work package and ensure the 'Tempo Account' field can be set
        6. Verify the mapping contains the correct custom field ID
        7. Verify the account_mapping_analysis.json file has been created with correct data
        8. Verify account mapping contains connections between Tempo accounts and OpenProject projects
  * [x] Test account custom field creation
  * [x] Test population of account values in the custom field

* **Projects** (`project_migration.py`)
  * [x] Extract Jira projects metadata (key, name, description, etc.)
  * [x] Extract OpenProject projects (for mapping/avoiding duplicates)
  * [x] Define Project Mapping Strategy (Jira Project -> OP Project)
    * [x] Map project attributes (name, identifier, description)
    * [x] Handle parent project relationships (if applicable)
  * [x] Map Jira projects to OpenProject projects (`var/data/project_mapping.json`)
  * [x] Create/Update projects in OpenProject via API
  * [x] Define testing steps for project migration
  * [x] Test project creation and attribute mapping
  * [x] Test project hierarchy mapping

* **Link Types (Relations)** (`link_type_migration.py`)
  * [x] Extract Jira issue link types
  * [ ] ~~Extract OpenProject relation types~~ **(BLOCKED: No OpenProject API to retrieve link types)**
  * [x] Define Link Type Mapping Strategy
    * [ ] Provide default mapping to standard OpenProject relation types:
      * relates to
      * duplicates/duplicated by
      * blocks/blocked by
      * precedes/follows
      * includes/part of
    * [ ] Implement user-configurable mapping via JSON file
    * [ ] Add custom field fallback for unmapped link types
  * [x] Map Jira link types to OpenProject relation types (`var/data/link_type_mapping.json`)
  * [ ] ~~Create/Update relation types in OpenProject~~ **(BLOCKED: OpenProject doesn't allow adding/editing relation types)**
  * [ ] Create custom fields for unmapped link types that cannot be represented with standard relation types
  * [ ] Implement user mapping template generation (`--generate-mapping-template`)
  * [x] Define testing steps for link type/relation migration
  * [x] Test relation type mapping
  * [x] Test relation usage in work package migration
  * [ ] Test custom field implementation for unmapped link types

* **Issue Types (Work Package Types)** (`issue_type_migration.py`)
  * [x] Extract Jira issue types
  * [x] Define OpenProject work package type equivalents strategy
  * [x] Implement Work Package Type Mapping Strategy (`var/data/issue_type_mapping.json`)
  * [x] Generate Rails script for OpenProject work package type creation (`--generate-ruby`)
  * [x] Implement direct Rails console execution (`--direct-migration`)
  * [x] Define execution steps for Rails script/direct execution
    * Option 1: Direct Migration (Automated):
      1. Ensure OpenProject container is running and Rails console is accessible
      2. Run `python src/main.py --components issue_types --direct-migration`
      3. The script will:
          * Connect to the Rails console directly
          * Create work package types one by one
          * Update mapping file with new IDs automatically
    * Option 2: Ruby Script (Semi-Automated):
      1. Run `python src/main.py --components issue_types`
      2. Review the generated Ruby script in the `output` directory
      3. Execute via Docker: `docker exec -it CONTAINER_NAME rails runner path/to/script.rb`
      4. Run `python src/main.py --update-mapping` to update mapping file
  * [x] Execute Rails script/direct command for work package type creation in test environment
    * Example: `python src/main.py --components issue_types --direct-migration`
    * Alternatively: Generate Ruby script and execute manually, then update mapping
  * [x] Implement logic to update mapping file with new work package type IDs
  * [x] Define testing steps for work package type creation
    1. Verify work package type count in OpenProject matches expected count
    2. Verify type names match expected names
    3. Verify types appear correctly in work package forms
    4. Verify the mapping file contains correct IDs for all types
    5. Check if types are correctly associated with projects
  * [x] Test work package type creation and mapping

* **Statuses** (`status_migration.py`)
  * [x] Extract Jira statuses
  * [x] Extract OpenProject statuses
  * [x] Define Status Mapping Strategy
  * [x] Map Jira statuses to OpenProject statuses (`var/data/status_mapping.json`)
  * [x] Define steps to create/update statuses in OpenProject (likely manual config or Rails, document clearly)
  * [x] Configure OpenProject statuses based on mapping
  * [x] Define testing steps for status mapping
  * [x] Test status mapping correctness

* **Workflows** (`workflow_migration.py`)
  * [x] Extract Jira workflows (statuses and transitions per issue type)
  * [x] Analyze OpenProject workflow capabilities (status transitions per type)
  * [x] Define Workflow Mapping Strategy (preserving basic lifecycle)
  * [x] Map Jira workflow transitions/statuses to OpenProject equivalents per Type (`var/data/workflow_mapping.json`)
  * [x] Define steps for configuring OpenProject workflows (manual config or Rails, document clearly)
  * [x] Configure OpenProject workflows based on mapping
  * [x] Define testing steps for workflow migration
  * [x] Test workflow state transitions for different work package types

* **Work Packages (Issues)** (`work_package_migration.py`)
  * [x] Implement extraction of Jira issues (including sub-tasks, epics)
    * [x] Handle batching/pagination for large issue counts
  * [x] Define Work Package Mapping Strategy
    * [x] Map Issue -> WP, Epic -> Epic, Sub-task -> Child WP
    * [x] Map core fields (subject, description, assignee, reporter, dates, etc.)
    * [x] Map status based on Status mapping
    * [x] Map type based on Issue Type mapping
    * [x] Map custom fields based on Custom Field mapping
    * [x] Map Tempo Account custom field value
  * [x] Implement Issue Link mapping (using Link Type mapping)
  * [x] Implement Attachment handling
    * [x] Download attachments from Jira
    * [x] Upload attachments to OpenProject
  * [x] Implement Comment handling
    * [x] Extract comments from Jira
    * [x] Create comments in OpenProject
  * [x] Implement Work Package creation/update in OpenProject
    * [x] Handle batching for API efficiency
    * [x] Set parent work package links (for hierarchy)
    * [x] Create work package relations (for links)
  * [x] Define testing steps for work package migration
    * [x] Test basic field mapping accuracy (spot checks)
    * [x] Test work package hierarchy (Epics, children)
    * [x] Test work package relations (links)
    * [x] Test attachment migration
    * [x] Test comment migration
    * [x] Test status and type mapping
    * [x] Test custom field value migration (including Tempo Account)
    * [x] Perform data validation (counts, specific examples)

## Phase 2.5: High Priority Improvements (NEW)

* [ ] **Remove obsolete project API code** (`project_migration.py`)
  * [ ] Remove fallback/obsolete code handling projects added via API
  * [ ] Ensure all project creation occurs through Rails console client
* [x] **Migrate Tempo Customer metadata** (`company_migration.py`)
  * [x] Migrate meta data from Tempo customer to OpenProject customer main project
* [ ] **Set Top-Level Project Status from Tempo** (`company_migration.py`)
  * [ ] Extract Tempo customer status information
  * [ ] Map Tempo customer status to equivalent OpenProject project status
  * [ ] Apply status to top-level customer projects during creation/update
  * [ ] Test status mapping and application accuracy
* [ ] **Add Tempo Customer Metadata Custom Fields** (`custom_field_migration.py`, `company_migration.py`)
  * [ ] Create custom fields for Tempo customer metadata:
    * [ ] Tempo name (string)
    * [ ] Tempo key (string)
    * [ ] Tempo ID (integer)
    * [ ] Tempo accounts (list)
    * [ ] Tempo URL (string)
  * [ ] Populate custom fields with Tempo customer data during company migration
  * [ ] Update custom field mapping to include new Tempo metadata fields
  * [ ] Test custom field creation and data population
* [x] **Fix Project Hierarchy Structure** (`project_migration.py`)
  * [x] Implement hierarchical project structure (Tempo Company -> Jira Project relationship)
  * [x] Modify project migration to create Tempo Companies as main projects
  * [x] Make Jira Projects sub-projects of their respective Tempo Company
  * [x] Update project mapping to reflect hierarchical structure
  * [x] Test hierarchical project creation and relationships

* [x] **Normalize Issue Types** (`issue_type_migration.py`)
  * [x] Identify issue types starting with "Sub:" or "Sub-"
  * [x] Map these to their corresponding "normal" issue types
  * [x] Update issue type mapping to reflect normalized types
  * [x] Test issue type normalization

* [x] **Complete Work Package Field Migration** (`work_package_migration.py`)
  * [x] Add missing meta fields:
    * [x] Creation date and update date
    * [x] Creator and author
    * [x] Watchers
  * [x] Implement custom field value migration
  * [x] Test complete field migration accuracy

* [ ] **Enhance ScriptRunner Integration** (`jira_client.py`)
  * [ ] Update ScriptRunner 'search' endpoint to include watchers in results
  * [ ] Update watchers extraction in work package migration
  * [ ] Test watchers extraction via both ScriptRunner and native API
  * [ ] Optimize issue fetching when watchers are needed

* [ ] **Implement Comments, Attachments, Work Log Migration** (`work_package_migration.py`)
  * [ ] Extract and migrate issue comments
  * [ ] Handle attachment downloading and uploading
  * [ ] Migrate work logs (time entries)
  * [ ] Test comment, attachment, and work log migration

* [ ] **Fix Work Package Relationships** (`work_package_migration.py`)
  * [ ] Implement proper Epic-Issue relationship (as parent-child)
  * [ ] Implement sub-issue relationships (as parent-child)
  * [ ] Migrate all other issue link types
  * [ ] Test work package relationship accuracy
  * [ ] Ensure proper hierarchy and links between work packages

## Phase 3: Bugfixes and Improvements

* [ ] **Improve User Migration Process** (`user_migration.py`)
  * [ ] Refactor to use JSON file method for user import like other components
  * [ ] Implement consistent extraction/transformation/loading pattern
  * [ ] Ensure proper caching of user data to avoid redundant API calls
  * [ ] Update data storage to use centralized file storage mechanism
  * [ ] Test improved user migration process for consistency with other components

* [ ] **Fix Custom Field Migration Warnings** (`custom_field_migration.py`)
  * [ ] Address "WARNING: Action required: 170 custom fields need manual creation or script execution" issue
  * [ ] Ensure custom fields are properly created as part of the component's task
  * [ ] Implement automatic creation of all required custom fields
  * [ ] Remove manual intervention requirements where possible
  * [ ] Add better error handling for custom field creation failures
  * [ ] Test complete end-to-end custom field migration without manual steps

* [ ] **Optimize API Request Caching** (`openproject_client.py`, `company_migration.py`)
  * [ ] Fix excessive cached API requests issue during company/customer migration
  * [ ] Ensure requests come from stored migration data instead of live API calls
  * [ ] Implement better caching strategy for OpenProject client
  * [ ] Reduce redundant API calls for projects when migrating companies
  * [ ] Add efficient data retrieval patterns for all components
  * [ ] Test reduced API call volume during migration

* [ ] **Unify Customer/Company Terminology** (multiple files)
  * [ ] Refactor code to consistently use "customer" instead of "company"
  * [ ] Update class and file names to reflect consistent terminology
  * [ ] Update documentation to clarify that companies = customers
  * [ ] Ensure consistent naming in logs and user messages
  * [ ] Test that all references are updated correctly

* [ ] **Fix Component Migration Counters** (multiple files)
  * [ ] Fix incorrect success/failure counters in migration components
  * [ ] Address "wrong numbers in SUCCESS Component 'companies' completed successfully (0/0 items migrated)" issue
  * [ ] Address "wrong numbers in SUCCESS Component 'accounts' completed successfully (0/0 items migrated)" issue
  * [ ] Address "wrong numbers in SUCCESS Component 'projects' completed successfully (0/0 items migrated)" issue
  * [ ] Ensure accurate counting of processed, successful, and failed items
  * [ ] Implement consistent counter tracking across all components
  * [ ] Test counter accuracy with various data sets

## Phase 4: Documentation & Production Migration

* [ ] **Finalize Documentation:**
  * [x] Update `README.md` (ongoing)
  * [x] Update `docs/configuration.md` (ongoing)
  * [x] Update/Create `docs/development.md`
  * [x] Update/Create `src/README.md`
  * [ ] Update/Create `scripts/README.md`
  * [ ] Update/Create `tests/README.md`
  * [ ] Ensure all manual steps are clearly documented
  * [ ] **Remove `PROGRESS.md` after content transfer.**
* [ ] **Schedule Production Migration:**
  * [ ] Define Downtime Window
  * [ ] Prepare Communication Plan
* [ ] **Execute Production Migration:**
  * [ ] Perform final dry run
  * [ ] Execute migration during scheduled window (`python src/main.py`)
  * [ ] Monitor progress closely
* [ ] **Post-Migration:**
  * [ ] Perform post-migration verification checks in production
  * [ ] Address any immediate issues
  * [ ] Handover documentation and procedures

## Manual Steps Required During Migration

_(Consolidated from PROGRESS.md - Ensure these are detailed in the final runbook/documentation)_

1. **Custom Fields Import (if not using `--direct-migration`):**
    * Generate Ruby script (`--generate-ruby`).
    * Review script.
    * Run via Rails console.
    * Update `custom_field_mapping.json`
2. **Work Package Types Import (if not using `--direct-migration`):**
    * Generate Ruby script (`--generate-ruby`).
    * Review script.
    * Run via Rails console.
    * Update `issue_type_mapping.json` if needed.
3. **Statuses and Workflows Configuration:**
    * Manually configure Statuses in OpenProject Admin based on `status_mapping.json`.
    * Manually configure Workflows in OpenProject Admin based on `workflow_mapping.json` analysis.
4. **'Tempo Account' Custom Field Creation (if needed):**
    * Execute necessary Rails commands/script (potentially generated by `account_migration.py`).
    * Ensure the CF is enabled for relevant projects/types.
