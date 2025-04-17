# Feature Overview for Jira → OpenProject Migration

This document outlines the key features and behaviors of the migration tool for:

- Tempo Customer (Company) Migration
- Tempo Account Migration
- Jira Project Migration (as sub‑projects under customers)

---

## 1. Customer Migration

- **Source**: Tempo Customers (↔ Jira Custom/Cust. API `/rest/tempo-accounts/1/customer`).
- **Target**: Top‑level OpenProject projects.
- **Mapping**:
  1. Extract all Tempo customers.
  2. Extract existing OpenProject projects via API.
  3. Match each Tempo customer by name to an existing OP project, or create a new one (Rails console required).
  4. Produce `company_mapping.json`: maps `tempo_id` → `{ openproject_id, identifier, name, matched_by }`.

**Outcome**: Every Tempo customer becomes a top‑level OP project. Failures or missing matches emit warnings.

---

## 2. Account Migration

- **Source**: Tempo Accounts (↔ Jira API `/rest/tempo-accounts/1/account`).
- **Target**: A single "Tempo Account" custom field on OP projects.
- **Mapping**:
  1. Extract Tempo accounts (with `companyId` and default Jira project links).
  2. Extract all existing OP projects.
  3. Build `account_mapping.json`:
     - Maps `tempo_id` → `{ openproject_id, identifier, name, parent_id (OP project), matched_by }`.
  4. Create or locate the "Tempo Account" custom field via Rails console.
  5. For each Tempo account, record its ID as a possible value on that custom field.

**Outcome**: All accounts appear in a single custom field on each project. Accounts may be matched by name or default project.

---

## 3. Project Migration

- **Source**: Jira Projects (↔ Jira API `get_projects()`).
- **Target**: OpenProject sub‑projects under their owning customer projects.
- **Key Behaviors**:
  1. **Rails‑Only Bulk Import**: All project creations/updates run through the Rails console client (no API fallback).
  2. **Identifier Generation**: Jira key → lowercase slug (non‑alphanumeric replaced with `-`, ensure starts with letter).
  3. **Account Association**: Fetch each Jira project's **default Tempo account** (via project‑account mapping).
  4. **Parent Lookup** (Customer):
     - Use the default account's `company_id` to retrieve its Tempo customer record.
     - From `company_mapping.json`, find the corresponding OP project for that customer.
     - That project becomes the **parent** under which the Jira project is created.
     - If any of these steps fail (no default account, no company_id, no company mapping), emit a detailed warning stating the exact missing piece.
  5. **Error & Warning Reporting**:
     - If no default account is found, emit a warning and continue without parent.
     - If parent lookup fails, emit a detailed warning (`key/id/name`) explaining why.
     - All migration failures/errors are surfaced in logs and in generated mapping files (`project_mapping.json`).

**Outcome**: Each Jira project ends up as a true sub‑project under the correct customer in OpenProject.

---

### Next Steps

1. Add unit tests for `find_parent_company_for_project`, verifying correct parent resolution and warning paths.
2. Run end‑to‑end migration on a staging instance to validate hierarchical relationships.

---

*End of features documentation.*
