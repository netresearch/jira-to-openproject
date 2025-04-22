# Project Goals

## Primary Goal

To develop a robust and configurable application for migrating project management data from **Jira Server 9.11** to **OpenProject 15**.

## Key Features

The migration tool aims to provide the following capabilities:

* **Comprehensive Data Migration:** Migrate essential project entities, including:
  * Users (with mapping strategies, e.g., AD/LDAP)
  * Projects (including hierarchy if applicable)
  * Issues / Work Packages (including epics, sub-tasks, core fields, custom fields)
  * Statuses
  * Issue Types / Work Package Types
  * Workflows (mapping transitions)
  * Issue Links / Work Package Relations
  * Attachments
  * Comments
  * Custom Fields (handling type mapping)
  * Specific Jira data like Tempo Accounts (mapping to OpenProject custom fields)
* **Handle API Limitations:** Implement workarounds for known API limitations in both Jira and OpenProject:
  * Utilize OpenProject Rails console (via SSH/Docker) for creating entities not supported by the API (e.g., Custom Fields, Work Package Types).
  * Optionally generate Ruby scripts for manual Rails console execution.
  * Address Jira API inconsistencies (e.g., field expansion, custom field option retrieval, potentially integrating with ScriptRunner).
* **Configuration Flexibility:** Allow users to configure:
  * Connection details for Jira and OpenProject (URL, credentials).
  * Migration parameters (batch sizes, rate limits).
  * Mapping strategies (e.g., for users, statuses, types).
  * SSL verification settings.
* **Modular Design:** Structure the migration process into distinct, runnable components.
* **Idempotency (Attempted):** Where possible, design migrations to be runnable multiple times without creating duplicate data (e.g., by checking for existing entities).
* **Dry-Run Capability:** Allow users to simulate a migration without making actual changes to the target OpenProject instance.
* **Clear Logging and Reporting:** Provide informative logs and progress indicators during the migration process.
* **Dockerized Environment:** Ensure easy setup and consistent execution using Docker containers.
